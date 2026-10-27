from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from PIL import Image

from galgame_news.archive.materialize import ArchiveMaterializer
from galgame_news.archive.models import ArchiveAsset
from galgame_news.config import load_config
from galgame_news.domain import ImageCandidate, VideoCandidate, VideoStatus
from galgame_news.review.session import ReviewSession


class MemoryProvider:
    def __init__(self, originals: dict[str, bytes], previews: dict[str, bytes] | None = None):
        self.originals = originals
        self.previews = previews or {}
        self.calls: list[tuple[str, str, int]] = []

    def fetch(self, asset, target, max_bytes=100_000_000, cancelled=None):
        return self._write("original", asset, target, max_bytes, cancelled)

    def preview(self, asset, target, max_bytes=20_000_000, cancelled=None):
        return self._write("preview", asset, target, max_bytes, cancelled)

    def _write(self, kind, asset, target, max_bytes, cancelled):
        self.calls.append((kind, asset.asset_id, max_bytes))
        data = (self.previews if kind == "preview" else self.originals)[asset.asset_id]
        path = Path(target)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("wb") as stream:
                for offset in range(0, len(data), 64):
                    if cancelled and cancelled():
                        raise InterruptedError("cancelled")
                    stream.write(data[offset : offset + 64])
            if len(data) > max_bytes:
                raise ValueError("asset exceeds byte limit")
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return path


def make_asset(*, kind="image", asset_id="remote:a", preview_url=None, size=None):
    return ArchiveAsset(
        asset_id=asset_id,
        source_id="remote",
        kind=kind,
        issue_number="230",
        path="230/media.jpg" if kind == "image" else "230/media.mp4",
        name="media.jpg" if kind == "image" else "media.mp4",
        game_names=("リルカは幾重に夜を彩る",),
        issue_date="2026-10-20",
        revision="etag-1",
        size=size,
        source_kind="nextcloud",
        original_url="https://cloud.example/original",
        original_source_url="https://news.example/story",
        preview_url=preview_url,
    )


def test_original_cache_is_revision_keyed_verified_and_repaired(tmp_path):
    payload = b"stable-original"
    provider = MemoryProvider({"remote:a": payload})
    materializer = ArchiveMaterializer(tmp_path / "cache", lambda source_id: provider)
    asset = make_asset()

    first = materializer.get_original(asset)
    assert first.read_bytes() == payload
    first.write_bytes(b"corrupt")
    second = materializer.get_original(asset)

    assert second.read_bytes() == payload
    assert len([call for call in provider.calls if call[0] == "original"]) == 2
    assert hashlib.sha256(second.read_bytes()).hexdigest() == hashlib.sha256(payload).hexdigest()


def test_original_cache_separates_revisions_of_the_same_asset(tmp_path):
    provider = MemoryProvider({"remote:a": b"same file id"})
    materializer = ArchiveMaterializer(tmp_path / "cache", lambda source_id: provider)

    first = materializer.get_original(make_asset())
    revised = make_asset()
    object.__setattr__(revised, "revision", "etag-2")
    second = materializer.get_original(revised)

    assert first != second
    assert len(provider.calls) == 2


def test_preview_without_server_preview_is_not_fetched_as_original(tmp_path):
    provider = MemoryProvider({"remote:a": b"original"})
    materializer = ArchiveMaterializer(tmp_path / "cache", lambda source_id: provider)

    assert materializer.get_preview(make_asset()) is None
    assert provider.calls == []


def test_preview_is_cached_separately_from_original(tmp_path):
    provider = MemoryProvider({"remote:a": b"original"}, {"remote:a": b"thumbnail"})
    materializer = ArchiveMaterializer(tmp_path / "cache", lambda source_id: provider)
    asset = make_asset(preview_url="https://cloud.example/preview")

    preview = materializer.get_preview(asset)

    assert preview.read_bytes() == b"thumbnail"
    assert materializer.get_preview(asset) == preview
    assert [call[0] for call in provider.calls] == ["preview"]


def test_cancelled_or_oversize_download_removes_partial_cache_file(tmp_path):
    payload = b"x" * 2048
    provider = MemoryProvider({"remote:a": payload})
    materializer = ArchiveMaterializer(tmp_path / "cache", lambda source_id: provider,
                                       config=load_config())
    checks = 0

    def cancel_soon():
        nonlocal checks
        checks += 1
        return checks > 2

    with pytest.raises(InterruptedError):
        materializer.get_original(make_asset(), cancelled=cancel_soon)
    assert not list((tmp_path / "cache").rglob("*.part"))


def test_asset_size_over_configured_limit_is_rejected_before_fetch(tmp_path):
    provider = MemoryProvider({"remote:a": b"tiny"})
    config = load_config()
    config.filters.max_image_bytes = 3
    materializer = ArchiveMaterializer(tmp_path / "cache", lambda source_id: provider, config=config)

    with pytest.raises(ValueError, match="byte limit"):
        materializer.get_original(make_asset(size=4))
    assert provider.calls == []


