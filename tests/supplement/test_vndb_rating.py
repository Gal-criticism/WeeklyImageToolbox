from types import SimpleNamespace

from galgame_news.domain import CollectionContext, NewsItem, SourceRef
from galgame_news.localization.vndb import _collect_screenshots
from galgame_news.localization.vndb_client import VNDBClient


def test_vn_query_requests_prefilter_ratings():
    client = VNDBClient(object())
    captured = {}
    client._query = lambda endpoint, payload, key, news_id="": captured.update(payload) or {}
    client.vn("v1")
    fields = captured["fields"]
    assert "sexual" in fields and "violence" in fields and "votecount" in fields


def test_screenshots_with_positive_sexual_rating_skipped_unknown_reviewed():
    news = NewsItem(issue_id="i", sequence=1, section="s", title="Title", body="", game_names=["Game"])
    source = SourceRef(url="https://vndb.org/v1", domain="vndb.org")
    context = CollectionContext(max_candidates=10)
    rows = [{"url":"https://images.vndb.org/sf/1.jpg", "sexual":1},
            {"url":"https://images.vndb.org/sf/2.jpg", "sexual":None}]
    out = []
    _collect_screenshots(out, news, source, [], context, "v1", "confirmed",
                         {"screenshots": rows, "rating": 80, "votecount": 20})
    assert len(out) == 1
    assert out[0].signals["localization_screenshot_sexual"] == "unknown"
    assert out[0].signals["localization_review_only"] is True
    assert out[0].signals["localization_work_rating"] == 80
