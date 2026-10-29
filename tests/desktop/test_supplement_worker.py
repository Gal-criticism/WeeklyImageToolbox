from types import SimpleNamespace

from galgame_news.desktop.worker import SupplementMissingWorker
from galgame_news.pipeline.contracts import CancellationToken


def test_automatic_supplement_worker_runs_async_service_and_returns_results(monkeypatch, tmp_path):
    from galgame_news.supplement import service

    calls = []
    expected = [SimpleNamespace(news_id="n1", success_count=2)]

    async def fake_supplement_missing(runner, **kwargs):
        calls.append((runner, kwargs))
        return expected

    monkeypatch.setattr(service, "supplement_missing", fake_supplement_missing)
    runner = object()
    token = CancellationToken()
    identity = ("task-1", "session-1", "run-1")
    worker = SupplementMissingWorker(runner, {"task_root": tmp_path, "issue": object(), "request": object()}, token, identity)
    finished = []
    failed = []
    worker.finished.connect(finished.append)
    worker.failed.connect(failed.append)

    worker.run()

    assert failed == []
    assert finished == [(identity, expected)]
    assert calls[0][0] is runner
    assert calls[0][1]["task_root"] == tmp_path
    assert calls[0][1]["cancellation_token"] is token
