from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from galgame_news.config import VideoConfig
from galgame_news.domain import FailureStage, VideoCandidate, VideoStatus
from galgame_news.video.downloader import VideoDownloader


class FakeBackend:
    def __init__(self, options, info, payload=b"fake-video", error=None, errors=None):
        self.options = options
        self.info = info
        self.current_info = None
        self.payload = payload
        self.error = error
        self.errors = errors or {}

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def extract_info(self, _url, download=False):
        assert download is False
        self.error = self.errors.get(_url, self.error)
        if self.error:
            raise self.error
        if isinstance(self.info, dict) and _url in self.info and isinstance(self.info[_url], dict):
            self.current_info = dict(self.info[_url])
        else:
            self.current_info = dict(self.info)
        return dict(self.current_info)

    def download(self, _urls):
        if self.error:
            raise self.error
        outtmpl = str(self.options["outtmpl"])
        extension = str((self.current_info or self.info).get("ext") or "mp4")
        output = Path(outtmpl.replace("%(ext)s", extension))
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(self.payload)
        return 0


def make_candidate(news_id="x1", url="https://video.example/watch?v=1"):
    return VideoCandidate(
        news_id=news_id,
        source_url="https://news.example/item",
        video_url=url,
        title="discovered title",
    )


def make_info(**overrides):
    info = {
        "title": "PV: heroine <preview>",
        "uploader": "official",
        "duration": 600,
        "width": 1920,
        "height": 1080,
        "ext": "mp4",
        "filesize": 16,
    }
    info.update(overrides)
    return info


def make_factory(infos, payload=b"fake-video", errors=None):
    calls = []
    errors = errors or {}

    def factory(options):
        calls.append(options)
        return FakeBackend(options, infos, payload=payload, errors=errors)

    return factory, calls


def test_downloads_at_exact_duration_limit_and_records_hash_atomically(tmp_path):
    candidate = make_candidate()
    factory, calls = make_factory({candidate.video_url: make_info()})

    downloaded, failures = VideoDownloader(
        VideoConfig(), backend_factory=factory, ffmpeg_detector=lambda: True
    ).download([candidate], tmp_path)

    assert failures == []
    assert downloaded[0].status is VideoStatus.DOWNLOADED
    assert downloaded[0].duration_seconds == 600
    assert downloaded[0].byte_size == len(b"fake-video")
    assert downloaded[0].sha256 == hashlib.sha256(b"fake-video").hexdigest()
    assert Path(downloaded[0].local_path).is_file()
    assert calls[0]["noplaylist"] is True
    assert "bv*[height<=1080]+ba" in calls[0]["format"]
    assert calls[0]["merge_output_format"] == "mp4"


def test_rejects_duration_over_limit_before_download(tmp_path):
    candidate = make_candidate()
    factory, calls = make_factory({candidate.video_url: make_info(duration=601)})

    downloaded, failures = VideoDownloader(
        VideoConfig(), backend_factory=factory, ffmpeg_detector=lambda: True
    ).download([candidate], tmp_path)

    assert downloaded[0].status is VideoStatus.SKIPPED
    assert "duration_exceeded" in downloaded[0].review_reasons
    assert failures[0].code == "duration_exceeded"
    assert len(calls) == 1


def test_rejects_file_size_over_limit_but_allows_exact_boundary(tmp_path):
    boundary = 1 << 30
    allowed = make_candidate(url="https://video.example/allowed")
    too_large = make_candidate(url="https://video.example/large")
    factory, _calls = make_factory(
        {
            allowed.video_url: make_info(filesize=boundary),
            too_large.video_url: make_info(filesize=boundary + 1),
        }
    )

    downloaded, failures = VideoDownloader(
        VideoConfig(), backend_factory=factory, ffmpeg_detector=lambda: False
    ).download([allowed, too_large], tmp_path)

    assert downloaded[0].status is VideoStatus.DOWNLOADED
    assert downloaded[1].status is VideoStatus.SKIPPED
    assert "file_size_exceeded" in downloaded[1].review_reasons
    assert any(f.code == "file_size_exceeded" for f in failures)


