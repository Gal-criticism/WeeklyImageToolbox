from pathlib import Path

from galgame_news.config import VideoConfig
from galgame_news.domain import NewsItem, VideoCandidate, VideoStatus
from galgame_news.video.downloader import VideoDownloader


class FakeBackend:
    def __init__(self, options, info):
        self.options, self.info, self.download_calls = options, info, 0

    def __enter__(self): return self
    def __exit__(self, *_exc): return False

    def extract_info(self, _url, download=False):
        assert download is False
        return dict(self.info)

    def download(self, _urls):
        self.download_calls += 1
        output = Path(str(self.options["outtmpl"]).replace("%(ext)s", "mp4"))
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"video")
        return 0


def news():
    return NewsItem(issue_id="255", sequence=5, section="新作", title="BA-KU宣布制作《百白》",
                    body="BA-KU announces its second game.", game_names=["百白"])


def run(tmp_path, info, *, context=True):
    backends = []
    def factory(options):
        backend = FakeBackend(options, info)
        backends.append(backend)
        return backend
    candidate = VideoCandidate(news_id="n1", source_url="https://search.example/result",
                               video_url="https://www.youtube.com/watch?v=Q3hxYqAjO8c", title="")
    args = {"news_item": news()} if context else {}
    downloaded, failures = VideoDownloader(VideoConfig(), backend_factory=factory,
        ffmpeg_detector=lambda: True).download([candidate], tmp_path, **args)
    return downloaded[0], failures, backends


def test_rejects_mismatched_video_metadata_before_download(tmp_path):
    candidate, failures, backends = run(tmp_path, {
        "title": "三立台灣台《針線緣》前導預告", "uploader": "三立台劇 SET Drama",
        "description": "戲劇預告 李燕", "duration": 30, "height": 720, "ext": "mp4",
    })
    assert candidate.status is VideoStatus.SKIPPED
    assert candidate.local_path is None
    assert any(f.code == "video_relevance_rejected" for f in failures)
    assert backends[0].download_calls == 0


def test_unverified_video_metadata_is_not_downloaded_with_news_context(tmp_path):
    candidate, failures, backends = run(tmp_path, {"title": "PV公開", "uploader": "", "description": ""})
    assert candidate.status is VideoStatus.SKIPPED
    assert candidate.local_path is None
    assert any(f.code == "video_relevance_unverified" for f in failures)
    assert backends[0].download_calls == 0


def test_other_work_title_is_not_rescued_by_recommended_target_text(tmp_path):
    candidate, failures, backends = run(tmp_path, {
        "title": "Another game official PV", "uploader": "BA-KU official",
        "description": "Recommended: 百白 game PV", "duration": 30, "height": 720, "ext": "mp4",
    })
    assert candidate.status is VideoStatus.SKIPPED
    assert any(f.code == "video_relevance_rejected" for f in failures)
    assert backends[0].download_calls == 0


def test_video_without_news_context_keeps_legacy_download_behavior(tmp_path):
    candidate, failures, _backends = run(tmp_path, {"title": "Unrelated trailer", "uploader": "TV"}, context=False)
    assert failures == []
    assert candidate.status is VideoStatus.DOWNLOADED
    assert Path(candidate.local_path).is_file()
