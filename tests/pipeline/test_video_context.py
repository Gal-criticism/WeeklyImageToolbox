from galgame_news.config import load_config
from galgame_news.domain import NewsItem
from galgame_news.pipeline.runner import PipelineRunner


def test_default_video_downloader_receives_current_news(monkeypatch, tmp_path):
    calls = []

    class Downloader:
        def __init__(self, *_args, **_kwargs):
            pass

        def download(self, candidates, output_dir, *, news_item=None):
            calls.append((candidates, output_dir, news_item))
            return [], []

    monkeypatch.setattr("galgame_news.video.VideoDownloader", Downloader)
    runner = PipelineRunner(config=load_config())
    news = NewsItem(issue_id="255", sequence=5, section="new", title="A", body="", game_names=["A"])

    runner._download_videos([], tmp_path, news_item=news)

    assert calls == [([], tmp_path, news)]


def test_injected_legacy_video_downloader_keeps_its_interface(tmp_path):
    calls = []

    class LegacyDownloader:
        def download(self, candidates, output_dir):
            calls.append((candidates, output_dir))
            return [], []

    runner = PipelineRunner(config=load_config(), video_downloader_factory=lambda _config: LegacyDownloader())
    news = NewsItem(issue_id="255", sequence=5, section="new", title="A", body="", game_names=["A"])

    runner._download_videos([], tmp_path, news_item=news)

    assert calls == [([], tmp_path)]
