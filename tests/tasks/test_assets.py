from datetime import datetime, timezone
import errno
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from galgame_news.config import load_config
from galgame_news.curation.download import ImageDownloader
from galgame_news.domain import (
    ImageCandidate,
    ImageType,
    Issue,
    NewsItem,
    PipelineResult,
    SourceType,
)
from galgame_news.delivery.output import OutputManager
from galgame_news.tasks.assets import TaskAssets
from galgame_news.tasks.repository import TaskRepository


def _png(color="red"):
    stream = BytesIO()
    Image.new("RGB", (400, 400), color).save(stream, format="PNG")
    return stream.getvalue()


def _candidate(url="https://example.org/image.png"):
    return ImageCandidate(
        news_id="n1",
        image_url=url,
        source_url="https://example.org/news",
        source_type=SourceType.OFFICIAL_SITE,
        image_type=ImageType.GAME_CG,
        fetched_at=datetime.now(timezone.utc),
        downloadable=True,
    )


def test_task_assets_are_content_addressed_and_profile_scoped(tmp_path):
    TaskRepository(tmp_path).activate()
    assets = TaskAssets(tmp_path)
    original = assets.put_original(_png(), ".png")
    assert original == assets.put_original(_png(), ".png")
    assert original == assets.put_original(_png(), ".jpg")
    assert len(list(original.parent.iterdir())) == 1
    a = assets.put_review(b"review", ".png", profile="v1")
    b = assets.put_review(b"review", ".png", profile="v2")
    assert a != b
    assert original.read_bytes() == _png()


def test_task_assets_fall_back_to_atomic_replace_when_hard_links_are_unsupported(
    tmp_path,
):
    TaskRepository(tmp_path).activate()
    assets = TaskAssets(tmp_path)
    payload = _png("blue")
    with patch(
        "galgame_news.tasks.assets.os.link",
        side_effect=OSError(errno.ENOTSUP, "unsupported"),
    ):
        path = assets.put_original(payload, ".png")
    assert path.is_file()
    assert path.read_bytes() == payload
    assert sorted(item.name for item in path.parent.iterdir()) == [path.name]


def test_downloader_reuses_shared_original_across_batch_directories(tmp_path):
    TaskRepository(tmp_path).activate()
    downloader = ImageDownloader(load_config())
    candidate_a = _candidate()
    candidate_b = _candidate("https://example.org/other.png")
    response = type(
        "Response",
        (),
        {
            "status_code": 200,
            "url": candidate_a.image_url,
            "headers": {"content-type": "image/png"},
            "content": _png(),
        },
    )()
    a, failure, _ = downloader._process_response(
        candidate_a, response, tmp_path / ".work" / "images"
    )
    response.url = candidate_b.image_url
    b, failure_b, _ = downloader._process_response(
        candidate_b, response, tmp_path / "retries" / "news" / "batch" / "images"
    )
    assert failure is None and failure_b is None
    assert a.local_path == b.local_path
    assert a.local_path.endswith(".png")
    assert "assets/originals" in a.local_path.replace("\\", "/")
    assert len(list((tmp_path / "assets" / "originals").iterdir())) == 1


def test_output_manager_uses_task_assets_without_changing_final_export(tmp_path):
    TaskRepository(tmp_path).activate()
    raw = tmp_path / "raw"
    item = NewsItem(
        id="n1", issue_id="1", sequence=1, section="新作", title="Game", body=""
    )
    source = tmp_path / "input.png"
    source.write_bytes(_png())
    candidate = _candidate()
    candidate.news_id = item.id
    candidate.local_path = str(source)
    candidate.selected = True
    result = PipelineResult(
        issue=Issue(issue_id="1", input_path="fixture", news_items=[item]),
        candidates=[candidate],
    )
    OutputManager().write(result, raw)
    assert candidate.local_path.startswith(str(tmp_path / "assets" / "review"))
    assert Path(candidate.local_path).is_file()
    assert not (raw / "images" / "Game" / "Game.01.png").exists()
    assert (raw / "image_index.json").is_file()
