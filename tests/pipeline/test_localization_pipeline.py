from datetime import datetime, timezone
from html import escape
from io import BytesIO
import json
from pathlib import Path
from types import SimpleNamespace

from PIL import Image, ImageDraw

from galgame_news.domain import Issue, NewsItem, SourceRef, SourceType
from galgame_news.pipeline import PipelineRunner, TaskRequest


def run_fixture(root: Path):
    """Frozen public-source replay; makes no network or paid API calls."""
    root.mkdir(parents=True, exist_ok=True)
    document = root / "fixture.docx"
    document.write_bytes(b"frozen localization pipeline input")
    url = "https://store.steampowered.com/app/3419820/"
    news = NewsItem(issue_id="localization", sequence=2, section="汉化", title="作品汉化发布",
                    body="汉化", game_names=["Fixture Work"], source_urls=[url])
    prefix = "https://shared.fastly.steamstatic.com/store_item_assets/steam/apps/3419820/"
    shots = [{"name": f"ss_{i}", "full": f"{prefix}ss_{i}.1920x1080.jpg",
              "thumbnail": f"{prefix}ss_{i}.116x65.jpg", "altText": "游戏截图"} for i in range(3)]
    html = '<div data-featuretarget="gamehighlight-desktopcarousel" data-props="' + escape(json.dumps({"appName": "Fixture Work", "screenshots": shots, "trailers": []}), quote=True) + '"></div>'
    class Parser:
        def parse(self, *_):
            return None
    class Analyzer:
        def analyze(self, _):
            return Issue(issue_id="localization", input_path=str(document), news_items=[news])
    class Resolver:
        def resolve(self, _):
            return [SourceRef(url=url, domain="store.steampowered.com", source_type=SourceType.STEAM)]
    requests = []
    def pages(address, **kwargs):
        requests.append(address)
        return SimpleNamespace(url=address, text=html, status_code=200, headers={"content-type": "text/html"})
    def images(address, **kwargs):
        index = int(address.split("ss_")[1].split(".")[0])
        size = (116, 65) if "116x65" in address else (1920, 1080)
        image = Image.new("RGB", size, (30 + index * 50, 50, 140))
        draw = ImageDraw.Draw(image)
        for n in range(12):
            x = (n * 127 + index * 83) % size[0]
            draw.line((x, 0, size[0] - x, size[1]), fill=(220, n * 15, 30), width=5)
        data = BytesIO()
        image.save(data, "JPEG", quality=95)
        return SimpleNamespace(url=address, content=data.getvalue(), status_code=200, headers={"content-type": "image/jpeg"})
    runner = PipelineRunner(parser=Parser(), analyzer=Analyzer(), resolver=Resolver(),
                            source_transport=pages, image_transport=images)
    request = TaskRequest(input_path=document, issue_id="localization", output_dir=root / "output",
                          no_videos=True, images_only=True)
    result = runner.run(request)
    return runner, result, request, requests


def test_formal_publication_keeps_all_localization_assets_and_resumes_selected_final(tmp_path):
    runner, result, request, requests = run_fixture(tmp_path)
    assert result.status == "completed", result.failures
    candidates = result.result.all_candidates
    assert len(candidates) == 6
    assert sum(c.selected for c in candidates) == 3
    assert all(not c.selected for c in candidates if c.localization_provenance.resolution == "thumbnail")
    assert result.task_dir.joinpath("raw/image_index.json").is_file()
    assert result.checkpoint_path.is_file()
    assert not result.images_only
    for candidate in candidates:
        assert Path(candidate.local_path).is_file()
        with Image.open(candidate.local_path) as image:
            assert image.format in {"PNG", "JPEG"}
        assert candidate.localization_provenance.source_chain
    assert runner.network_metrics["socialdata_requests"] == 0
    files = list((result.task_dir / "final/images").rglob("*.jpg")) + list((result.task_dir / "final/images").rglob("*.png"))
    assert len(files) == sum(candidate.selected for candidate in candidates) == 3
    assert all("未候选" not in str(path) for path in files)
    raw_index = json.loads((result.task_dir / "raw/image_index.json").read_text(encoding="utf-8"))
    assert len(raw_index["candidates"]) == 6
    assert {candidate["id"] for candidate in raw_index["candidates"]} == {candidate.id for candidate in candidates}
    assert all(Path(candidate["local_path"]).is_file() for candidate in raw_index["candidates"])
    assert len(requests) == 1
    resume = TaskRequest(input_path=request.input_path, issue_id=request.issue_id, output_dir=request.output_dir,
                         no_videos=True, task_dir=result.task_dir, resume=True, images_only=True)
    resumed = runner.run(resume)
    assert resumed.status == "completed"
    assert len(requests) == 1
    assert resumed.task_dir.joinpath("raw/image_index.json").is_file()


