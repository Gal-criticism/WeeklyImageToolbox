"""SQLite-backed durable state for one task directory."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, quote, urlsplit

_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()
_ASSET_KEYS = {"local_path", "original_path", "output_path", "path"}
_SECRET_KEY = re.compile(
    r"(api.?key|access.?token|password|passwd|secret|authorization|cookie|bearer)", re.I
)
_OPTIONS = {
    "offline",
    "no_videos",
    "max_images",
    "selected_sections",
    "llm_provider",
    "llm_model",
    "use_socialdata_x",
    "include_x",
    "include_h",
    "include_z",
}
_OPTIONS |= {"config_path", "history_db"}
_MAX_TASK_IMAGE_PIXELS = 50_000_000


def _json(value: Any) -> str:
    _check_secrets(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _check_secrets(value: Any, key: str = "") -> None:
    if isinstance(value, dict):
        for k, v in value.items():
            if (
                _SECRET_KEY.search(str(k))
                and isinstance(v, (str, dict, list, tuple))
                and v
            ):
                raise ValueError(
                    f"credential-like data cannot be stored in task state ({k})"
                )
            _check_secrets(v, f"{key}.{k}" if key else str(k))
    elif isinstance(value, (list, tuple)):
        for v in value:
            _check_secrets(v, key)
    elif isinstance(value, str):
        explicit = re.search(
            r"(?i)(bearer\s+\S+|(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|\d{5,}\|[A-Za-z0-9]{20,}))",
            value,
        )
        sensitive_query = False
        if "://" in value:
            try:
                sensitive_query = any(
                    re.search(
                        r"(?i)(token|key|signature|credential|secret|auth|password)",
                        name,
                    )
                    and item
                    for name, item in parse_qsl(
                        urlsplit(value).query, keep_blank_values=True
                    )
                )
            except ValueError:
                pass
        if explicit or sensitive_query:
            raise ValueError(
                f"credential-like data cannot be stored in task state ({key or 'string value'})"
            )


class TaskRepository:
    def __init__(self, root: Path | str, create: bool = True):
        self.root = Path(root).expanduser().resolve()
        self.path = self.root / "task_state.sqlite3"
        if create:
            self.root.mkdir(parents=True, exist_ok=True)
            with self._connection() as db:
                with self._lock(self.path):
                    self._schema(db)
        elif not self.path.is_file():
            raise FileNotFoundError(self.path)
        else:
            with self._connection() as db:
                tables = {
                    r[0]
                    for r in db.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                if "task_meta" in tables:
                    version = db.execute(
                        "SELECT value FROM task_meta WHERE key='task_schema_version'"
                    ).fetchone()
                    if version and int(json.loads(version[0])) > 3:
                        raise ValueError(
                            "task state database schema is newer than this application"
                        )

    @staticmethod
    def _lock(path: Path):
        key = os.path.normcase(str(path.resolve()))
        with _LOCKS_GUARD:
            return _LOCKS.setdefault(key, threading.RLock())

    @contextmanager
    def _connection(self):
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA busy_timeout=5000")
            db.execute("PRAGMA journal_mode=DELETE")
            db.execute("PRAGMA synchronous=FULL")
            yield db
        except Exception:
            if db.in_transaction:
                db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _schema(db):
        tables = {
            r[0]
            for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "task_meta" in tables:
            version = db.execute(
                "SELECT value FROM task_meta WHERE key='task_schema_version'"
            ).fetchone()
            if version and int(json.loads(version[0])) > 3:
                raise ValueError(
                    "task state database schema is newer than this application"
                )
        db.executescript(
            """
        BEGIN IMMEDIATE;
        CREATE TABLE IF NOT EXISTS task_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS retry_attempts(news_id TEXT NOT NULL,attempt_id TEXT NOT NULL,path TEXT NOT NULL,created_at TEXT NOT NULL DEFAULT '',kind TEXT NOT NULL DEFAULT 'retry',PRIMARY KEY(news_id,attempt_id));
        CREATE TABLE IF NOT EXISTS review_decisions(media_id TEXT PRIMARY KEY,decision TEXT NOT NULL,updated_at TEXT NOT NULL DEFAULT '');
        CREATE TABLE IF NOT EXISTS news(id TEXT PRIMARY KEY,payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sources(id TEXT PRIMARY KEY,payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS candidates(id TEXT PRIMARY KEY,news_id TEXT,section TEXT,kind TEXT NOT NULL,payload TEXT NOT NULL,ordinal INTEGER NOT NULL DEFAULT 0);
        CREATE INDEX IF NOT EXISTS candidate_query ON candidates(news_id,section,kind);
        CREATE TABLE IF NOT EXISTS failures(id TEXT PRIMARY KEY,news_id TEXT,payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS assets(id TEXT PRIMARY KEY,kind TEXT,payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS candidate_assets(candidate_id TEXT NOT NULL,role TEXT NOT NULL,asset_id TEXT NOT NULL,PRIMARY KEY(candidate_id,role),FOREIGN KEY(candidate_id) REFERENCES candidates(id),FOREIGN KEY(asset_id) REFERENCES assets(id));
        CREATE TABLE IF NOT EXISTS progress(id TEXT PRIMARY KEY,payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS post_cache(post_id TEXT NOT NULL,identity TEXT NOT NULL,state TEXT NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(post_id,identity));
        CREATE TABLE IF NOT EXISTS batches(news_id TEXT NOT NULL,attempt_id TEXT NOT NULL,path TEXT NOT NULL,kind TEXT NOT NULL,status TEXT NOT NULL,images TEXT NOT NULL,videos TEXT NOT NULL,failures TEXT NOT NULL,PRIMARY KEY(news_id,attempt_id));
        CREATE TABLE IF NOT EXISTS checkpoint(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS options(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS journal_news(news_id TEXT NOT NULL,identity TEXT NOT NULL,collected INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(news_id,identity));
        CREATE TABLE IF NOT EXISTS review_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS asset_intents(candidate_id TEXT PRIMARY KEY,payload TEXT NOT NULL);
        """
        )
        cols = {r[1] for r in db.execute("PRAGMA table_info(candidates)")}
        if "ordinal" not in cols:
            db.execute(
                "ALTER TABLE candidates ADD COLUMN ordinal INTEGER NOT NULL DEFAULT 0"
            )
        cols = {r[1] for r in db.execute("PRAGMA table_info(review_decisions)")}
        if "manual" not in cols:
            db.execute(
                "ALTER TABLE review_decisions ADD COLUMN manual INTEGER NOT NULL DEFAULT 0"
            )
        db.execute(
            "INSERT INTO task_meta(key,value) VALUES('task_schema_version','3') ON CONFLICT(key) DO UPDATE SET value=excluded.value"
        )
        db.commit()

    @staticmethod
    def is_authoritative(root):
        path = Path(root) / "task_state.sqlite3"
        if not path.is_file():
            return False
        db = None
        try:
            db = sqlite3.connect(
                f"file:{quote(path.resolve().as_posix(),safe='/:')}?mode=ro",
                uri=True,
                timeout=1,
            )
            row = db.execute(
                "SELECT value FROM task_meta WHERE key='storage_backend'"
            ).fetchone()
            return bool(row and row[0] in ('"sqlite"', "sqlite"))
        except sqlite3.Error as exc:
            if isinstance(exc, sqlite3.OperationalError) and any(
                value in str(exc).casefold() for value in ("locked", "busy")
            ):
                # A temporary lock is recoverable, not database corruption.
                raise
            raise ValueError(
                f"task state database is corrupt or incompatible: {path}"
            ) from exc
        finally:
            if db is not None:
                db.close()

    def activate(self):
        self.set_meta("storage_backend", "sqlite")

    def get_meta(self, key, default=None):
        with self._lock(self.path), self._connection() as db:
            row = db.execute(
                "SELECT value FROM task_meta WHERE key=?", (key,)
            ).fetchone()
            if row is None:
                return default
            try:
                return json.loads(row[0])
            except (ValueError, TypeError):
                return row[0]

    def set_meta(self, key, value):
        encoded = _json(value)
        with self._lock(self.path), self._connection() as db:
            db.execute(
                "INSERT INTO task_meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, encoded),
            )

    def prepare_asset(self, candidate_id, metadata):
        """Commit expected original asset metadata before its file is landed."""
        allowed = {
            "original_path",
            "original_sha256",
            "original_mime_type",
            "original_width",
            "original_height",
            "original_byte_size",
            "downloaded_url",
        }
        intent = {k: metadata[k] for k in allowed if k in metadata}
        if not intent.get("original_path") or not re.fullmatch(
            r"[0-9a-fA-F]{64}", str(intent.get("original_sha256", ""))
        ):
            raise ValueError("asset intent requires an original path and SHA-256")
        intent = self._paths(intent)
        with self._lock(self.path), self._connection() as db:
            db.execute(
                "INSERT INTO asset_intents VALUES(?,?) ON CONFLICT(candidate_id) DO UPDATE SET payload=excluded.payload WHERE asset_intents.payload<>excluded.payload",
                (str(candidate_id), _json(intent)),
            )

    @staticmethod
    def _decode_image(path):
        from PIL import Image

        with Image.open(path) as image:
            if image.width * image.height > _MAX_TASK_IMAGE_PIXELS:
                raise ValueError("image exceeds task decode limit")
            image.verify()
        with Image.open(path) as image:
            if image.width * image.height > _MAX_TASK_IMAGE_PIXELS:
                raise ValueError("image exceeds task decode limit")
            image.load()

    def recover_asset_intents(self):
        """Adopt already-landed originals after a crash, without network access."""
        recovered = 0
        with self._lock(self.path), self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            intents = db.execute(
                "SELECT candidate_id,payload FROM asset_intents ORDER BY candidate_id"
            ).fetchall()
            for intent_row in intents:
                candidate_id = intent_row["candidate_id"]
                row = db.execute(
                    "SELECT payload FROM candidates WHERE id=?", (candidate_id,)
                ).fetchone()
                if not row:
                    continue
                candidate = json.loads(row[0])
                metadata = self._paths(json.loads(intent_row["payload"]), True)
                path = Path(metadata["original_path"]).resolve()
                if not path.is_relative_to(self.root) or not path.is_file():
                    continue
                digest = hashlib.sha256()
                with path.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
                if (
                    digest.hexdigest().casefold()
                    != str(metadata["original_sha256"]).casefold()
                ):
                    continue
                try:
                    self._decode_image(path)
                except Exception:
                    continue
                candidate.update(
                    {k: v for k, v in metadata.items() if k != "original_path"}
                )
                candidate["original_path"] = str(path)
                current_local = candidate.get("local_path")
                preserve_review = bool(
                    current_local
                    and Path(current_local).is_file()
                    and Path(current_local).resolve() != path
                )
                if not preserve_review:
                    candidate["local_path"] = str(path)
                candidate["download_status"] = "downloaded"
                candidate["download_error_code"] = None
                candidate["downloadable"] = True
                if not preserve_review:
                    candidate["curation_status"] = "unselected"
                signals = candidate.get("signals")
                if isinstance(signals, dict):
                    for key in (
                        "invalid_reason",
                        "download_error",
                        "download_failure",
                        "download_failed",
                    ):
                        signals.pop(key, None)
                db.execute(
                    "UPDATE candidates SET payload=? WHERE id=?",
                    (_json(self._paths(candidate)), candidate_id),
                )
                self._record_candidate_assets(db, candidate_id, self._paths(candidate))
                db.execute(
                    "DELETE FROM asset_intents WHERE candidate_id=?", (candidate_id,)
                )
                recovered += 1
            db.commit()
        return recovered

    def _paths(self, value, restore=False):
        if isinstance(value, dict):
            out = {}
            for k, v in value.items():
                if (k in _ASSET_KEYS or k == "output_dir") and isinstance(v, str) and v:
                    p = Path(v)
                    if p.is_absolute():
                        try:
                            v = str(p.resolve().relative_to(self.root))
                        except ValueError:
                            raise ValueError("asset path escapes task root")
                    elif not (self.root / p).resolve().is_relative_to(self.root):
                        raise ValueError("asset path escapes task root")
                    if restore:
                        q = (self.root / p).resolve()
                        if not q.is_relative_to(self.root):
                            raise ValueError("asset path escapes task root")
                        v = str(q)
                out[k] = self._paths(v, restore)
            return out
        if isinstance(value, list):
            return [self._paths(v, restore) for v in value]
        return value

    def save_checkpoint(self, payload):
        if not isinstance(payload, dict):
            raise TypeError("checkpoint must be a dict")
        data = {
            k: v
            for k, v in payload.items()
            if k not in {"candidates", "videos", "failures", "source_map"}
        }
        issue = data.get("issue")
        if isinstance(issue, dict):
            issue = dict(issue)
            news = issue.pop("news_items", issue.pop("news", []))
            data["issue"] = issue
        else:
            news = []
        result = data.get("result")
        memberships = {}
        if isinstance(result, dict):
            result = dict(result)
            memberships = {
                k: [str(x.get("id")) for x in result.get(k, []) if isinstance(x, dict)]
                for k in (
                    "candidates",
                    "filtered_candidates",
                    "review_required",
                    "videos",
                )
            }
            result = {
                k: v
                for k, v in result.items()
                if k
                not in {
                    "issue",
                    "candidates",
                    "filtered_candidates",
                    "review_required",
                    "videos",
                    "failures",
                }
            }
            result["issue_ref"] = True
            data["result"] = result
        data["_membership"] = memberships
        data = self._paths(data)
        with self._lock(self.path), self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            encoded = _json(data)
            db.execute(
                "INSERT INTO checkpoint VALUES('payload',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value WHERE checkpoint.value<>excluded.value",
                (encoded,),
            )
            if payload.get("issue_id"):
                db.execute(
                    "INSERT INTO task_meta VALUES('issue_id',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (_json(payload["issue_id"]),),
                )
            for n, item in enumerate(news):
                ident = str(item.get("id") or item.get("news_id") or n)
                old = db.execute(
                    "SELECT payload FROM news WHERE id=?", (ident,)
                ).fetchone()
                merged = {**(json.loads(old[0]) if old else {}), **item}
                db.execute(
                    "INSERT INTO news VALUES(?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload WHERE news.payload<>excluded.payload",
                    (ident, _json(merged)),
                )
            for sk, sv in payload.get("source_map", {}).items():
                db.execute(
                    "INSERT INTO sources VALUES(?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload WHERE sources.payload<>excluded.payload",
                    (str(sk), _json(sv)),
                )
            for table, rows, kind in (
                ("candidates", payload.get("candidates", []), "image"),
                ("candidates", payload.get("videos", []), "video"),
                ("failures", payload.get("failures", []), None),
            ):
                for ordinal, row in enumerate(rows if isinstance(rows, list) else []):
                    if not isinstance(row, dict):
                        continue
                    if table == "candidates":
                        ident = str(
                            row.get("id")
                            or row.get("candidate_id")
                            or hashlib.sha256(_json(row).encode()).hexdigest()
                        )
                        normalized = self._paths(row)
                        db.execute(
                            "INSERT INTO candidates(id,news_id,section,kind,payload,ordinal) VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET news_id=excluded.news_id,section=excluded.section,kind=excluded.kind,payload=excluded.payload,ordinal=excluded.ordinal WHERE candidates.payload<>excluded.payload OR candidates.kind<>excluded.kind OR candidates.ordinal<>excluded.ordinal",
                            (
                                ident,
                                row.get("news_id"),
                                row.get("section"),
                                kind,
                                _json(normalized),
                                ordinal,
                            ),
                        )
                        self._record_candidate_assets(db, ident, normalized)
                    else:
                        ident = hashlib.sha256(
                            _json(
                                [
                                    row.get(k)
                                    for k in (
                                        "stage",
                                        "news_id",
                                        "candidate_id",
                                        "code",
                                        "message",
                                        "source_url",
                                    )
                                ]
                            ).encode()
                        ).hexdigest()
                        db.execute(
                            "INSERT INTO failures VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload WHERE failures.payload<>excluded.payload",
                            (ident, row.get("news_id"), _json(self._paths(row))),
                        )
            if isinstance(payload.get("result"), dict):
                result = payload["result"]
                for group, kind in (
                    ("filtered_candidates", "image"),
                    ("review_required", "image"),
                ):
                    for n, row in enumerate(result.get(group, [])):
                        if isinstance(row, dict):
                            ident = str(
                                row.get("id")
                                or row.get("candidate_id")
                                or hashlib.sha256(_json(row).encode()).hexdigest()
                            )
                            if not db.execute(
                                "SELECT 1 FROM candidates WHERE id=?", (ident,)
                            ).fetchone():
                                self._upsert_candidate(
                                    db, self._paths(row), kind, ordinal=n
                                )
                for row in result.get("failures", []):
                    ident = hashlib.sha256(
                        _json(
                            [
                                row.get(k)
                                for k in (
                                    "stage",
                                    "news_id",
                                    "candidate_id",
                                    "code",
                                    "message",
                                    "source_url",
                                )
                            ]
                        ).encode()
                    ).hexdigest()
                    db.execute(
                        "INSERT INTO failures VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload WHERE failures.payload<>excluded.payload",
                        (ident, row.get("news_id"), _json(self._paths(row))),
                    )
            db.commit()

    def save_index(self, image_payload, video_payload, reviews=None, failures=None):
        """Commit index state without changing runner checkpoint identity/status."""
        image_payload = image_payload or {}
        video_payload = video_payload or {}

        def rows(payload, keys):
            if isinstance(payload, list):
                return payload
            if isinstance(payload, dict):
                return next(
                    (payload[k] for k in keys if isinstance(payload.get(k), list)), []
                )
            return []

        image_rows = rows(image_payload, ("candidates", "images", "image_candidates"))
        video_rows = rows(video_payload, ("videos", "video_candidates", "candidates"))
        news_rows = rows(image_payload, ("news_items", "items")) or rows(
            video_payload, ("news_items", "items")
        )
        failures = (
            failures
            if failures is not None
            else rows(image_payload, ("failures",)) + rows(video_payload, ("failures",))
        )
        review_ids = set()
        if isinstance(reviews, dict):
            review_ids = {
                str(x.get("id")) if isinstance(x, dict) else str(x)
                for x in reviews.get("candidates", [])
            }
        elif isinstance(reviews, list):
            review_ids = {
                str(x.get("id")) if isinstance(x, dict) else str(x) for x in reviews
            }
        with self._lock(self.path), self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            for payload in (image_payload, video_payload):
                if isinstance(payload, dict) and payload.get("issue_id"):
                    db.execute(
                        "INSERT INTO task_meta VALUES('issue_id',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (_json(payload["issue_id"]),),
                    )
            for item in news_rows:
                if not isinstance(item, dict):
                    continue
                ident = str(item.get("news_id") or item.get("id") or "")
                if ident:
                    old = db.execute(
                        "SELECT payload FROM news WHERE id=?", (ident,)
                    ).fetchone()
                    merged = json.loads(old[0]) if old else {}
                    for key in ("id", "news_id", "sequence", "section", "title"):
                        if key in item:
                            merged[key] = item[key]
                    db.execute(
                        "INSERT INTO news VALUES(?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload WHERE news.payload<>excluded.payload",
                        (ident, _json(merged)),
                    )
            news_sections = {
                str(item.get("news_id") or item.get("id")): item.get("section")
                for item in news_rows
                if isinstance(item, dict)
            }
            for kind, records in (("image", image_rows), ("video", video_rows)):
                for ordinal, row in enumerate(records):
                    if not isinstance(row, dict):
                        continue
                    candidate = dict(row)
                    section = news_sections.get(str(candidate.get("news_id")))
                    candidate.pop("section", None)
                    self._upsert_candidate(
                        db,
                        self._paths(candidate),
                        kind,
                        ordinal=ordinal,
                        section=section,
                    )
            for row in failures:
                if not isinstance(row, dict):
                    continue
                ident = hashlib.sha256(
                    _json(
                        [
                            row.get(k)
                            for k in (
                                "stage",
                                "news_id",
                                "candidate_id",
                                "code",
                                "message",
                                "source_url",
                            )
                        ]
                    ).encode()
                ).hexdigest()
                db.execute(
                    "INSERT INTO failures VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload WHERE failures.payload<>excluded.payload",
                    (ident, row.get("news_id"), _json(self._paths(row))),
                )
            db.execute(
                "INSERT INTO review_meta VALUES('review_ids',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (_json(sorted(review_ids)),),
            )
            db.commit()
        if isinstance(reviews, dict) and "decisions" in reviews:
            self.save_review_state(reviews)

    def load_checkpoint(self):
        with self._lock(self.path), self._connection() as db:
            row = db.execute(
                "SELECT value FROM checkpoint WHERE key='payload'"
            ).fetchone()
            if not row:
                return None
            data = json.loads(row[0])
            membership = data.pop("_membership", {})
            data["issue"]["news_items"] = [
                {
                    k: v
                    for k, v in json.loads(r[0]).items()
                    if k not in {"news_id", "status", "candidates", "videos"}
                }
                for r in db.execute("SELECT payload FROM news ORDER BY rowid")
            ]
            vals = {
                k: [
                    json.loads(r[0])
                    for r in db.execute(
                        "SELECT payload FROM candidates WHERE kind=? ORDER BY ordinal,id",
                        (kind,),
                    )
                ]
                for k, kind in (
                    ("candidates", "image"),
                    ("filtered_candidates", "image"),
                    ("review_required", "image"),
                    ("videos", "video"),
                )
            }
            data["candidates"] = vals["candidates"]
            data["videos"] = vals["videos"]
            data["failures"] = [
                json.loads(r[0])
                for r in db.execute("SELECT payload FROM failures ORDER BY rowid")
            ]
            data["source_map"] = {
                r[0]: json.loads(r[1])
                for r in db.execute("SELECT id,payload FROM sources")
            }
            if data.get("result"):
                result = data["result"]
                result.pop("issue_ref", None)
                result["issue"] = data["issue"]
                for key in (
                    "candidates",
                    "filtered_candidates",
                    "review_required",
                    "videos",
                ):
                    byid = {x.get("id"): x for x in vals[key]}
                    result[key] = [
                        byid[i] for i in membership.get(key, []) if i in byid
                    ]
                result["failures"] = data["failures"]
            return self._paths(data, True)

    def upsert_candidates(self, rows, kind="image", news_id=None):
        with self._lock(self.path), self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            base = db.execute(
                "SELECT COALESCE(MAX(ordinal),-1)+1 FROM candidates WHERE kind=?",
                (kind,),
            ).fetchone()[0]
            for n, row in enumerate(rows):
                self._upsert_candidate(db, self._paths(row), kind, news_id, base + n)
            db.commit()

    def query_candidates(
        self, news_id=None, section=None, kind="image", offset=0, limit=100
    ):
        safe_limit = max(0, min(100, int(limit)))
        safe_offset = max(0, int(offset))
        sql = """SELECT c.payload FROM candidates c LEFT JOIN news n ON n.id=c.news_id
               WHERE c.kind=?"""
        args = [kind]
        if news_id is not None:
            sql += " AND c.news_id=?"
            args.append(news_id)
        if section is not None:
            sql += " AND COALESCE(c.section,json_extract(n.payload,'$.section'))=?"
            args.append(section)
        sql += " ORDER BY c.ordinal,c.id LIMIT ? OFFSET ?"
        args.extend((safe_limit, safe_offset))
        with self._lock(self.path), self._connection() as db:
            return [self._paths(json.loads(r[0]), True) for r in db.execute(sql, args)]

    def iter_candidates(self, news_id=None, section=None, kind="image", batch_size=100):
        offset = 0
        while True:
            rows = self.query_candidates(
                news_id, section, kind, offset, min(100, batch_size)
            )
            if not rows:
                return
            yield from rows
            offset += len(rows)

    def iter_candidate_ids(self, news_id=None, kind="image", batch_size=100):
        """Read only membership IDs for an already loaded review snapshot."""
        limit = max(1, min(100, int(batch_size)))
        offset = 0
        while True:
            sql = "SELECT id FROM candidates WHERE kind=?"
            parameters = [kind]
            if news_id is not None:
                sql += " AND news_id=?"
                parameters.append(news_id)
            sql += " ORDER BY ordinal,id LIMIT ? OFFSET ?"
            parameters.extend((limit, offset))
            with self._lock(self.path), self._connection() as db:
                rows = [row[0] for row in db.execute(sql, parameters)]
            if not rows:
                return
            yield from rows
            offset += len(rows)

    def save_review_state(self, payload):
        revision = int(payload.get("revision", 0))
        if revision < 0:
            raise ValueError("review revision must be nonnegative")
        decisions = payload.get("decisions", {})
        manual_ids = set(map(str, payload.get("manual_decisions", [])))
        normalized = {}
        for media_id, decision in decisions.items():
            val = decision.get("decision") if isinstance(decision, dict) else decision
            if not isinstance(val, str) or val not in {
                "accepted",
                "rejected",
                "pending",
            }:
                raise ValueError(f"invalid review decision for {media_id}")
            manual = (
                bool(decision.get("manual", False))
                if isinstance(decision, dict)
                else str(media_id) in manual_ids
            )
            normalized[str(media_id)] = (str(val), int(manual))
        with self._lock(self.path), self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute(
                "SELECT value FROM review_meta WHERE key='revision'"
            ).fetchone()
            if old and revision < int(json.loads(old[0])):
                db.rollback()
                raise ValueError("stale review revision")
            if old and revision == int(json.loads(old[0])):
                current = {
                    r[0]: (r[1], r[2])
                    for r in db.execute(
                        "SELECT media_id,decision,manual FROM review_decisions"
                    )
                }
                if current != normalized:
                    db.rollback()
                    raise ValueError("conflicting review state at the same revision")
            keep_ids = set(normalized)
            if keep_ids:
                marks = ",".join("?" for _ in keep_ids)
                db.execute(
                    f"DELETE FROM review_decisions WHERE media_id NOT IN ({marks})",
                    tuple(keep_ids),
                )
            else:
                db.execute("DELETE FROM review_decisions")
            for media_id, decision in decisions.items():
                val, manual = normalized[str(media_id)]
                db.execute(
                    "INSERT INTO review_decisions(media_id,decision,updated_at,manual) VALUES(?,?,?,?) ON CONFLICT(media_id) DO UPDATE SET decision=excluded.decision,updated_at=excluded.updated_at,manual=excluded.manual WHERE review_decisions.decision<>excluded.decision OR review_decisions.manual<>excluded.manual",
                    (
                        str(media_id),
                        str(val),
                        str(payload.get("updated_at", "")),
                        int(manual),
                    ),
                )
            db.execute(
                "INSERT INTO review_meta VALUES('revision',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (json.dumps(revision),),
            )
            extras = {
                k: v
                for k, v in payload.items()
                if k not in {"decisions", "manual_decisions", "revision"}
            }
            db.execute(
                "INSERT INTO review_meta VALUES('extras',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value WHERE review_meta.value<>excluded.value",
                (_json(extras),),
            )
            db.commit()

    def load_review_state(self):
        with self._lock(self.path), self._connection() as db:
            revision = db.execute(
                "SELECT value FROM review_meta WHERE key='revision'"
            ).fetchone()
            if revision:
                extras = db.execute(
                    "SELECT value FROM review_meta WHERE key='extras'"
                ).fetchone()
                result = json.loads(extras[0]) if extras else {}
                result.update(revision=int(json.loads(revision[0])))
                result["decisions"] = {
                    r[0]: r[1]
                    for r in db.execute(
                        "SELECT media_id,decision FROM review_decisions"
                    )
                }
                result["manual_decisions"] = [
                    r[0]
                    for r in db.execute(
                        "SELECT media_id FROM review_decisions WHERE manual=1"
                    )
                ]
                return result
            legacy = db.execute(
                "SELECT value FROM review_meta WHERE key='payload'"
            ).fetchone()
            if legacy:
                return json.loads(legacy[0])
            return self.get_meta("review_state")

    def save_options(self, payload):
        safe = {k: v for k, v in payload.items() if k in _OPTIONS}
        _check_secrets(safe)
        with self._lock(self.path), self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            for k, v in safe.items():
                db.execute(
                    "INSERT INTO options VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (k, _json(v)),
                )
            db.commit()

    def load_options(self):
        with self._lock(self.path), self._connection() as db:
            return {
                r[0]: json.loads(r[1])
                for r in db.execute("SELECT key,value FROM options")
            }

    def save_post(self, post_id, state, payload, identity):
        identity = _json(identity) if isinstance(identity, dict) else str(identity)
        with self._lock(self.path), self._connection() as db:
            db.execute(
                "INSERT INTO post_cache VALUES(?,?,?,?) ON CONFLICT(post_id,identity) DO UPDATE SET state=excluded.state,payload=excluded.payload WHERE post_cache.state<>excluded.state OR post_cache.payload<>excluded.payload",
                (post_id, identity, state, _json(self._paths(payload))),
            )

    def load_posts(self, identity):
        identity = _json(identity) if isinstance(identity, dict) else str(identity)
        with self._lock(self.path), self._connection() as db:
            return {
                r[0]: {"state": r[1], "payload": self._paths(json.loads(r[2]), True)}
                for r in db.execute(
                    "SELECT post_id,state,payload FROM post_cache WHERE identity=?",
                    (identity,),
                )
            }

    def save_journal(self, news_id, rows, identity):
        identity = _json(identity) if isinstance(identity, dict) else str(identity)
        with self._lock(self.path), self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "INSERT INTO journal_news VALUES(?,?,1) ON CONFLICT(news_id,identity) DO UPDATE SET collected=1",
                (news_id, identity),
            )
            for row in rows:
                if isinstance(row, dict) and row.get("id"):
                    kind = "video" if "video_url" in row else "image"
                    self._upsert_candidate(db, self._paths(row), kind, news_id)
                    if row.get("download_status") == "downloaded":
                        db.execute(
                            "DELETE FROM asset_intents WHERE candidate_id=?",
                            (str(row["id"]),),
                        )
            db.commit()

    def load_journal(self, identity):
        with self._lock(self.path), self._connection() as db:
            identity = _json(identity) if isinstance(identity, dict) else str(identity)
            ids = {
                r[0]
                for r in db.execute(
                    "SELECT news_id FROM journal_news WHERE identity=? AND collected=1",
                    (identity,),
                )
            }
            rows = [
                self._paths(json.loads(r[0]), True)
                for r in db.execute(
                    "SELECT payload FROM candidates WHERE kind='image' AND news_id IN (SELECT news_id FROM journal_news WHERE identity=? AND collected=1) ORDER BY ordinal,id",
                    (identity,),
                )
            ]
        return (rows, ids)

    def _upsert_candidate(
        self, db, row, kind, news_id=None, ordinal=None, section=None
    ):
        ident = str(
            row.get("id")
            or row.get("candidate_id")
            or hashlib.sha256(_json(row).encode()).hexdigest()
        )
        # Empty/new batch copies must not overwrite an asset already on disk.
        payload = _json(row)
        old = db.execute(
            "SELECT payload FROM candidates WHERE id=?", (ident,)
        ).fetchone()
        if old:
            previous = json.loads(old[0])
            for key in ("local_path", "original_path", "output_path"):
                if previous.get(key) and not row.get(key):
                    row = dict(row, **{key: previous[key]})
            payload = _json(row)
        order = 0 if ordinal is None else ordinal
        section_value = row.get("section") if section is None else section
        db.execute(
            "INSERT INTO candidates(id,news_id,section,kind,payload,ordinal) VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET news_id=excluded.news_id,section=excluded.section,kind=excluded.kind,payload=excluded.payload,ordinal=CASE WHEN ? IS NULL THEN candidates.ordinal ELSE excluded.ordinal END WHERE candidates.payload<>excluded.payload OR candidates.kind<>excluded.kind OR candidates.section IS NOT excluded.section OR (? IS NOT NULL AND candidates.ordinal<>excluded.ordinal)",
            (
                ident,
                row.get("news_id", news_id),
                section_value,
                kind,
                payload,
                order,
                ordinal,
                ordinal,
            ),
        )
        self._record_candidate_assets(db, ident, row)

    def _record_candidate_assets(self, db, candidate_id, row):
        specs = (
            (
                "original",
                "original_path",
                "original_sha256",
                "original_mime_type",
                "original_width",
                "original_height",
                "original_byte_size",
                "",
            ),
            (
                "local",
                "local_path",
                "output_sha256" if row.get("output_sha256") else "sha256",
                "output_mime_type" if row.get("output_mime_type") else "mime_type",
                "output_width" if row.get("output_width") else "width",
                "output_height" if row.get("output_height") else "height",
                "output_byte_size" if row.get("output_byte_size") else "byte_size",
                "",
            ),
            (
                "output",
                "output_path",
                "output_sha256",
                "output_mime_type",
                "output_width",
                "output_height",
                "output_byte_size",
                str(row.get("conversion_profile") or ""),
            ),
        )
        for (
            role,
            path_key,
            hash_key,
            mime_key,
            width_key,
            height_key,
            size_key,
            profile,
        ) in specs:
            value = row.get(path_key)
            if not value:
                db.execute(
                    "DELETE FROM candidate_assets WHERE candidate_id=? AND role=?",
                    (candidate_id, role),
                )
                continue
            path = (self.root / value).resolve()
            if not path.is_relative_to(self.root) or not path.is_file():
                db.execute(
                    "DELETE FROM candidate_assets WHERE candidate_id=? AND role=?",
                    (candidate_id, role),
                )
                continue
            stat = path.stat()
            cached = db.execute(
                "SELECT a.id,a.payload FROM candidate_assets c JOIN assets a ON a.id=c.asset_id WHERE c.candidate_id=? AND c.role=?",
                (candidate_id, role),
            ).fetchone()
            metadata = json.loads(cached["payload"]) if cached else {}
            if (
                metadata.get("path") == str(value)
                and metadata.get("size") == stat.st_size
                and metadata.get("mtime_ns") == stat.st_mtime_ns
            ):
                digest = cached["id"]
                width, height = metadata.get("width"), metadata.get("height")
                decodable = metadata.get("decodable")
            else:
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                width, height, decodable = None, None, None
                if path.suffix.casefold() in {
                    ".png",
                    ".jpg",
                    ".jpeg",
                    ".gif",
                    ".webp",
                    ".bmp",
                }:
                    try:
                        from PIL import Image

                        with Image.open(path) as image:
                            width, height = image.size
                            image.verify()
                            decodable = True
                    except Exception:
                        decodable = False
            data = {
                "path": str(value),
                "sha256": digest,
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
                "mime_type": row.get(mime_key),
                "width": row.get(width_key) or width,
                "height": row.get(height_key) or height,
                "profile": profile,
                "decodable": decodable,
            }
            db.execute(
                "INSERT INTO assets(id,kind,payload) VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET kind=excluded.kind,payload=excluded.payload WHERE assets.payload<>excluded.payload",
                (digest, role, _json(data)),
            )
            db.execute(
                "INSERT INTO candidate_assets(candidate_id,role,asset_id) VALUES(?,?,?) ON CONFLICT(candidate_id,role) DO UPDATE SET asset_id=excluded.asset_id",
                (candidate_id, role, digest),
            )

    def save_batch(
        self,
        news_id,
        attempt_id,
        path,
        kind,
        status,
        images,
        videos,
        failures,
        source_url=None,
        sources=None,
    ):
        with self._lock(self.path), self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            for row in [*(images or []), *(videos or [])]:
                if row.get("news_id") is not None and str(row["news_id"]) != str(
                    news_id
                ):
                    raise ValueError("batch candidate news id mismatch")
            stored_path = self._paths({"path": str(path)})["path"]
            image_ids = []
            video_ids = []
            for i, row in enumerate(images or []):
                self._upsert_candidate(db, self._paths(row), "image", news_id, i)
                image_ids.append(str(row.get("id") or row.get("candidate_id")))
            for i, row in enumerate(videos or []):
                self._upsert_candidate(db, self._paths(row), "video", news_id, i)
                video_ids.append(str(row.get("id") or row.get("candidate_id")))
            fail_ids = []
            for row in failures or []:
                ident = hashlib.sha256(
                    _json(
                        [
                            row.get(k)
                            for k in (
                                "stage",
                                "news_id",
                                "candidate_id",
                                "code",
                                "message",
                                "source_url",
                            )
                        ]
                    ).encode()
                ).hexdigest()
                db.execute(
                    "INSERT INTO failures VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload WHERE failures.payload<>excluded.payload",
                    (ident, row.get("news_id", news_id), _json(self._paths(row))),
                )
                fail_ids.append(ident)
            meta = {
                "image_ids": image_ids,
                "video_ids": video_ids,
                "failure_ids": fail_ids,
                "source_url": source_url,
                "sources": sources or [],
            }
            db.execute(
                "INSERT INTO batches VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(news_id,attempt_id) DO UPDATE SET path=excluded.path,kind=excluded.kind,status=excluded.status,images=excluded.images,videos=excluded.videos,failures=excluded.failures",
                (
                    news_id,
                    attempt_id,
                    stored_path,
                    kind,
                    status,
                    _json(meta["image_ids"]),
                    _json(meta["video_ids"]),
                    _json(meta),
                ),
            )
            db.execute(
                "INSERT INTO retry_attempts(news_id,attempt_id,path,kind) VALUES(?,?,?,?) ON CONFLICT(news_id,attempt_id) DO UPDATE SET path=excluded.path,kind=excluded.kind",
                (news_id, attempt_id, stored_path, kind),
            )
            db.commit()

    def read_batch(self, news_id, attempt_id):
        with self._lock(self.path), self._connection() as db:
            r = db.execute(
                "SELECT * FROM batches WHERE news_id=? AND attempt_id=?",
                (news_id, attempt_id),
            ).fetchone()
            return self._batch_dict(db, r) if r else None

    def list_batches(self, news_id=None):
        with self._lock(self.path), self._connection() as db:
            q = "SELECT * FROM batches"
            args = ()
            if news_id is not None:
                q += " WHERE news_id=?"
                args = (news_id,)
            return [self._batch_dict(db, r) for r in db.execute(q, args)]

    def _batch_dict(self, db, r):
        meta = json.loads(r["failures"])
        get = lambda table, ids: [
            self._paths(json.loads(x[0]), True)
            for ident in ids
            if (
                x := db.execute(
                    f"SELECT payload FROM {table} WHERE id=?", (ident,)
                ).fetchone()
            )
        ]
        return {
            "news_id": r["news_id"],
            "attempt_id": r["attempt_id"],
            "path": self._paths({"path": r["path"]}, True)["path"],
            "kind": r["kind"],
            "status": r["status"],
            "images": get("candidates", meta.get("image_ids", [])),
            "videos": get("candidates", meta.get("video_ids", [])),
            "failures": get("failures", meta.get("failure_ids", [])),
            "source_url": meta.get("source_url"),
            "sources": meta.get("sources", []),
        }

    def export_review_payloads(self):
        with self._lock(self.path), self._connection() as db:
            meta = db.execute(
                "SELECT value FROM task_meta WHERE key='issue_id'"
            ).fetchone()
            issue = json.loads(meta[0]) if meta else ""
            news = [
                json.loads(r[0])
                for r in db.execute("SELECT payload FROM news ORDER BY rowid")
            ]
            images = [
                self._paths(json.loads(r[0]), True)
                for r in db.execute(
                    "SELECT payload FROM candidates WHERE kind='image' ORDER BY ordinal,id"
                )
            ]
            videos = [
                self._paths(json.loads(r[0]), True)
                for r in db.execute(
                    "SELECT payload FROM candidates WHERE kind='video' ORDER BY ordinal,id"
                )
            ]
            failures = [
                self._paths(json.loads(r[0]), True)
                for r in db.execute("SELECT payload FROM failures ORDER BY rowid")
            ]
            review_row = db.execute(
                "SELECT value FROM review_meta WHERE key='review_ids'"
            ).fetchone()
            review_ids = set(json.loads(review_row[0])) if review_row else set()
            required = [
                x
                for x in images
                if x.get("review_reasons")
                or x.get("review_required")
                or str(x.get("id")) in review_ids
            ]
            image_news = []
            for n in news:
                nid = n.get("news_id") or n.get("id")
                related = [x for x in images if str(x.get("news_id")) == str(nid)]
                image_news.append(
                    {
                        **n,
                        "news_id": nid,
                        "sequence": n.get("sequence"),
                        "section": n.get("section"),
                        "title": n.get("title"),
                        "status": (
                            "selected"
                            if any(x.get("selected") for x in related)
                            else ("no_candidate" if not related else "not_selected")
                        ),
                        "candidates": [x.get("id") for x in related],
                    }
                )
            video_news = [
                {
                    **n,
                    "news_id": n.get("news_id") or n.get("id"),
                    "sequence": n.get("sequence"),
                    "section": n.get("section"),
                    "title": n.get("title"),
                    "videos": [
                        x.get("id")
                        for x in videos
                        if str(x.get("news_id")) == str(n.get("news_id") or n.get("id"))
                    ],
                }
                for n in news
            ]
        return (
            {
                "schema_version": 1,
                "issue_id": issue,
                "news_items": image_news,
                "candidates": images,
                "failures": failures,
            },
            {
                "schema_version": 1,
                "issue_id": issue,
                "news_items": video_news,
                "videos": videos,
                "failures": failures,
            },
            required,
            failures,
        )

    def backup(self, destination):
        destination = Path(destination).expanduser().resolve()
        if (
            destination == self.root
            or self.root in destination.parents
            or destination in self.root.parents
        ):
            raise ValueError("backup destination must be outside task root hierarchy")
        if destination.exists() and any(destination.iterdir()):
            raise FileExistsError("backup destination must be empty")
        destination.mkdir(parents=True, exist_ok=True)
        with self._lock(self.path), self._connection() as db:
            referenced_work = set()
            for row in db.execute("SELECT payload FROM candidates"):
                payload = json.loads(row[0])
                for key in _ASSET_KEYS:
                    value = payload.get(key)
                    if not value:
                        continue
                    asset = (self.root / value).resolve()
                    if not asset.is_relative_to(self.root):
                        raise ValueError("candidate asset path escapes task root")
                    if ".work" in asset.relative_to(self.root).parts:
                        referenced_work.add(asset)
            target_path = destination / "task_state.sqlite3"
            target = sqlite3.connect(target_path)
            try:
                db.backup(target)
                for p in self.root.rglob("*"):
                    rel = p.relative_to(self.root)
                    if p.name.endswith((".part", "-journal", "-wal", "-shm")):
                        continue
                    resolved = p.resolve()
                    if (
                        ".work" in rel.parts
                        and resolved not in referenced_work
                        and not (
                            p.is_dir()
                            and any(
                                asset.is_relative_to(resolved)
                                for asset in referenced_work
                            )
                        )
                    ):
                        continue
                    if p.is_file() and p != self.path:
                        out = destination / rel
                        out.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(p, out)
            finally:
                target.close()
        return destination

    def integrity_check(self):
        with self._lock(self.path), self._connection() as db:
            check = db.execute("PRAGMA quick_check").fetchone()[0]
            rows = db.execute("SELECT id,payload FROM candidates").fetchall()
            cached = {
                (r[0], r[1]): json.loads(r[2])
                for r in db.execute(
                    "SELECT c.candidate_id,c.role,a.payload FROM candidate_assets c JOIN assets a ON a.id=c.asset_id"
                )
            }
        assets = []
        for row in rows:
            data = json.loads(row["payload"])
            for key in _ASSET_KEYS:
                value = data.get(key)
                if value:
                    role = (
                        "original"
                        if key == "original_path"
                        else (
                            "output"
                            if key == "output_path"
                            else "local" if key == "local_path" else None
                        )
                    )
                    p = (self.root / value).resolve()
                    if not p.is_relative_to(self.root):
                        assets.append(
                            {
                                "id": row["id"],
                                "path": str(p),
                                "exists": False,
                                "sha256": None,
                                "error": "asset path escapes task root",
                            }
                        )
                        continue
                    stat = p.stat() if p.is_file() else None
                    memo = cached.get((row["id"], role)) if role else None
                    memo_ok = bool(
                        memo
                        and memo.get("path") == str(value)
                        and stat
                        and memo.get("size") == stat.st_size
                        and memo.get("mtime_ns") == stat.st_mtime_ns
                    )
                    digest = (
                        (
                            memo.get("sha256")
                            if memo_ok
                            else hashlib.sha256(p.read_bytes()).hexdigest()
                        )
                        if p.is_file()
                        else None
                    )
                    expected = (
                        data.get("original_sha256")
                        if key == "original_path"
                        else (
                            data.get("output_sha256") or data.get("sha256")
                            if key in {"output_path", "local_path"}
                            else None
                        )
                    )
                    decodable = memo.get("decodable") if memo_ok else None
                    if (
                        p.is_file()
                        and key != "path"
                        and p.suffix.casefold()
                        in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
                    ):
                        if decodable is None:
                            try:
                                self._decode_image(p)
                                decodable = True
                            except Exception:
                                decodable = False
                    assets.append(
                        {
                            "id": row["id"],
                            "path": str(p),
                            "exists": p.is_file(),
                            "sha256": digest,
                            "hash_matches": (
                                None if not expected else digest == expected
                            ),
                            "decodable": decodable,
                        }
                    )
        return {
            "quick_check": check,
            "assets": assets,
            "ok": check == "ok"
            and all(
                x["exists"]
                and x.get("hash_matches") is not False
                and x.get("decodable") is not False
                for x in assets
            ),
        }
