from datetime import datetime, timezone
import asyncio
from io import BytesIO
import json

import httpx
from PIL import Image

from galgame_news.config import load_config
from galgame_news.domain import CollectionResult, ImageCandidate, Issue, NewsItem, SourceRef, SourceType
from galgame_news.pipeline import PipelineRunner, TaskRequest
from galgame_news.supplement.ledger import SupplementLedger


def _png():
    buf = BytesIO()
    Image.new("RGB", (640, 480), "blue").save(buf, "PNG")
    return buf.getvalue()


def _png_color(color):
    buf = BytesIO()
    Image.new("RGB", (800, 600), color).save(buf, "PNG")
    return buf.getvalue()


def test_supplement_persists_unselected_review_candidate_before_export(tmp_path, monkeypatch):
    from galgame_news.supplement import providers
    from galgame_news.supplement.service import supplement_missing

    root = tmp_path / "task"
    root.mkdir()
    news = NewsItem(issue_id="issue", sequence=1, section="新作", title="Nebula 2.0 CG update",
        body="Nebula 2.0 new CG", game_names=["Nebula"], source_urls=["https://product.example.invalid/gallery"])
    other_news = NewsItem(issue_id="issue", sequence=2, section="新作", title="Nebula 2.0 CG update (follow-up)",
        body="Nebula 2.0 new CG", game_names=["Nebula"])
    issue = Issue(issue_id="issue", input_path=str(root / "input.docx"), news_items=[news, other_news])
    request = TaskRequest(input_path=root / "input.docx", issue_id="issue", output_dir=root,
        no_videos=True, use_image_supplement=True, supplement_target_images=1)
    source = SourceRef(url="https://product.example.invalid/gallery", domain="product.example.invalid",
        source_type=SourceType.OFFICIAL_SITE)
    monkeypatch.setattr(providers, "discover_sources", lambda *_a, **_k: [source])

    class Adapter:
        def collect(self, item, src, context):
            return CollectionResult(candidates=[ImageCandidate(news_id=item.id,
                image_url="https://img.example.invalid/nebula.png", source_url=src.url,
                    source_type=SourceType.OFFICIAL_SITE, nearby_text="Nebula 2.0 CG update",
                fetched_at=datetime.now(timezone.utc))])

    payload = _png()
    calls = {"page": 0, "media": 0}
    class CountingAdapter(Adapter):
        def collect(self, *args):
            calls["page"] += 1
            return super().collect(*args)
    def handle(req):
        calls["media"] += 1
        return httpx.Response(200, content=payload, headers={"content-type": "image/png"}, request=req)
    transport = httpx.MockTransport(handle)
    runner = PipelineRunner(config=load_config(), adapter_factory=lambda *_a: Adapter(),
        image_transport=lambda url, **kwargs: transport.handle_request(httpx.Request("GET", url)))
    runner.adapter_factory = lambda *_a: CountingAdapter()
    result = asyncio.run(supplement_missing(runner, task_root=root, issue=issue, request=request))

    assert len(result) == 2
    candidate = result[0].candidates[0]
    assert candidate.signals["supplement_module"] is True
    assert candidate.signals["supplement_adopted"] is False
    assert candidate.signals["supplement_relation"] == "direct"
    assert not candidate.selected
    assert candidate.download_status == "downloaded"
    assert list((root / "retries" / news.id).glob("automatic-*/image_index.json"))
    assert SupplementLedger(root).get(news.id, source.url)["state"] == "completed"
    assert runner._network_runtime is None
    assert calls == {"page": 1, "media": 1}
    manual = runner.supplement_news(task_root=root, issue_id=issue.issue_id, news_id=news.id,
        source_url=source.url, news_item=news, request=request)
    assert manual.success_count == 1
    assert calls == {"page": 1, "media": 1}


def test_six_identical_gallery_images_do_not_fill_five_unique_image_target(tmp_path, monkeypatch):
    from galgame_news.supplement import providers
    from galgame_news.supplement.service import supplement_missing

    root = tmp_path / "task"
    root.mkdir()
    official_url = "https://official.example.invalid/gallery"
    store_url = "https://store.example.invalid/gallery"
    news = NewsItem(issue_id="issue", sequence=1, section="新作", title="Nebula gallery",
        body="Nebula gallery update", game_names=["Nebula"], source_urls=[official_url, store_url])
    issue = Issue(issue_id="issue", input_path=str(root / "input.docx"), news_items=[news])
    request = TaskRequest(input_path=root / "input.docx", issue_id="issue", output_dir=root,
        no_videos=True, use_image_supplement=True, supplement_target_images=5)
    sources = {
        "official": SourceRef(url=official_url, domain="official.example.invalid", source_type=SourceType.OFFICIAL_SITE),
        "store": SourceRef(url=store_url, domain="store.example.invalid", source_type=SourceType.OFFICIAL_SITE),
    }
    stages = []
    monkeypatch.setattr(providers, "discover_sources", lambda _news, *, stage, **_kw: stages.append(stage) or ([sources[stage]] if stage in sources else []))
    calls = {"media": 0}

    class Adapter:
        def collect(self, item, source, context):
            n = 6 if source.url == official_url else 5
            return CollectionResult(candidates=[ImageCandidate(news_id=item.id,
                image_url=f"https://img.example.invalid/{'duplicate' if source.url == official_url else 'unique'}-{i}.png",
                source_url=source.url, source_type=SourceType.OFFICIAL_SITE,
                nearby_text=item.title, fetched_at=datetime.now(timezone.utc)) for i in range(n)])

    duplicate = _png_color("red")
    unique = [_png_color((i * 35, 100 + i * 20, 200 - i * 25)) for i in range(5)]
    transport = httpx.MockTransport(lambda req: calls.__setitem__("media", calls["media"] + 1) or
        httpx.Response(200, content=(duplicate if "duplicate" in req.url.path else unique[int(req.url.path.rsplit("-", 1)[1].split(".")[0])]),
            headers={"content-type": "image/png"}, request=req))
    runner = PipelineRunner(config=load_config(), adapter_factory=lambda *_a: Adapter(),
        image_transport=lambda url, **_kw: transport.handle_request(httpx.Request("GET", url)))
    results = asyncio.run(supplement_missing(runner, task_root=root, issue=issue, request=request))

    assert stages == ["official", "store"], (stages, results[0].status,
        [(c.download_status, c.curation_status.value, c.signals.get("supplement_relation"), c.sha256) for c in results[0].candidates])
    assert calls["media"] == 11
    assert results[0].status == "completed"

