"""Section selection must happen before every public/paid media operation."""
from itertools import combinations
from io import BytesIO
from types import SimpleNamespace

import pytest
from PIL import Image

from galgame_news.config import load_config
from galgame_news.delivery.helpers import item_names
from galgame_news.domain import (
    CollectionResult, ImageCandidate, ImageEvidence, Issue, IssueDraft,
    NewsDraft, NewsItem, SourceRef, SourceType,
)
from galgame_news.pipeline import CancellationToken, PipelineRunner, TaskRequest
from galgame_news.pipeline.checkpoint import config_hash


ROWS = [(1, "新作"), (2, "周报"), (3, "汉化"), (4, "新作"),
        (5, "周边"), (6, "漢化"), (7, "业界"), (8, "其他")]
PREFIXES = {1: "x", 2: "z", 3: "h", 4: "x", 5: "z", 6: "h", 7: "z", 8: "z"}
SELECTIONS = [list(values) for size in (1, 2, 3) for values in combinations("xhz", size)]


@pytest.fixture(autouse=True)
def public_mock_media_dns(monkeypatch):
    """All network transports in this module are offline fixtures."""
    from galgame_news.pipeline import network_runtime
    client_class = network_runtime.AsyncHttpClient

    def create_client(**kwargs):
        return client_class(resolver=lambda _: ["93.184.216.34"], **kwargs)

    monkeypatch.setattr(network_runtime, "AsyncHttpClient", create_client)


def components(tmp_path, rows=ROWS):
    document = tmp_path / "weekly.docx"
    document.write_bytes(b"frozen document stub")
    calls = {"pages": [], "paid": [], "images": []}

    class Parser:
        def parse(self, path, issue_id):
            return IssueDraft(issue_id=issue_id, input_path=str(path), entries=[
                NewsDraft(sequence=sequence, section=section, title=f"Game {sequence}", body="新闻",
                    source_urls=[f"https://x.com/fixture/status/{1700000000000000000 + sequence}"])
                for sequence, section in rows])

    class Analyzer:
        def analyze(self, draft):
            return Issue(issue_id=draft.issue_id, input_path=draft.input_path, news_items=[
                NewsItem(issue_id=draft.issue_id, sequence=e.sequence, section=e.section,
                         title=e.title, body=e.body, source_urls=e.source_urls) for e in draft.entries])

    class Resolver:
        def resolve(self, news):
            calls["pages"].append(news.sequence)
            return [SourceRef(url=f"https://x.com/fixture/status/{1700000000000000000 + news.sequence}",
                              domain="x.com", source_type=SourceType.OFFICIAL_X)]

    class Adapter:
        def collect(self, news, source, context):
            # This stand-in for the paid adapter records calls, never credentials
            # or requests to an external service.
            calls["paid"].append(news.sequence)
            return CollectionResult(candidates=[
                ImageCandidate(news_id=news.id, source_url=source.url, source_type=SourceType.OFFICIAL_X, fetched_at=context.now,
                    image_url=f"https://pbs.twimg.com/media/fixture{news.sequence}?format=png&name=orig",
                    signals={"x_api_photo": True}),
                ImageCandidate(news_id=news.id, source_url=source.url, source_type=SourceType.OFFICIAL_X, fetched_at=context.now,
                    image_url=f"https://pbs.twimg.com/media/backup{news.sequence}?format=png&name=orig",
                    evidence=[ImageEvidence(page_url=source.url, role="decorative", text="栏目标题", method="dom")]),
            ])

    def image_transport(url, **kwargs):
        calls["images"].append(url)
        stream = BytesIO()
        Image.new("RGB", (800, 600), "blue" if "backup" in url else "red").save(stream, format="PNG")
        return SimpleNamespace(url=url, status_code=200, content=stream.getvalue(),
                               headers={"content-type": "image/png"})

    runner = PipelineRunner(parser=Parser(), analyzer=Analyzer(), resolver=Resolver(),
        adapter_factory=lambda *args: Adapter(), image_transport=image_transport)
    full = Analyzer().analyze(Parser().parse(document, "section-test"))
    return runner, document, calls, full