def test_no_ffmpeg_prefers_single_file_mp4_and_allows_webm_fallback(tmp_path):
    mp4 = make_candidate(url="https://video.example/mp4")
    webm = make_candidate(url="https://video.example/webm")
    factory, calls = make_factory(
        {
            mp4.video_url: make_info(ext="mp4"),
            webm.video_url: make_info(ext="webm"),
        }
    )

    VideoDownloader(
        VideoConfig(), backend_factory=factory, ffmpeg_detector=lambda: False
    ).download([mp4, webm], tmp_path)

    assert "[ext=mp4]" in calls[0]["format"]
    assert "[ext=webm]" in calls[0]["format"]
    assert "merge_output_format" not in calls[0]
    assert calls[0]["format"].index("mp4") < calls[0]["format"].index("webm")


def test_deduplicates_normalized_url_and_limits_each_news_to_three(tmp_path):
    first = make_candidate(url="https://video.example/watch?v=1#fragment")
    duplicate = make_candidate(url="https://VIDEO.example/watch?v=1")
    others = [make_candidate(url=f"https://video.example/watch?v={n}") for n in range(2, 6)]
    all_candidates = [first, duplicate, *others]
    infos = {candidate.video_url: make_info(title=f"video {index}") for index, candidate in enumerate(all_candidates)}
    infos[duplicate.video_url] = infos[first.video_url]
    factory, calls = make_factory(infos)

    downloaded, _failures = VideoDownloader(
        VideoConfig(max_per_news=3), backend_factory=factory, ffmpeg_detector=lambda: False
    ).download(all_candidates, tmp_path)

    assert sum(item.status is VideoStatus.DOWNLOADED for item in downloaded) == 3
    assert downloaded[1].status is VideoStatus.SKIPPED
    assert "duplicate_video_url" in downloaded[1].review_reasons
    assert len(calls) == 3
    assert sum("per_news_limit" in item.review_reasons for item in downloaded) == 2


def test_filename_is_cleaned_and_collisions_get_numbered_names(tmp_path):
    first = make_candidate(url="https://video.example/first")
    second = make_candidate(url="https://video.example/second")
    infos = {
        first.video_url: make_info(title='A<>:"/\\|?*\x00. '),
        second.video_url: make_info(title='A<>:"/\\|?*\x00. '),
    }
    factory, _calls = make_factory(infos)

    downloaded, _failures = VideoDownloader(
        VideoConfig(max_per_news=3), backend_factory=factory, ffmpeg_detector=lambda: False
    ).download([first, second], tmp_path)

    names = sorted(Path(item.local_path).name for item in downloaded)
    assert names == ["A (2).mp4", "A.mp4"]


def test_failure_isolated_and_failure_record_keeps_news_context(tmp_path):
    failed = make_candidate(url="https://video.example/fail")
    good = make_candidate(url="https://video.example/good")
    factory, _calls = make_factory(
        {failed.video_url: make_info(), good.video_url: make_info(title="good")},
        errors={failed.video_url: RuntimeError("private video")},
    )

    downloaded, failures = VideoDownloader(
        VideoConfig(), backend_factory=factory, ffmpeg_detector=lambda: False
    ).download([failed, good], tmp_path)

    assert downloaded[0].status is VideoStatus.FAILED
    assert downloaded[1].status is VideoStatus.DOWNLOADED
    assert failures[0].stage is FailureStage.DOWNLOAD
    assert failures[0].news_id == "x1"
    assert failures[0].retryable is True


def test_playlist_is_never_downloaded(tmp_path):
    candidate = make_candidate()
    factory, calls = make_factory(
        {candidate.video_url: {"entries": [{"id": "1"}], "_type": "playlist"}}
    )

    downloaded, failures = VideoDownloader(
        VideoConfig(), backend_factory=factory, ffmpeg_detector=lambda: False
    ).download([candidate], tmp_path)

    assert downloaded[0].status is VideoStatus.SKIPPED
    assert failures[0].code == "playlist_not_allowed"
    assert calls[0]["noplaylist"] is True


def test_selected_short_is_not_rejected_due_to_unused_portrait_formats(tmp_path):
    candidate = make_candidate(url="https://www.youtube.com/watch?v=AVxfuTX-f8M")
    factory, _ = make_factory(make_info(
        width=1080, height=1920, filesize=None,
        requested_formats=[{"width": 608, "height": 1080, "vcodec": "vp9", "filesize": 16},
                           {"vcodec": "none", "filesize": 4}],
        formats=[{"width": 1080, "height": 1920}, {"height": 2160}],
    ))
    values, failures = VideoDownloader(backend_factory=factory, ffmpeg_detector=lambda: True).download([candidate], tmp_path)
    assert not failures
    assert values[0].status is VideoStatus.DOWNLOADED
    assert (values[0].width, values[0].height) == (608, 1080)


