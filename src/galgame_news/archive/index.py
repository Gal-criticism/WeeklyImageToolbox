"""Rebuildable SQLite metadata index for weekly archive assets."""

from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from .models import ArchiveAsset


def normalize_issue_number(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    text = re.sub(r"[_\-\s]*(?:副本|copy|复制)(?:\s*\(\d+\))?$", "", text, flags=re.IGNORECASE).strip()
    match = re.fullmatch(r"(?:第\s*(\d+)\s*期|(\d+)\s*(?:周报|期)|(\d+))", text)
    if not match:
        return ""
    number = next(group for group in match.groups() if group is not None)
    return str(int(number))


def _norm_name(value: str) -> str:
    return "".join(ch for ch in unicodedata.normalize("NFKC", value).casefold() if ch.isalnum())


def _date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except (TypeError, ValueError):
        return None


def _issue_key(value: str) -> tuple[int, str]:
    normalized = normalize_issue_number(value)
    return (0, f"{int(normalized):012d}") if normalized else (1, "")


def _natural_name_key(value: str) -> tuple[tuple[int, object], ...]:
    return tuple((0, int(part)) if part.isdigit() else (1, part.casefold()) for part in re.split(r"(\d+)", value))


class ArchiveIndex:
    """A local, independently rebuildable index; all searches are offline."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self.path))
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("CREATE TABLE IF NOT EXISTS assets (asset_id TEXT NOT NULL, source_id TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(source_id,asset_id))")
        self._db.execute("CREATE INDEX IF NOT EXISTS assets_source_id ON assets(source_id)")
        self._db.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        self._db.commit()

    @property
    def last_updated(self) -> str | None:
        row = self._db.execute("SELECT value FROM metadata WHERE key='last_updated'").fetchone()
        return row[0] if row else None

    def replace_source(self, source_id: str, assets: Iterable[ArchiveAsset]) -> None:
        records = list(assets)
        if any(not isinstance(asset, ArchiveAsset) for asset in records):
            raise TypeError("assets must contain ArchiveAsset instances")
        if any(asset.source_id != source_id for asset in records):
            raise ValueError("asset source_id must match replacement source")
        dates_by_issue: dict[int, list[date]] = {}
        for asset in records:
            issue = normalize_issue_number(asset.issue_number)
            issue_date = _date(asset.issue_date)
            if issue and issue_date:
                dates_by_issue.setdefault(int(issue), []).append(issue_date)
        suspect_issues: set[int] = set()
        latest_prior_date: date | None = None
        for issue in sorted(dates_by_issue):
            issue_dates = dates_by_issue[issue]
            if latest_prior_date is not None and min(issue_dates) < latest_prior_date:
                suspect_issues.add(issue)
            latest_prior_date = max(latest_prior_date, max(issue_dates)) if latest_prior_date else max(issue_dates)
        normalized_records = []
        for asset in records:
            issue = normalize_issue_number(asset.issue_number)
            if issue and int(issue) in suspect_issues and not asset.date_suspect:
                asset = ArchiveAsset.from_dict({**asset.to_dict(), "date_suspect": True})
            normalized_records.append(asset)
        now = datetime.now(timezone.utc)
        encoded = [(asset.asset_id, source_id, json.dumps(asset.to_dict(), ensure_ascii=False)) for asset in normalized_records]
        stamp = now.isoformat()
        with self._db:
            self._db.execute("DELETE FROM assets WHERE source_id=?", (source_id,))
            self._db.executemany("INSERT INTO assets(asset_id,source_id,payload) VALUES(?,?,?)", encoded)
            self._db.execute("INSERT INTO metadata(key,value) VALUES('last_updated',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (stamp,))

    def list_assets(self, source_id: str | None = None) -> list[ArchiveAsset]:
        if source_id is None:
            rows = self._db.execute("SELECT payload FROM assets ORDER BY source_id,asset_id").fetchall()
        else:
            rows = self._db.execute("SELECT payload FROM assets WHERE source_id=? ORDER BY asset_id", (source_id,)).fetchall()
        return [ArchiveAsset.from_dict(json.loads(row[0])) for row in rows]

    def search(
        self,
        game_names: Iterable[str],
        current_issue: str | int,
        no_videos: bool = False,
        days: int | None = 365,
        now: datetime | None = None,
    ) -> list[ArchiveAsset]:
        raw_queries = [unicodedata.normalize("NFKC", name).casefold().strip() for name in game_names if _norm_name(name)]
        names = {_norm_name(name) for name in raw_queries if _norm_name(name)}
        names -= {"weeklyintro", "weeklyoutro"}
        if not names:
            return []
        current_number = normalize_issue_number(current_issue)
        if not current_number:
            return []
        current_key = _issue_key(current_number)
        today = (now or datetime.now(timezone.utc)).date()
        earliest = today - timedelta(days=max(0, days)) if days is not None else None
        assets = [
            asset for asset in self.list_assets()
            if asset.kind in {"image", "video"}
            and normalize_issue_number(asset.issue_number)
            and _issue_key(asset.issue_number) < current_key
            and _game_version_matches(asset.game_names, asset.version, raw_queries)
            and (earliest is None or asset.date_suspect or asset.issue_date is None
                 or _date(asset.issue_date) is None or _date(asset.issue_date) >= earliest)
        ]
        if not assets:
            return []
        issues = sorted({_issue_key(asset.issue_number) for asset in assets}, reverse=True)[:2]

        # Merge local copies only when they identify the same candidate in the same issue.
        deduped: dict[tuple[tuple[int, str], str, str], ArchiveAsset] = {}
        for asset in assets:
            issue = _issue_key(asset.issue_number)
            if issue not in issues:
                continue
            meta = asset.metadata or {}
            candidate_id = meta.get("candidate_id") or meta.get("source_candidate_id") or meta.get("original_candidate_id")
            identity = str(candidate_id) if candidate_id else f"{asset.source_id}:{asset.asset_id}"
            key = (issue, asset.kind, identity)
            previous = deduped.get(key)
            if previous is None or _duplicate_rank(asset) < _duplicate_rank(previous):
                deduped[key] = asset
        if no_videos:
            deduped = {key: asset for key, asset in deduped.items() if asset.kind != "video"}
        selected = []
        for issue in issues:
            issue_assets = [asset for (asset_issue, _kind, _identity), asset in deduped.items()
                            if asset_issue == issue]
            selected.extend(sorted(issue_assets, key=lambda asset: _selection_rank(asset)))
        return selected

    def close(self) -> None:
        self._db.close()


def _game_version_matches(game_names: Iterable[str], version: str | None, queries: Iterable[str]) -> bool:
    for game in game_names:
        normalized_game = _norm_name(game)
        if not normalized_game or normalized_game in {"weeklyintro", "weeklyoutro"}:
            continue
        for query in queries:
            normalized_query = _norm_name(query)
            if not version:
                if normalized_query == normalized_game:
                    return True
                continue
            normalized_version = unicodedata.normalize("NFKC", version).strip().casefold()
            escaped_version = re.escape(normalized_version)
            version_matches = bool(re.search(rf"(?<!\w){escaped_version}(?!\w)", query, flags=re.UNICODE))
            if version_matches and normalized_query.startswith(normalized_game):
                return True
    return False


def _duplicate_rank(asset: ArchiveAsset) -> tuple[int, int, str]:
    metadata = asset.metadata or {}
    selected = bool(metadata.get("selected"))
    local = asset.source_kind == "local"
    return (0 if selected else 1, 0 if local else 1, asset.asset_id)


def _selection_rank(asset: ArchiveAsset) -> tuple[int, tuple[tuple[int, object], ...], int, str]:
    selected = bool((asset.metadata or {}).get("selected"))
    return (0 if selected else 1, _natural_name_key(asset.name or asset.path), 0 if asset.source_kind == "local" else 1, asset.asset_id)