def test_prepare_image_writes_retry_candidate_and_preserves_original(tmp_path):
    source = tmp_path / "source.png"
    Image.new("RGB", (512, 512), "red").save(source)
    provider = MemoryProvider({"remote:a": source.read_bytes()})
    materializer = ArchiveMaterializer(tmp_path / "cache", lambda source_id: provider)
    task_root = tmp_path / "task"

    candidate = materializer.prepare(make_asset(), "news-riruka", task_root)

    assert isinstance(candidate, ImageCandidate)
    assert candidate.selected is True
    assert candidate.archive_provenance["attempt_id"].startswith("archive_")
    assert candidate.archive_provenance["asset_id"] == "remote:a"
    assert Path(candidate.original_path).is_file()
    assert Path(candidate.local_path).is_file()
    assert Path(candidate.original_path).read_bytes() == source.read_bytes()
    attempt = task_root / "retries" / "news-riruka" / candidate.archive_provenance["attempt_id"]
    payload = json.loads((attempt / "image_index.json").read_text(encoding="utf-8"))
    assert payload["kind"] == "retry"
    assert payload["candidates"][0]["archive_provenance"]["asset_id"] == "remote:a"
    assert json.loads((attempt / "video_index.json").read_text(encoding="utf-8"))["videos"] == []
    session = ReviewSession(
        output_dir=task_root / "raw",
        task_root=task_root,
        state_path=task_root / "review_state.json",
        images=[], videos=[], news_items=[], source_paths={}, raw_types={},
        review_ids=set(), failed_ids=set(),
    )
    delta = session.prepare_retry_delta("news-riruka", candidate.archive_provenance["attempt_id"])
    session.apply_retry_delta(delta, force_pending=True)
    assert [item.id for item in session.images] == [candidate.id]


def test_prepare_video_probes_media_and_writes_downloaded_retry(tmp_path):
    media = b"small video bytes"
    provider = MemoryProvider({"remote:a": media})
    materializer = ArchiveMaterializer(
        tmp_path / "cache",
        lambda source_id: provider,
        media_probe=lambda path: {"width": 1280, "height": 720, "duration_seconds": 12.5},
    )
    asset = make_asset(kind="video")

    candidate = materializer.prepare(asset, "news-riruka", tmp_path / "task")

    assert isinstance(candidate, VideoCandidate)
    assert candidate.status is VideoStatus.DOWNLOADED
    assert candidate.local_path and Path(candidate.local_path).read_bytes() == media
    assert candidate.archive_provenance["asset_id"] == "remote:a"
    attempt = tmp_path / "task" / "retries" / "news-riruka" / candidate.archive_provenance["attempt_id"]
    payload = json.loads((attempt / "video_index.json").read_text(encoding="utf-8"))
    assert payload["kind"] == "retry"
    assert payload["videos"][0]["status"] == "downloaded"
    assert json.loads((attempt / "image_index.json").read_text(encoding="utf-8"))["candidates"] == []


def test_prepare_video_rejects_unplayable_or_out_of_policy_media(tmp_path):
    provider = MemoryProvider({"remote:a": b"media"})
    materializer = ArchiveMaterializer(
        tmp_path / "cache",
        lambda source_id: provider,
        media_probe=lambda path: None,
    )

    with pytest.raises(ValueError, match="playable"):
        materializer.prepare(make_asset(kind="video"), "news-riruka", tmp_path / "task")


def test_prepare_video_rejects_unsupported_container_without_renaming_bytes(tmp_path):
    provider = MemoryProvider({"remote:a": b"AVI bytes"})
    materializer = ArchiveMaterializer(
        tmp_path / "cache",
        lambda source_id: provider,
        media_probe=lambda path: {"width": 1280, "height": 720, "duration_seconds": 12},
    )
    asset = make_asset(kind="video")
    object.__setattr__(asset, "name", "clip.avi")

    with pytest.raises(ValueError, match="unsupported video container"):
        materializer.prepare(asset, "news-riruka", tmp_path / "task")

    assert not list((tmp_path / "task").rglob("*.mp4"))


def test_prepare_video_rejects_probe_container_that_disagrees_with_suffix(tmp_path):
    provider = MemoryProvider({"remote:a": b"Matroska bytes"})
    materializer = ArchiveMaterializer(
        tmp_path / "cache",
        lambda source_id: provider,
        media_probe=lambda path: {
            "width": 1280, "height": 720, "duration_seconds": 12,
            "format_name": "matroska,webm",
        },
    )
    asset = make_asset(kind="video")  # .mp4 suffix

    with pytest.raises(ValueError, match="container does not match"):
        materializer.prepare(asset, "news-riruka", tmp_path / "task")


@pytest.mark.parametrize("suffix", [".webm", ".mov", ".mkv"])
def test_prepare_video_keeps_supported_container_suffix(tmp_path, suffix):
    provider = MemoryProvider({"remote:a": b"media"})
    materializer = ArchiveMaterializer(
        tmp_path / "cache",
        lambda source_id: provider,
        media_probe=lambda path: {"width": 1280, "height": 720, "duration_seconds": 12},
    )
    asset = make_asset(kind="video")
    object.__setattr__(asset, "name", f"clip{suffix}")

    candidate = materializer.prepare(asset, "news-riruka", tmp_path / "task")

    assert isinstance(candidate, VideoCandidate)
    assert Path(candidate.local_path).suffix == suffix


@pytest.mark.parametrize(
    "measured, message",
    [
        ({"width": 1280, "height": 2160, "duration_seconds": 12}, "height"),
        ({"width": 1280, "height": 720, "duration_seconds": 900}, "duration"),
    ],
)
def test_prepare_video_enforces_configured_height_and_duration(tmp_path, measured, message):
    provider = MemoryProvider({"remote:a": b"media"})
    config = load_config()
    config.video.max_height = 1080
    config.video.max_duration_seconds = 600
    materializer = ArchiveMaterializer(
        tmp_path / "cache", lambda source_id: provider, config=config,
        media_probe=lambda path: measured,
    )

    with pytest.raises(ValueError, match=message):
        materializer.prepare(make_asset(kind="video"), "news-riruka", tmp_path / "task")
