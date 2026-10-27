"""Typed, non-secret application settings and their JSON store."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
from typing import Literal, Mapping, Any

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, ValidationError


APP_DATA_NAME = "GalgameNewsToolbox"


def default_app_data_dir(environ: Mapping[str, str] | None = None) -> Path:
    """Return the platform app-data directory without creating it.

    The Windows path intentionally matches :class:`TaskStore` so settings and
    task catalogs live below the same portable-toolbox root.  ``environ`` is
    injectable for deterministic tests and for embedded launchers.
    """

    values = environ if environ is not None else os.environ
    if os.name == "nt":
        base = values.get("LOCALAPPDATA") or values.get("APPDATA")
        if base:
            return Path(base).expanduser() / APP_DATA_NAME
        return Path.home() / "AppData" / "Local" / APP_DATA_NAME

    xdg = values.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg).expanduser() / APP_DATA_NAME
    if os.name == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_DATA_NAME
    return Path.home() / ".config" / APP_DATA_NAME


def _default_output_path() -> Path:
    return default_app_data_dir() / "output"


class ArchiveSettings(BaseModel):
    """Non-secret connection and indexing settings for past weekly material."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    server_url: str = ""
    username: str = ""
    root_path: str = ""
    local_roots: list[str] = Field(default_factory=list)
    range_days: int = Field(default=365, gt=0)


class AppSettings(BaseModel):
    """Only values safe to persist as ordinary JSON.

    Credentials are intentionally absent from this model.  They belong in a
    keyring-backed :class:`~galgame_news.settings.CredentialStore`.
    """

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        validate_assignment=True,
    )

    default_output_path: Path = Field(default_factory=_default_output_path)
    max_image_bytes: int = Field(
        default=12_582_912,
        gt=0,
        validation_alias=AliasChoices("max_image_bytes", "image_max_bytes"),
    )
    max_image_width: int = Field(default=0, ge=0)
    max_image_height: int = Field(default=0, ge=0)
    max_video_bytes: int = Field(
        default=1_073_741_824,
        gt=0,
        validation_alias=AliasChoices("max_video_bytes", "video_max_bytes"),
    )
    max_video_duration_seconds: int = Field(
        default=600,
        gt=0,
        validation_alias=AliasChoices(
            "max_video_duration_seconds", "video_max_duration_seconds"
        ),
    )
    max_video_height: int = Field(default=1080, gt=0)
    theme: Literal["system", "light", "dark"] = "system"
    browser_enabled: bool = True
    archive: ArchiveSettings = Field(default_factory=ArchiveSettings)
    ffmpeg_location: Path | None = Field(
        default=None,
        validation_alias=AliasChoices("ffmpeg_location", "ffmpeg_path"),
    )
    ffprobe_location: Path | None = Field(
        default=None,
        validation_alias=AliasChoices("ffprobe_location", "ffprobe_path"),
    )

    # Compatibility aliases keep the model pleasant to use from callers that
    # phrase the limits as image/video settings while the JSON schema remains
    # stable and explicit.
    @property
    def image_max_bytes(self) -> int:
        return self.max_image_bytes

    @property
    def video_max_bytes(self) -> int:
        return self.max_video_bytes

    @property
    def video_max_duration_seconds(self) -> int:
        return self.max_video_duration_seconds

    @property
    def ffmpeg_path(self) -> Path | None:
        return self.ffmpeg_location

    @property
    def ffprobe_path(self) -> Path | None:
        return self.ffprobe_location

    def json_payload(self) -> dict[str, Any]:
        """Return the complete non-secret JSON payload for persistence."""

        # Keep this explicit even if the model grows later.  A settings
        # subclass is rejected by SettingsStore, and no future/accidental
        # attribute can become persisted by implication.
        return self.model_dump(
            mode="json",
            include=set(type(self).model_fields).intersection(AppSettings.model_fields),
        )

class SettingsStore:
    """Atomically persist :class:`AppSettings` in app data."""

    def __init__(
        self,
        path: Path | str | None = None,
        *,
        app_data: Path | str | None = None,
    ) -> None:
        if path is not None and app_data is not None:
            raise ValueError("provide path or app_data, not both")
        self.path = (
            Path(path).expanduser()
            if path is not None
            else Path(app_data).expanduser() / "settings.json"
            if app_data is not None
            else default_app_data_dir() / "settings.json"
        )

    def load(self) -> AppSettings:
        if not self.path.exists():
            return AppSettings()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("settings JSON must be an object")
            return AppSettings.model_validate(payload)
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError, ValidationError):
            # Keep diagnostics free of file contents: a malformed settings
            # file must not accidentally echo a credential-like value.
            raise ValueError(f"invalid settings file: {self.path}") from None

    read = load

    def save(self, settings: AppSettings | Mapping[str, Any]) -> None:
        if isinstance(settings, AppSettings):
            if type(settings) is not AppSettings:
                raise ValueError("exact AppSettings instance required")
            validated = settings
        else:
            try:
                validated = AppSettings.model_validate(dict(settings))
            except (TypeError, ValueError, ValidationError):
                raise ValueError("invalid non-secret settings") from None

        payload = validated.json_payload()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            if os.name != "nt":
                try:
                    self.path.chmod(0o600)
                except OSError:
                    pass
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    write = save
