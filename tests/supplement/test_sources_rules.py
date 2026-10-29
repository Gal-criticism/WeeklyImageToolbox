from types import SimpleNamespace
from datetime import datetime, timezone
from galgame_news.domain import NewsItem, ImageCandidate, SourceRef, SourceType, CollectionContext
from galgame_news.supplement.providers import discover_sources, YmgalPicsetAdapter
from galgame_news.supplement.rules import classify_candidate


def news():
    return NewsItem(issue_id="265", sequence=1, section="新作", title="AMBITIOUS MISSION CG 更新", body="", game_names=["AMBITIOUS MISSION"])


def test_discovery_accepts_entries_not_search_pages():
    transport = SimpleNamespace(search=lambda *a, **k: [{"url": "https://www.ymgal.games/search?q=abc"}, {"url": "https://evil.example/co/picset/1"}, {"url": "https://www.ymgal.games/co/picset/123"}])
    found = discover_sources(news(), stage="ymgal", config=None, transport=transport)
    assert [s.url for s in found] == ["https://www.ymgal.games/co/picset/123"]


def test_visited_links_do_not_consume_the_fresh_source_limit():
    n = news()
    visited = {"https://official.example/old1", "https://official.example/old2"}
    n.source_urls = list(visited)
    transport = SimpleNamespace(search=lambda *a, **k: [
        {"url": url} for url in [*sorted(visited), "https://official.example/new1", "https://official.example/new2"]])
    found = discover_sources(n, stage="official", config=None, transport=transport,
                             skip_url=lambda url: url in visited)
    assert [s.url for s in found] == ["https://official.example/new1", "https://official.example/new2"]


def test_ymgal_scope_excludes_sidebar_and_thumbnail():
    url = "https://www.ymgal.games/co/picset/123"
    html = '<h1>AMBITIOUS MISSION</h1><img src="https://cdn.ymgal.games/avatar/a.jpg"><div id="main-picset-warp"><div class="list-box"><div data-background="https://cdn.ymgal.games/topic/content/a.jpg"></div></div><div class="thumbnail-box"><div data-background="https://cdn.ymgal.games/topic/content/b.jpg"></div></div></div><img src="https://cdn.ymgal.games/topic/content/recommended.jpg">'
    client = SimpleNamespace(get=lambda u: SimpleNamespace(status_code=200, url=url, text=html))
    result = YmgalPicsetAdapter(client).collect(news(), SourceRef(url=url, domain="www.ymgal.games", source_type=SourceType.UNVERIFIED), CollectionContext())
    assert [c.image_url for c in result.candidates] == ["https://cdn.ymgal.games/topic/content/a.jpg"]
    assert result.candidates[0].evidence[0].container == "ymgal:picset-main-slider"


def test_gallery_keyword_never_proves_work_identity():
    n = news()
    c = ImageCandidate(news_id=n.id, source_url="https://example.com", image_url="https://example.com/a.jpg", source_type=SourceType.UNVERIFIED, nearby_text="Another game CG gallery", fetched_at=datetime.now(timezone.utc))
    assert classify_candidate(n, c)["supplement_relation"] == "unrelated"
    c.nearby_text = "AMBITIOUS MISSION アフターエピソード 2"
    assert classify_candidate(n, c)["supplement_relation"] == "version_uncertain"
    c.nearby_text = "AMBITIOUS MISSION gallery"
    result = classify_candidate(n, c)
    assert result["supplement_relation"] == "general"
    assert result["supplement_quality"] == "needs_review"
