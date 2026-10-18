"""Confined asset path rebasing and recovery for atomic publication."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel


def resolve_review_index_root(value: Path | str) -> Path:
    """Resolve only documented task layouts; never search other tasks."""
    root = Path(value).expanduser().resolve()
    if root.is_file():
        root = root.parent
    from ..tasks.repository import TaskRepository
    for candidate in (root, root.parent if root.name.casefold() in {"raw", "final", "assets"} else root,
                      root.parent.parent if root.name.casefold() == "images" else root):
        if TaskRepository.is_authoritative(candidate):
            return candidate / "raw"
    candidates = [root, root / "raw"]
    if root.name.casefold() == "images" and root.parent.name.casefold() in {"raw", "final"}:
        candidates.append(root.parent.parent / "raw")
    elif root.name.casefold() == "final":
        candidates.append(root.parent / "raw")
    for candidate in candidates:
        if any((candidate / name).is_file() for name in ("image_index.json", "video_index.json")):
            return candidate
    raise ValueError("此目录没有图片审核索引。请选择任务结果目录或其中的 raw 目录；仅保留 final/images 的结果请直接打开图片文件夹人工选择。")


def rebase_asset_paths(value: Any, old_root: Path, new_root: Path) -> Any:
    """Rebase recorded paths under old_root, retaining relative manifest entries.

    Model, list, and dictionary containers are updated in place so candidate
    aliases in ``review_required`` retain their identity.
    """
    if isinstance(value, str):
        try:
            suffix = Path(value).relative_to(old_root)
        except (ValueError, OSError):
            return value
        if ".." in suffix.parts:
            return value
        return str(new_root / suffix)
    if isinstance(value, BaseModel):
        for name in type(value).model_fields:
            item = getattr(value, name)
            updated = rebase_asset_paths(item, old_root, new_root)
            if updated is not item:
                setattr(value, name, updated)
    elif isinstance(value, dict):
        for key, item in value.items():
            value[key] = rebase_asset_paths(item, old_root, new_root)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            value[index] = rebase_asset_paths(item, old_root, new_root)
    elif isinstance(value, tuple):
        return tuple(rebase_asset_paths(item, old_root, new_root) for item in value)
    return value


def recover_asset_path(
    value: Any, raw_root: Path | str, expected_sha256: str | None = None,
) -> Path | None:
    """Resolve a confined asset, recovering only same-task publish staging paths.

    Recovery never searches by basename or repairs a path from another task.
    Symlink escapes and lexical traversal are rejected before reading bytes.
    Hash validation applies to both current and recovered paths when supplied.
    Image decoding remains the caller's responsibility.
    """
    if not value:
        return None
    try:
        raw = Path(raw_root).expanduser().resolve()
        candidate = Path(str(value)).expanduser()
        if ".." in candidate.parts:
            return None
        if candidate.is_absolute():
            try:
                suffix = candidate.relative_to(raw.parent / ".work")
            except ValueError:
                path = candidate
            else:
                if raw.name != "raw" or len(suffix.parts) < 2:
                    return None
                if not re.fullmatch(r"publish-[A-Za-z0-9_-]+", suffix.parts[0]):
                    return None
                path = raw.joinpath(*suffix.parts[1:])
        else:
            # A historical relative path may include its task prefix. Require
            # its absolute lexical location to match this task's work folder.
            if any(part.casefold() == ".work" for part in candidate.parts):
                return recover_asset_path(candidate.absolute(), raw, expected_sha256)
            path = raw / candidate
        resolved = path.resolve()
        resolved.relative_to(raw)
        if not resolved.is_file():
            return None
        if expected_sha256:
            if not re.fullmatch(r"[0-9a-fA-F]{64}", str(expected_sha256)):
                return None
            if hashlib.sha256(resolved.read_bytes()).hexdigest() != str(expected_sha256).casefold():
                return None
        return resolved
    except (OSError, ValueError, RuntimeError):
        return None
