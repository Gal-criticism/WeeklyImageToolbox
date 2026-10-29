"""Data models shared by archive indexing and materialization."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class ArchiveAsset:
    asset_id: str
    source_id: str
    kind: str
    issue_number: str
    path: str
    name: str
    game_names: tuple[str, ...] = ()
    issue_date: str | None = None
    news_title: str = ""
    revision: str | None = None
    size: int | None = None
    source_kind: str = "local"
    local_path: str | None = None
    original_url: str | None = None
    original_source_url: str | None = None
    preview_url: str | None = None
    version: str | None = None
    date_suspect: bool = False
    metadata: dict[str, Any] = field(default_factory=dict, compare=True)

    def __post_init__(self) -> None:
        if self.kind not in {"image", "video"}:
            raise ValueError("kind must be 'image' or 'video'")
        if self.source_kind not in {"local", "nextcloud"}:
            raise ValueError("source_kind must be 'local' or 'nextcloud'")
        object.__setattr__(self, "game_names", tuple(self.game_names or ()))
        if (
            self.source_kind == "nextcloud"
            and self.original_source_url
            and self.original_url
            and self.original_source_url == self.original_url
        ):
            # Legacy indexes duplicated the WebDAV file URL into the original
            # news-source field.  It proves the archive copy, not the upstream
            # publisher URL, so keep it only as the materialization URL.
            object.__setattr__(self, "original_source_url", None)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ArchiveAsset":
        values = dict(data)
        if "game_names" in values and not isinstance(values["game_names"], tuple):
            values["game_names"] = tuple(values["game_names"] or ())
        allowed = cls.__dataclass_fields__.keys()
        return cls(**{key: value for key, value in values.items() if key in allowed})
