"""Task-local durable ledger for automatic image supplementation."""
from __future__ import annotations

import sqlite3
import json
from pathlib import Path
from urllib.parse import unquote_plus, urlsplit, urlunsplit

_TRACKING = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "utm_id", "utm_name", "utm_reader", "utm_referrer"}


def normalize_url(url: str) -> str:
    p = urlsplit(str(url).strip())
    # Keep significant query bytes/order intact (some providers sign raw query
    # strings); remove only the explicitly recognized tracking keys.
    query = "&".join(part for part in p.query.split("&")
                     if unquote_plus(part.partition("=")[0]).casefold() not in _TRACKING)
    # Fragments can identify meaningful galleries or product sections; retain them.
    return urlunsplit((p.scheme.casefold(), p.netloc.casefold(), p.path or "/", query, p.fragment))


class SupplementLedger:
    """One ledger per task. Redirect aliases resolve to their canonical URL."""
    def __init__(self, task_root):
        self.root = Path(task_root).resolve() / "supplement"
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "ledger.sqlite3"
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS sources(news_id TEXT NOT NULL,url TEXT NOT NULL,state TEXT NOT NULL,
                    redirect_url TEXT,detail TEXT NOT NULL DEFAULT '',updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(news_id,url));
                CREATE TABLE IF NOT EXISTS aliases(news_id TEXT NOT NULL,alias TEXT NOT NULL,url TEXT NOT NULL,
                    PRIMARY KEY(news_id,alias));
                CREATE TABLE IF NOT EXISTS page_cache(url TEXT PRIMARY KEY,payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
            """)

    def _db(self):
        db = sqlite3.connect(self.path, timeout=20)
        db.row_factory = sqlite3.Row
        return db

    def resolve(self, news_id, url):
        key = normalize_url(url)
        with self._db() as db:
            row = db.execute("SELECT url FROM aliases WHERE news_id=? AND alias=?", (str(news_id), key)).fetchone()
            return row["url"] if row else key

    def get(self, news_id, url):
        key = self.resolve(news_id, url)
        with self._db() as db:
            row = db.execute("SELECT * FROM sources WHERE news_id=? AND url=?", (str(news_id), key)).fetchone()
            return dict(row) if row else None

    def running(self, news_id):
        with self._db() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM sources WHERE news_id=? AND state='running'", (str(news_id),)
            )]

    def get_page(self, url):
        key = normalize_url(url)
        with self._db() as db:
            row = db.execute("SELECT payload FROM page_cache WHERE url=?", (key,)).fetchone()
        return json.loads(row["payload"]) if row else None

    def set_page(self, url, source, candidates):
        key = normalize_url(url)
        value = {"source": source.model_dump(mode="json") if hasattr(source, "model_dump") else source,
                 "candidates": [c.model_dump(mode="json") if hasattr(c, "model_dump") else c for c in candidates]}
        with self._db() as db:
            db.execute("INSERT INTO page_cache(url,payload) VALUES(?,?) ON CONFLICT(url) DO UPDATE SET payload=excluded.payload,updated_at=CURRENT_TIMESTAMP",
                       (key, json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)))

    def set_state(self, news_id, url, state, *, redirect_url=None, detail=""):
        if state not in {"pending", "running", "completed", "partial", "no_assets", "failed", "cancelled", "seeded"}:
            raise ValueError(f"unknown supplement state: {state}")
        news_id, original = str(news_id), normalize_url(url)
        redirect = normalize_url(redirect_url) if redirect_url else None
        key = redirect or original
        with self._db() as db:
            db.execute("INSERT INTO sources(news_id,url,state,redirect_url,detail,updated_at) VALUES(?,?,?,?,?,CURRENT_TIMESTAMP) "
                       "ON CONFLICT(news_id,url) DO UPDATE SET state=excluded.state,redirect_url=excluded.redirect_url,detail=excluded.detail,updated_at=CURRENT_TIMESTAMP",
                       (news_id, key, state, redirect, str(detail)))
            db.execute("INSERT OR REPLACE INTO aliases(news_id,alias,url) VALUES(?,?,?)", (news_id, original, key))
            if redirect:
                db.execute("INSERT OR REPLACE INTO aliases(news_id,alias,url) VALUES(?,?,?)", (news_id, redirect, redirect))
        return key

    def should_skip(self, news_id, url, *, retry_failed=False):
        row = self.get(news_id, url)
        if not row:
            return False
        if row["state"] in {"completed", "no_assets", "seeded"}:
            return True
        return row["state"] == "failed" and not retry_failed

    def alias(self, news_id, alias, canonical):
        alias, canonical, news_id = normalize_url(alias), normalize_url(canonical), str(news_id)
        with self._db() as db:
            db.execute("INSERT OR REPLACE INTO aliases(news_id,alias,url) VALUES(?,?,?)", (news_id, alias, canonical))