def test_old_candidate_and_news_indexes_default_to_no_localization_proof():
    from galgame_news.domain import ImageCandidate
    news = NewsItem(issue_id="1", sequence=1, section="汉化", title="旧任务", body="")
    candidate = ImageCandidate(news_id=news.id, image_url="https://example.test/image.jpg",
                               source_url="https://example.test", fetched_at=datetime.now(timezone.utc))
    assert news.localization_context is None
    assert candidate.localization_provenance is None


def test_unlinked_candidates_cannot_create_work_identity():
    from galgame_news.domain import ImageCandidate, LocalizationContext, LocalizationProvenance
    news = NewsItem(issue_id="1", sequence=1, section="汉化", title="作品", body="",
                    source_urls=["https://store.steampowered.com/app/123/"],
                    localization_context=LocalizationContext(steam_app_ids=["123"]))
    candidate = ImageCandidate(news_id=news.id, image_url="https://example.test/image.jpg",
        source_url="https://store.steampowered.com/app/456/", fetched_at=datetime.now(timezone.utc),
        localization_provenance=LocalizationProvenance(source="steam", work_id="456", binding="confirmed"))
    PipelineRunner._bind_localization_context(news, [candidate])
    assert news.localization_context.steam_app_ids == ["123"]
    assert news.localization_context.vndb_ids == []
    candidate.news_source_url = news.source_urls[0]
    PipelineRunner._bind_localization_context(news, [candidate])
    assert news.localization_context.steam_app_ids == ["123"]


def test_h_screenshots_share_twenty_slots_with_x_photos(tmp_path):
    from galgame_news.curation import ImageCurator
    from galgame_news.domain import ImageCandidate, LocalizationContext, LocalizationProvenance
    steam = "https://store.steampowered.com/app/123/"
    post = "https://x.com/example/status/123"
    news = NewsItem(issue_id="1", sequence=1, section="汉化", title="作品", body="",
        source_urls=[steam, post], localization_context=LocalizationContext(steam_app_ids=["123"]))
    path = tmp_path / "original.png"
    Image.new("RGB", (1024, 768), "blue").save(path)
    values = [ImageCandidate(news_id=news.id, image_url=f"https://example.test/{i}.png", source_url=steam,
        fetched_at=datetime.now(timezone.utc), source_type=SourceType.STEAM, width=1024, height=768,
        sha256=f"unique-{i}", local_path=str(path), downloadable=True, download_status="downloaded",
        localization_provenance=LocalizationProvenance(source="steam", work_id="123", binding="confirmed",
                                                       resolution="full", screenshot=True, native=True)) for i in range(20)]
    issue = Issue(issue_id="1", input_path="fixture", news_items=[news])
    result = ImageCurator().curate(issue, values)
    assert sum(c.selected for c in result.candidates) == 20
    x = ImageCandidate(news_id=news.id, image_url="https://pbs.twimg.com/media/test.jpg?name=orig",
        source_url=post, fetched_at=datetime.now(timezone.utc), source_type=SourceType.OFFICIAL_X,
        width=1024, height=768, sha256="unique-x", local_path=str(path), downloadable=True,
        download_status="downloaded", signals={"x_api_photo": True})
    result = ImageCurator().curate(issue, [*values, x])
    assert x.selected
    assert sum(c.selected for c in result.candidates) == 20
    assert sum(c.selected for c in values) == 19