def test_selected_size_uses_one_estimate_not_top_level_plus_streams(tmp_path):
    candidate = make_candidate(url="https://video.twimg.com/ext_tw_video/sample.mp4")
    factory, _ = make_factory(make_info(
        filesize=None, filesize_approx=100,
        requested_formats=[{"height": 1080, "filesize": 80, "filesize_approx": 90},
                           {"vcodec": "none", "filesize": 20}],
    ))
    values, failures = VideoDownloader(VideoConfig(max_file_bytes=100), backend_factory=factory,
                                      ffmpeg_detector=lambda: True).download([candidate], tmp_path)
    assert not failures and values[0].status is VideoStatus.DOWNLOADED


def test_direct_x_allows_unknown_dimensions_then_checks_actual_media(tmp_path):
    factory, calls = make_factory(make_info(width=None, height=None))
    candidate = make_candidate(url="https://video.twimg.com/tweet_video/example.mp4")
    values, failures = VideoDownloader(
        backend_factory=factory, ffmpeg_detector=lambda: False,
        media_probe=lambda path: {"width": 1280, "height": 720, "duration_seconds": 2},
    ).download([candidate], tmp_path)
    assert "height<=?1080" in calls[0]["format"]
    assert not failures and values[0].status is VideoStatus.DOWNLOADED
    assert (values[0].width, values[0].height) == (1280, 720)


def test_default_backend_does_not_download_unverifiable_media_without_ffprobe(tmp_path, monkeypatch):
    downloader = VideoDownloader(ffmpeg_detector=lambda: False)
    monkeypatch.setattr(downloader, "_probe_executable", lambda: None)
    monkeypatch.setattr(downloader, "backend_factory", lambda options: pytest.fail("must detect missing tool before download"))
    values, failures = downloader.download([make_candidate()], tmp_path)
    assert values[0].status is VideoStatus.FAILED
    assert failures[0].code == "video_tools_missing" and failures[0].retryable
    assert not list(tmp_path.rglob("*.mp4"))


def test_failed_download_does_not_consume_news_limit_or_reuse_partial_file(tmp_path):
    bad = make_candidate(url="https://video.example/bad")
    good = make_candidate(url="https://video.example/good")

    class PartialBackend(FakeBackend):
        def download(self, urls):
            if "bad" in urls[0]:
                Path(self.options["outtmpl"]).with_suffix(".part").write_bytes(b"partial")
                raise TimeoutError()
            return super().download(urls)

    values, failures = VideoDownloader(
        VideoConfig(max_per_news=1), ffmpeg_detector=lambda: False,
        backend_factory=lambda options: PartialBackend(options, make_info()),
    ).download([bad, good], tmp_path)
    assert values[0].failure_reason and "TimeoutError" in values[0].failure_reason
    assert values[1].status is VideoStatus.DOWNLOADED
    assert len(failures) == 1
    assert not list(tmp_path.rglob("*.part"))


def test_download_uses_same_selected_metadata_when_backend_supports_it(tmp_path):
    class SelectedBackend(FakeBackend):
        def process_info(self, info):
            assert info == self.current_info
            return super().download(["selected-streams"])

        def download(self, urls):
            raise AssertionError("do not extract the URL a second time")

    values, failures = VideoDownloader(
        backend_factory=lambda options: SelectedBackend(options, make_info()), ffmpeg_detector=lambda: False,
    ).download([make_candidate()], tmp_path)
    assert not failures and values[0].status is VideoStatus.DOWNLOADED


def test_probe_updates_actual_metadata_and_failed_probe_keeps_original_url(tmp_path):
    candidate = make_candidate()
    factory, _ = make_factory(make_info())
    values, failures = VideoDownloader(
        backend_factory=factory, ffmpeg_detector=lambda: False,
        media_probe=lambda path: {"width": 640, "height": 360, "duration_seconds": 1.0},
    ).download([candidate], tmp_path)
    assert not failures
    assert (values[0].width, values[0].height, values[0].duration_seconds) == (640, 360, 1.0)

    def invalid(path):
        raise ValueError("video validation failed (ValueError)")

    values, failures = VideoDownloader(backend_factory=factory, ffmpeg_detector=lambda: False,
                                      media_probe=invalid).download([candidate], tmp_path / "invalid")
    assert values[0].status is VideoStatus.FAILED and values[0].video_url == candidate.video_url
    assert failures[0].code == "invalid_video" and not failures[0].retryable
    assert not list((tmp_path / "invalid").rglob("*.mp4"))
