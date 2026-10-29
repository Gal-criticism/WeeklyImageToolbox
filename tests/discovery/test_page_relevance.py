from galgame_news.domain import CollectionContext, DiscoveryMethod, NewsItem, SourceRef, SourceType
from galgame_news.discovery.adapters import OfficialHtmlAdapter


class Response:
    status_code = 200
    def __init__(self, url, text): self.url, self.text = url, text


def news():
    return NewsItem(issue_id="255", sequence=5, section="新作", title="BA-KU宣布制作《百白》",
                    body="BA-KU announces its second game.", game_names=["百白"],
                    source_urls=["https://x.com/BA_KU_official/status/123"])


def test_rejects_mismatched_search_hit_page_before_extracting_media():
    html = '<html><head><title>三立电视剧《針線緣》</title></head><body><img src="/poster.jpg"></body></html>'
    adapter = OfficialHtmlAdapter(transport=lambda url, **_: Response(url, html))
    source = SourceRef(url="https://news.example.net/1014220", domain="news.example.net",
        source_type=SourceType.UNVERIFIED, search_title="三立电视剧《針線緣》",
        search_snippet="最新戲劇預告與演員資訊")

    result = adapter.collect(news(), source, CollectionContext())

    assert result.candidates == []
    assert any(f.code == "page_relevance_rejected" for f in result.failures)


def test_ddgs_page_without_search_metadata_is_still_gated_before_gallery_script_fetch():
    html = '''<html><head><title>針線緣 official drama</title>
      <script src="/gallery.js"></script></head><body>
      <main>Drama synopsis and cast</main><footer>推薦：百白 visual novel game</footer>
      <img src="/poster.jpg"></body></html>'''
    calls = []
    adapter = OfficialHtmlAdapter(transport=lambda url, **_: (calls.append(url) or Response(url, html)))
    source = SourceRef(url="https://news.example.net/1014220", domain="news.example.net",
        discovered_via=DiscoveryMethod.DDGS)

    result = adapter.collect(news(), source, CollectionContext())

    assert result.candidates == []
    assert any(f.code == "page_relevance_rejected" for f in result.failures)
    assert calls == [source.url]


def test_html_video_discovery_ignores_recommendation_and_ad_iframes():
    html = '''<html><head><title>百白 BA-KU official game</title></head><body>
      <main><iframe src="https://www.youtube.com/watch?v=MAIN1234567"></iframe></main>
      <aside class="recommend"><iframe src="https://www.youtube.com/watch?v=Q3hxYqAjO8c"></iframe></aside>
      <div id="advert"><iframe src="https://www.youtube.com/watch?v=AD123456789"></iframe></div>
    </body></html>'''
    adapter = OfficialHtmlAdapter(transport=lambda url, **_: Response(url, html))
    source = SourceRef(url="https://ba-ku.example/byaku", domain="ba-ku.example",
        search_title="百白 BA-KU official game")
    result = adapter.collect(news(), source, CollectionContext())

    assert [candidate.video_url for candidate in result.video_candidates] == ["https://www.youtube.com/watch?v=MAIN1234567"]


def test_accepts_short_work_with_vendor_and_explicit_game_context():
    html = '<html><head><title>百白 | BA-KU official game</title></head><body><img src="/pv.jpg"></body></html>'
    adapter = OfficialHtmlAdapter(transport=lambda url, **_: Response(url, html))
    source = SourceRef(url="https://ba-ku.example/byaku", domain="ba-ku.example",
        search_title="百白 BA-KU游戏官网", search_snippet="BA-KU第二作 game")
    result = adapter.collect(news(), source, CollectionContext())
    assert result.candidates


def test_official_page_with_unrelated_primary_identity_cannot_borrow_search_snippet_identity():
    html = '''<html><head><title>Another Visual Novel Official Site</title>
      <meta name="description" content="A separate galgame from another maker"></head>
      <body><main>Unrelated game synopsis</main><img src="/unrelated.jpg"></body></html>'''
    adapter = OfficialHtmlAdapter(transport=lambda url, **_: Response(url, html))
    source = SourceRef(
        url="https://official.example/other", domain="official.example",
        source_type=SourceType.OFFICIAL_SITE, officiality=0.9,
        search_title="百白 BA-KU official game",
        search_snippet="BA-KU 百白 visual novel gallery",
        source_chain=["https://x.com/BA_KU_official/status/123"],
    )

    result = adapter.collect(news(), source, CollectionContext())

    assert result.candidates == []
    assert any(f.code == "page_relevance_rejected" for f in result.failures)


def test_verified_official_product_navigation_chain_accepts_generic_content_title():
    html = '<html><head><title>Artwork Gallery</title></head><body><img src="/cg.jpg"></body></html>'
    adapter = OfficialHtmlAdapter(transport=lambda url, **_: Response(url, html))
    source = SourceRef(
        url="https://official.example/game/gallery", domain="official.example",
        source_type=SourceType.OFFICIAL_SITE, officiality=0.9,
        discovered_via=DiscoveryMethod.SAME_DOMAIN,
        source_chain=["https://x.com/BA_KU_official/status/123"],
        search_title="百白 BA-KU official game",
    )

    result = adapter.collect(news(), source, CollectionContext())

    assert result.candidates