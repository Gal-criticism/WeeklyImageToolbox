import asyncio
from concurrent.futures import ThreadPoolExecutor
import threading
import time

import pytest

from galgame_news.delivery.history import SQLiteHistoryStore
from galgame_news.discovery.async_resolver import resolve_async
from galgame_news.discovery.resolver import DefaultSourceResolver
from galgame_news.domain import NewsItem


def _news():
    return NewsItem(issue_id="1", sequence=1, section="新作", title="Game", body="", game_names=["Game"])


def test_blocked_history_lookup_leaves_event_loop_free_for_deadline():
    release = threading.Event()

    def lookup(_):
        release.wait(0.3)
        return []

    async def scenario():
        async def call_sync(call):
            return await asyncio.to_thread(call)

        resolver = DefaultSourceResolver(history_lookup=lookup, same_domain_depth=0, search_provider=lambda _: [])
        try:
            started = time.monotonic()
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(resolve_async(resolver, _news(), None, call_sync), 0.03)
            assert time.monotonic() - started < 0.2
        finally:
            release.set()

    asyncio.run(scenario())


def test_sqlite_history_has_thread_owned_read_connection(tmp_path):
    store = SQLiteHistoryStore(tmp_path / "history.sqlite3")
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(store.sources_for_in_worker, _news()).result(timeout=1) == []
        assert store.sources_for(_news()) == []
    finally:
        store.connection.close()