@pytest.mark.parametrize("selected", SELECTIONS)
def test_combinations_only_collect_selected_sections_and_keep_numbering(tmp_path, selected):
    runner, document, calls, full = components(tmp_path)
    events = []
    result = runner.run(TaskRequest(document, "section-test", tmp_path / "out",
        selected_sections=selected, no_videos=True, use_socialdata_x=True), events.append)
    expected = [sequence for sequence, _ in ROWS if PREFIXES[sequence] in selected]
    assert result.status == "completed", [f.message for f in result.failures]
    assert calls["pages"] == calls["paid"] == expected
    assert len(calls["images"]) == 2 * len(expected)
    assert [n.sequence for n in result.issue.news_items] == expected
    assert {n.id for n in result.issue.news_items} == {n.id for n in full.news_items if n.sequence in expected}
    names = item_names(full.news_items)
    image_root = result.task_dir / "final" / "images"
    assert {p.name for p in image_root.iterdir()} == {names[n.id] for n in full.news_items if n.sequence in expected}
    for news in result.issue.news_items:
        folder = image_root / names[news.id]
        assert len(list(folder.glob("*.png"))) + len(list(folder.glob("*.jpg"))) == 1
        assert len(list((folder / "未候选").glob("*"))) == 1
    starts = [e for e in events if e.kind == "news_started"]
    assert [e.news_index for e in starts] == list(range(1, len(expected) + 1))
    assert all(e.total_news == len(expected) for e in starts)
    analyzed = next(e for e in events if e.kind == "analyze_completed")
    assert analyzed.payload["selected_news_count"] == len(expected)
    assert analyzed.payload["document_news_count"] == len(ROWS)
    assert runner.network_metrics["socialdata_requests"] == 0


def test_empty_section_has_no_collection_or_download(tmp_path):
    runner, document, calls, _ = components(tmp_path, [(1, "新作")])
    result = runner.run(TaskRequest(document, "section-test", tmp_path / "out", selected_sections=["h"]))
    assert result.status == "failed"
    assert any(f.code == "no_selected_news_items" and "所选栏目" in f.message for f in result.failures)
    assert calls == {"pages": [], "paid": [], "images": []}
    assert not list(result.task_dir.rglob("images"))


@pytest.mark.parametrize("invalid", [[], ["wrong"], "x", None])
def test_request_rejects_empty_or_unknown_selection(tmp_path, invalid):
    with pytest.raises(ValueError):
        TaskRequest(tmp_path / "in.docx", "1", tmp_path / "out", selected_sections=invalid)


def test_default_and_order_preserve_historical_hash(tmp_path):
    request = TaskRequest(tmp_path / "in.docx", "1", tmp_path / "out")
    assert request.selected_sections == ["x", "h", "z"]
    old = SimpleNamespace(**{key: getattr(request, key) for key in (
        "offline", "no_videos", "max_images", "llm_provider", "llm_model", "use_socialdata_x")})
    config = load_config()
    assert config_hash(config, request) == config_hash(config, old)
    subset = TaskRequest(tmp_path / "in.docx", "1", tmp_path / "out", selected_sections=["z", "x", "x"])
    reordered = TaskRequest(tmp_path / "in.docx", "1", tmp_path / "out", selected_sections=["x", "z"])
    assert subset.selected_sections == ["x", "z"]
    assert config_hash(config, subset) == config_hash(config, reordered)
    assert config_hash(config, subset) != config_hash(config, request)


def test_resume_keeps_selected_sections_and_rejects_changes(tmp_path):
    runner, document, calls, _ = components(tmp_path)
    token = CancellationToken()

    def stop(event):
        if event.kind == "news_completed":
            token.cancel()

    request = TaskRequest(document, "section-test", tmp_path / "out", selected_sections=["z"],
                          no_videos=True, use_socialdata_x=True)
    paused = runner.run(request, stop, token)
    assert paused.status == "cancelled"
    assert calls["paid"] == [2]
    changed = runner.run(TaskRequest(document, "section-test", tmp_path / "out", selected_sections=["x"],
        no_videos=True, use_socialdata_x=True, task_dir=paused.task_dir, resume=True))
    assert changed.status == "failed"
    assert any(f.code == "checkpoint_config_mismatch" for f in changed.failures)
    assert calls["paid"] == [2]
    resumed = runner.run(TaskRequest(document, "section-test", tmp_path / "out", selected_sections=["z"],
        no_videos=True, use_socialdata_x=True, task_dir=paused.task_dir, resume=True))
    assert resumed.status == "completed"
    assert calls["paid"] == [2, 5, 7, 8]
    assert calls["pages"] == [2, 5, 7, 8]
