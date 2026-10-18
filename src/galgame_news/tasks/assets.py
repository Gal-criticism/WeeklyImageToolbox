"""Content-addressed, task-local image files (the database stores metadata only)."""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from io import BytesIO
from pathlib import Path

from PIL import Image, UnidentifiedImageError

_SAFE_SUFFIX = re.compile(r"^\.[a-z0-9]{1,8}$")


class TaskAssets:
    """Atomic content-addressed files rooted in an authoritative task."""

    def __init__(self, task_root: Path | str):
        self.root = Path(task_root).resolve()

    @classmethod
    def for_path(cls, path: Path | str) -> "TaskAssets | None":
        from .repository import TaskRepository

        candidate = Path(path).resolve()
        if candidate.is_file():
            candidate = candidate.parent
        for parent in (candidate, *candidate.parents):
            if TaskRepository.is_authoritative(parent):
                return cls(parent)
        return None

    @staticmethod
    def _suffix(suffix: str) -> str:
        value = str(suffix).lower()
        if not _SAFE_SUFFIX.fullmatch(value):
            raise ValueError("unsafe asset suffix")
        return value

    def _put(self, directory: Path, data: bytes, suffix: str) -> Path:
        suffix = self._suffix(suffix)
        digest = hashlib.sha256(data).hexdigest()
        directory.mkdir(parents=True, exist_ok=True)
        try:
            directory.resolve().relative_to(self.root)
        except ValueError:
            raise ValueError("task asset directory escapes task root") from None
        target = directory / f"{digest}{suffix}"
        try:
            target.resolve().relative_to(self.root)
        except ValueError:
            raise ValueError("task asset path escapes task root") from None
        if target.exists():
            if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                raise OSError("content-addressed asset hash mismatch")
            return target
        fd, temporary_name = tempfile.mkstemp(
            prefix=".asset-", suffix=".part", dir=directory
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            # A concurrent identical writer may win; never replace its file.
            try:
                os.link(temporary, target)
            except FileExistsError:
                if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                    raise OSError("content-addressed asset hash mismatch")
            except OSError as link_error:
                # Some filesystems (notably exFAT and network mounts) do not
                # support hard links. Both names are on the same volume, so an
                # atomic rename can still publish the complete staged file.
                # Recheck a concurrent winner before replacing anything.
                if target.exists():
                    if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                        raise OSError(
                            "content-addressed asset hash mismatch"
                        ) from link_error
                    return target
                os.replace(temporary, target)
                if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                    raise OSError("content-addressed asset hash mismatch")
            return target
        finally:
            temporary.unlink(missing_ok=True)

    def put_original(
        self, source: Path | str | bytes, suffix: str | None = None
    ) -> Path:
        if isinstance(source, bytes):
            data = source
        else:
            path = Path(source)
            data = path.read_bytes()
            suffix = suffix or path.suffix
        target = self.original_path(data, suffix or ".img")
        return self._put(target.parent, data, target.suffix)

    def original_path(self, data: bytes, suffix: str) -> Path:
        """Use decoded format, so URL suffix variants share one original."""
        canonical_suffix = self._suffix(suffix)
        try:
            with Image.open(BytesIO(data)) as source:
                canonical_suffix = {"JPEG": ".jpg", "PNG": ".png", "GIF": ".gif",
                                    "WEBP": ".webp", "AVIF": ".avif", "BMP": ".bmp",
                                    "TIFF": ".tiff"}.get(source.format, canonical_suffix)
        except (UnidentifiedImageError, OSError, ValueError):
            # Non-image legacy assets retain their validated extension.
            pass
        return self.root / "assets" / "originals" / f"{hashlib.sha256(data).hexdigest()}{canonical_suffix}"

    def put_review(self, data: bytes, suffix: str, profile: str = "v1") -> Path:
        if not re.fullmatch(r"[a-zA-Z0-9._-]{1,40}", profile):
            raise ValueError("unsafe conversion profile")
        # Profile is part of the namespace: changed conversion settings cannot
        # accidentally reuse output produced by an earlier profile.
        return self._put(self.root / "assets" / "review" / profile, data, suffix)
