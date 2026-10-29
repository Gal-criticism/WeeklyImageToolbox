from __future__ import annotations

import multiprocessing
import time

import pytest

from galgame_news.discovery.search import _run_spawned_worker


def _hang_worker(connection):
    time.sleep(30)


def test_spawned_search_worker_has_hard_deadline_and_is_reaped():
    name = "galgame-ddgs-deadline-test"
    started = time.monotonic()
    with pytest.raises(TimeoutError, match="DDGS search exceeded"):
        _run_spawned_worker(_hang_worker, (), 0.2, process_name=name)
    assert time.monotonic() - started < 2
    assert not any(child.name == name for child in multiprocessing.active_children())
