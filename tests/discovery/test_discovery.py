from datetime import datetime, timezone

import pytest

from galgame_news.domain import (
    CollectionContext,
    DiscoveryMethod,
    ImageNeed,
    NewsItem,
    ReviewReason,
    SourceRef,
    SourceType,
)


def item(**kwargs):
    data = dict(
        issue_id="259",
        sequence=1,
        section="新作",
        title="《Happy Weekend》主机板制作完成",
        body="HOOKSOFT 公布一张贺图",
        game_names=["Happy Weekend"],
        organizations=["HOOKSOFT"],
        image_need=ImageNeed.EXPLICIT_NEW_IMAGE,
    )
    data.update(kwargs)
    return NewsItem(**data)


class FakeResponse:
    def __init__(self, status_code=200, text="", content=b"", headers=None, url=None):
        self.status_code = status_code
        self.text = text
        self.content = content
        self.headers = headers or {"content-type": "text/html"}
        self.url = url or "https://official.example/news"


def test_official_html_extracts_meta_lazy_srcset_links_and_jsonld():
    from galgame_news.discovery.adapters import OfficialHtmlAdapter

    html = """
    <html><head>
      <meta property="og:image" content="/og.jpg">
      <meta name="twitter:image" content="https://official.example/tw.jpg">
      <script type="application/ld+json">{"image": ["/jsonld.jpg"]}</script>
    </head><body>
      <img src="/normal.jpg" data-src="/lazy.jpg" srcset="/small.jpg 400w, /large.jpg 1200w">
      <a href="/linked.png">image</a>
    </body></html>
    """
    adapter = OfficialHtmlAdapter(transport=lambda url, **_: FakeResponse(text=html, url=url))
    result = adapter.collect(item(), SourceRef(url="https://official.example/news", domain="official.example", source_type=SourceType.OFFICIAL_SITE), CollectionContext())
    urls = {candidate.image_url for candidate in result.candidates}
    assert {"https://official.example/og.jpg", "https://official.example/tw.jpg", "https://official.example/jsonld.jpg", "https://official.example/normal.jpg", "https://official.example/lazy.jpg", "https://official.example/large.jpg", "https://official.example/linked.png"} <= urls


def test_gallery_anchor_prefers_full_image_over_thumbnail_and_picture_source():
    from galgame_news.discovery.adapters import OfficialHtmlAdapter

    html = """
    <picture><source srcset="/hero.webp" type="image/webp"><img src="/hero-thumb.jpg"></picture>
    <a href="/gallery06.jpg" data-lightbox="simple-group"><img src="/gallery06s.jpg"></a>
    """
    adapter = OfficialHtmlAdapter(transport=lambda url, **_: FakeResponse(text=html, url=url))
    result = adapter.collect(item(), SourceRef(url="https://official.example/gallery", domain="official.example", source_type=SourceType.OFFICIAL_SITE), CollectionContext())
    urls = [candidate.image_url for candidate in result.candidates]
    assert "https://official.example/gallery06.jpg" in urls
    assert "https://official.example/gallery06s.jpg" not in urls
    assert "https://official.example/hero.webp" in urls


def test_gallery_page_collects_more_than_five_original_images():
    from galgame_news.discovery.adapters import OfficialHtmlAdapter

    html = "".join(
        f'<a href="/gallery/cg{index:02d}.jpg"><img src="/gallery/thumb/cg{index:02d}s.jpg"></a>'
        for index in range(1, 7)
    )
    adapter = OfficialHtmlAdapter(transport=lambda url, **_: FakeResponse(text=html, url=url))
    result = adapter.collect(
        item(),
        SourceRef(url="https://official.example/gallery", domain="official.example", source_type=SourceType.OFFICIAL_SITE),
        CollectionContext(max_candidates=20),
    )
    assert [candidate.image_url for candidate in result.candidates] == [
        f"https://official.example/gallery/cg{index:02d}.jpg" for index in range(1, 7)
    ]


def test_wix_transformed_thumbnail_is_upgraded_to_original_media_url():
    from galgame_news.discovery.adapters import OfficialHtmlAdapter

    transformed = "https://static.wixstatic.com/media/abc123~mv2.png/v1/fill/w_485,h_273,q_90/abc123~mv2.png"
    html = f"<img src=\"{transformed}\">"
    adapter = OfficialHtmlAdapter(transport=lambda url, **_: FakeResponse(text=html, url=url))
    result = adapter.collect(item(), SourceRef(url="https://official.example/wix-gallery", domain="official.example", source_type=SourceType.OFFICIAL_SITE), CollectionContext())
    assert result.candidates[0].image_url == "https://static.wixstatic.com/media/abc123~mv2.png"


def test_direct_image_adapter_returns_candidate_without_fetching_html():
    from galgame_news.discovery.adapters import DirectImageAdapter

    result = DirectImageAdapter().collect(item(), SourceRef(url="https://cdn.example/cg.jpg", domain="cdn.example", source_type=SourceType.DIRECT_IMAGE), CollectionContext())
    assert [c.image_url for c in result.candidates] == ["https://cdn.example/cg.jpg"]


def test_official_html_accepts_injected_client():
    from galgame_news.discovery.adapters import OfficialHtmlAdapter

    class Client:
        def __init__(self):
            self.urls = []

        def get(self, url):
            self.urls.append(url)
            return FakeResponse(text='<img src="/gallery/cg01.jpg">', url=url)

    client = Client()
    adapter = OfficialHtmlAdapter(client=client)
    result = adapter.collect(
        item(),
        SourceRef(url="https://official.example/gallery", domain="official.example", source_type=SourceType.OFFICIAL_SITE),
        CollectionContext(),
    )

    assert client.urls == ["https://official.example/gallery"]
    assert result.candidates[0].image_url == "https://official.example/gallery/cg01.jpg"


def test_x_without_token_keeps_source_and_requires_manual_review():
    from galgame_news.discovery.adapters import XAdapter

    source = SourceRef(url="https://x.com/AirVelo2026/status/1", domain="x.com", source_type=SourceType.OFFICIAL_X)
    result = XAdapter(token=None, public_transport=lambda url, **_: FakeResponse(text="", url=url)).collect(item(), source, CollectionContext())
    assert not result.candidates
    assert result.manual_review_reasons
    assert source.requires_review is True


def test_http_client_rejects_private_and_rechecks_redirects():
    from galgame_news.discovery.http import SafeHttpClient, UnsafeUrlError

    client = SafeHttpClient(transport=lambda url, **_: FakeResponse(url="http://127.0.0.1/secret"))
    with pytest.raises(UnsafeUrlError):
        client.get("http://127.0.0.1/image.jpg")
    with pytest.raises(UnsafeUrlError):
        client.get("https://public.example/image.jpg")


def test_http_client_retries_429_then_succeeds():
    from galgame_news.discovery.http import SafeHttpClient

    calls = []

    def transport(url, **kwargs):
        calls.append(url)
        return FakeResponse(status_code=429 if len(calls) < 3 else 200, content=b"ok", headers={"content-type": "text/plain"}, url=url)

    response = SafeHttpClient(transport=transport, max_retries=3).get("https://public.example/page")
    assert response.status_code == 200
    assert len(calls) == 3


def test_http_client_retries_www_when_certificate_hostname_mismatches():
    from galgame_news.discovery.http import SafeHttpClient

    calls = []

    def transport(url, **kwargs):
        calls.append(url)
        if url == "https://official.example/game":
            raise RuntimeError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: Hostname mismatch")
        return FakeResponse(url=url, text="<html>ok</html>")

    response = SafeHttpClient(transport=transport, max_retries=1).get("https://official.example/game")
    assert response.url == "https://www.official.example/game"
    assert calls == ["https://official.example/game", "https://www.official.example/game"]


def test_source_resolver_order_document_history_same_domain_then_search():
    from galgame_news.discovery.resolver import DefaultSourceResolver

    history = [SourceRef(url="https://history.example/game", domain="history.example", source_type=SourceType.OFFICIAL_SITE, discovered_via=DiscoveryMethod.HISTORY, officiality=1.0)]
    search_calls = []

    resolver = DefaultSourceResolver(
        history_lookup=lambda news: history,
        same_domain_lookup=lambda source, news: [SourceRef(url="https://history.example/gallery", domain="history.example", source_type=SourceType.OFFICIAL_SITE, discovered_via=DiscoveryMethod.SAME_DOMAIN, officiality=1.0)],
        search_provider=lambda news: (search_calls.append(news.id) or [SourceRef(url="https://search.example/result", domain="search.example", source_type=SourceType.UNVERIFIED, discovered_via=DiscoveryMethod.DDGS)]),
    )
    result = resolver.resolve(item(source_urls=["https://doc.example/news", "https://doc.example/news#fragment"]))
    assert [source.url for source in result][:3] == ["https://doc.example/news", "https://history.example/game", "https://history.example/gallery"]
    assert result[-1].officiality == 0.0
    assert search_calls


def test_dynamic_and_age_gate_sources_are_reviewable():
    from galgame_news.discovery.adapters import OfficialHtmlAdapter

    html = "<html><head><meta name='age-verification' content='required'></head><body><script>dynamic()</script></body></html>"
    source = SourceRef(url="https://official.example/age", domain="official.example", source_type=SourceType.OFFICIAL_SITE)
    result = OfficialHtmlAdapter(transport=lambda url, **_: FakeResponse(text=html, url=url)).collect(item(), source, CollectionContext())
    assert not result.candidates
    assert source.requires_review


def test_official_html_clicks_explicit_18_plus_cookie_gate_and_collects_images():
    from galgame_news.discovery.adapters import OfficialHtmlAdapter
    from galgame_news.discovery.http import SafeHttpClient

    url = "https://circus.example/product/game/"
    gate = """
    <meta charset="utf-8"><a id="confirm-yes">はい、18才以上です。</a>
    <script>Cookies.set('PermitRate', '18', { expires: 180 });</script>
    """
    confirmed = '<html><title>Gallery</title><img src="/gallery/cg01.jpg"></html>'
    requests = []

    def transport(request_url, *, headers, **_kwargs):
        requests.append((request_url, headers.get("cookie", "")))
        return FakeResponse(text=confirmed if "PermitRate=18" in headers.get("cookie", "") else gate, url=request_url)

    client = SafeHttpClient(
        transport=transport,
        resolver=lambda _host: ["93.184.216.34"],
        max_retries=1,
    )
    source = SourceRef(url=url, domain="circus.example", source_type=SourceType.OFFICIAL_SITE)
    result = OfficialHtmlAdapter(client=client).collect(item(), source, CollectionContext())

    assert [cookie for _, cookie in requests] == ["", "PermitRate=18"]
    assert [candidate.image_url for candidate in result.candidates] == ["https://circus.example/gallery/cg01.jpg"]
    assert result.manual_review_reasons == []
    assert source.requires_review is False
    assert result.candidates[0].signals["age_gate_confirmed"] is True


def test_default_resolver_discovers_same_domain_gallery_using_configured_depth():
    from galgame_news.discovery.resolver import DefaultSourceResolver

    pages = {
        "https://official.example/product": '<a href="/gallery">Gallery</a><a href="https://other.example/gallery">other</a>',
        "https://official.example/gallery": '<a href="/special/cg">CG Special</a>',
    }
    resolver = DefaultSourceResolver(same_domain_depth=2, same_domain_transport=lambda url, **_: FakeResponse(text=pages[url], url=url), search_provider=lambda _news: [])
    result = resolver.resolve(item(source_urls=["https://official.example/product"]))
    assert [source.url for source in result] == ["https://official.example/product", "https://official.example/gallery", "https://official.example/special/cg"]


def test_resolver_discovers_gallery_modal_iframe_pages():
    from galgame_news.discovery.resolver import DefaultSourceResolver

    html = '''
    <div class="cgwindow cg00" data-izimodal-iframeurl="/pages/gallery/00.html"></div>
    <iframe src="/pages/gallery/01.html"></iframe>
    '''
    resolver = DefaultSourceResolver(
        same_domain_depth=1,
        same_domain_transport=lambda url, **_: FakeResponse(text=html, url=url),
        search_provider=lambda _news: [],
    )
    result = resolver.resolve(item(source_urls=["https://official.example/game"]))
    assert [source.url for source in result] == [
        "https://official.example/game",
        "https://official.example/pages/gallery/00.html",
        "https://official.example/pages/gallery/01.html",
    ]


def test_document_link_trust_does_not_mark_known_third_party_as_official():
    from galgame_news.discovery.resolver import DefaultSourceResolver

    resolver = DefaultSourceResolver(search_provider=lambda _news: [])
    result = resolver.resolve(item(source_urls=["https://vndb.org/v123", "https://store.steampowered.com/app/1"]))
    assert result[0].source_type is SourceType.UNVERIFIED
    assert result[0].officiality < 0.5
    assert result[1].source_type is SourceType.STEAM
    assert result[1].officiality < 1.0


def test_ddgs_provider_accepts_current_href_and_body_field_names():
    from galgame_news.discovery.search import DDGSSearchProvider

    provider = DDGSSearchProvider(
        search_fn=lambda query, **_: [
            {"title": "Official game site", "href": "https://official.example/game", "body": "Gallery and news"}
        ]
    )
    result = provider.search(item())
    assert [source.url for source in result] == ["https://official.example/game"]
    assert result[0].source_type is SourceType.UNVERIFIED


def test_ddgs_provider_expands_explicit_cg_updates_with_gallery_search_terms():
    from galgame_news.discovery.search import DDGSSearchProvider

    calls = []

    def search(query, **_):
        calls.append(query)
        return [{"title": query, "href": f"https://search.example/result-{len(calls)}", "body": "CG gallery"}]

    provider = DDGSSearchProvider(search_fn=search, max_results=5)
    result = provider.search(item(
        title="《One Night After》CG更新",
        game_names=["One Night After"],
        body="官网更新了一组 CG 图片。",
    ))

    assert calls[0] == "One Night After"
    assert any("CG" in query and "gallery" in query.casefold() for query in calls[1:])
    assert any("画像" in query for query in calls[1:])
    assert len(result) >= 3


def test_fallback_search_combines_providers_for_explicit_cg_updates():
    from galgame_news.discovery.search import FallbackSearchProvider

    class Provider:
        def __init__(self, prefix):
            self.prefix = prefix
            self.calls = 0

        def search(self, _news):
            self.calls += 1
            return [SourceRef(
                url=f"https://{self.prefix}.example/result-{i}",
                domain=f"{self.prefix}.example",
                source_type=SourceType.UNVERIFIED,
                discovered_via=DiscoveryMethod.DDGS,
            ) for i in range(3)]

    brave, ddgs = Provider("brave"), Provider("ddgs")
    provider = FallbackSearchProvider(brave=brave, ddgs=ddgs, max_results=2)
    result = provider.search(item(title="《Happy Weekend》CG更新", game_names=["Happy Weekend"]))

    assert brave.calls == ddgs.calls == 1
    assert len(result) == 6


def test_default_fallback_search_does_not_use_brave_environment_key(monkeypatch):
    from galgame_news.discovery.search import FallbackSearchProvider

    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "legacy-key")
    ddgs = type("DDGS", (), {"search": lambda self, _news: []})()
    provider = FallbackSearchProvider(ddgs=ddgs)
    assert provider.brave is None


def test_ddgs_provider_passes_bounded_timeout_to_client(monkeypatch):
    from types import SimpleNamespace
    import sys
    from galgame_news.discovery.search import DDGSSearchProvider

    seen = {}

    class FakeDDGS:
        def __init__(self, **kwargs):
            seen.update(kwargs)

        def text(self, query, **kwargs):
            return [{"href": "https://official.example/game", "title": "game"}]

    monkeypatch.setitem(sys.modules, "ddgs", SimpleNamespace(DDGS=FakeDDGS))
    provider = DDGSSearchProvider(timeout=2.5, max_results=3)
    result = provider.search(item())

    assert result[0].url == "https://official.example/game"
    assert seen["timeout"] == 2.5


def test_wix_embedded_dynamic_data_yields_original_media_images(monkeypatch):
    from galgame_news.discovery.adapters import OfficialHtmlAdapter
    import socket

    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [(socket.AF_INET, 0, 0, "", ("8.8.8.8", 0))])

    html = r'''<script id="wix-warmup-data" type="application/json">{"gallery":{"items":[{"src":"https:\/\/static.wixstatic.com\/media\/one~mv2.jpg\/v1\/fill\/w_400,h_200\/one.jpg"},{"src":"wix:image://v1/two~mv2.png/two.png#originWidth=1600&originHeight=900"}]}}</script>'''
    adapter = OfficialHtmlAdapter(transport=lambda url, **_: FakeResponse(text=html, url=url))
    result = adapter.collect(item(), SourceRef(url="https://site.wixsite.com/game", domain="site.wixsite.com", source_type=SourceType.OFFICIAL_SITE), CollectionContext())
    assert {candidate.image_url for candidate in result.candidates} == {"https://static.wixstatic.com/media/one~mv2.jpg", "https://static.wixstatic.com/media/two~mv2.png"}


def test_x_without_token_extracts_public_meta_image_and_still_requires_review():
    from galgame_news.discovery.adapters import XAdapter

    html = '<meta property="og:image" content="https://pbs.twimg.com/media/cg.jpg">'
    source = SourceRef(url="https://x.com/studio/status/1", domain="x.com", source_type=SourceType.OFFICIAL_X)
    adapter = XAdapter(token=None, public_transport=lambda url, **_: FakeResponse(text=html, url=url))
    result = adapter.collect(item(), source, CollectionContext())
    assert [candidate.image_url for candidate in result.candidates] == ["https://pbs.twimg.com/media/cg.jpg?name=orig"]
    assert ReviewReason.X_SOURCE in result.manual_review_reasons


def test_x_public_metadata_rejects_avatar_and_accepts_only_post_media_paths():
    from galgame_news.discovery.adapters import XAdapter
    html = "".join([
        '<meta property="og:image" content="https://pbs.twimg.com/profile_images/123/avatar.jpg">',
        '<meta property="twitter:image" content="https://pbs.twimg.com/media/post.jpg?name=orig">',
        '<meta property="og:image:url" content="https://pbs.twimg.com/card_img/456/card.jpg">',
        '<meta property="og:image" content="https://abs.twimg.com/icons/favicon.ico">',
    ])
    source = SourceRef(url="https://x.com/studio/status/1", domain="x.com", source_type=SourceType.OFFICIAL_X)
    result = XAdapter(token=None, public_transport=lambda url, **_: FakeResponse(text=html, url=url)).collect(item(), source, CollectionContext())
    assert {candidate.image_url for candidate in result.candidates} == {
        "https://pbs.twimg.com/media/post.jpg?name=orig",
        "https://pbs.twimg.com/card_img/456/card.jpg",
    }


def test_wix_pseudo_quality_url_is_ignored_when_original_media_exists(monkeypatch):
    from galgame_news.discovery.adapters import OfficialHtmlAdapter
    import socket

    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [(socket.AF_INET, 0, 0, "", ("8.8.8.8", 0))])
    html = '''<img src="https://site.wixsite.com/q_90/image.jpg"><img src="https://static.wixstatic.com/media/abc~mv2.jpg/v1/fill/w_400,h_300,q_90/abc.jpg">'''
    adapter = OfficialHtmlAdapter(transport=lambda url, **_: FakeResponse(text=html, url=url))
    result = adapter.collect(item(), SourceRef(url="https://site.wixsite.com/gallery", domain="site.wixsite.com", source_type=SourceType.OFFICIAL_SITE), CollectionContext())
    assert [candidate.image_url for candidate in result.candidates] == ["https://static.wixstatic.com/media/abc~mv2.jpg"]


def test_common_cdn_thumbnail_urls_are_upgraded_to_originals():
    from galgame_news.discovery.adapters import DirectImageAdapter, OfficialHtmlAdapter

    html = '''
    <img src="https://cdn.example/game-300x169.jpg">
    <img src="https://pbs.twimg.com/media/cg.jpg?format=jpg&name=small">
    <img src="https://images.ctfassets.net/game/cg.jpg?w=640&h=360&q=80">
    '''
    adapter = OfficialHtmlAdapter(transport=lambda url, **_: FakeResponse(text=html, url=url))
    result = adapter.collect(item(), SourceRef(url="https://official.example/gallery", domain="official.example", source_type=SourceType.OFFICIAL_SITE), CollectionContext())
    urls = {candidate.image_url for candidate in result.candidates}
    assert "https://cdn.example/game.jpg" in urls
    assert "https://pbs.twimg.com/media/cg.jpg?format=jpg&name=orig" in urls
    assert "https://images.ctfassets.net/game/cg.jpg" in urls

    direct = DirectImageAdapter().collect(
        item(),
        SourceRef(url="https://cdn.example/game-640x360.jpg", domain="cdn.example", source_type=SourceType.DIRECT_IMAGE),
        CollectionContext(),
    )
    assert direct.candidates[0].image_url == "https://cdn.example/game.jpg"


def test_html_discovers_background_data_iframe_and_json_images():
    from galgame_news.discovery.adapters import OfficialHtmlAdapter
    html = '''
    <div style="background-image: url('/gallery/bg01.jpg')" data-background="/gallery/bg02.jpg" data-bg="/gallery/bg03.jpg" data-image="/gallery/bg04.jpg"></div>
    <iframe src="/gallery/frame.html"></iframe><div data-iframe="/gallery/embed.html" data-src="/gallery/data.html"></div>
    <script type="application/json">{"image":"https://official.example/gallery/json.jpg"}</script>
    <script id="__NEXT_DATA__" type="application/json">{"props":{"pageProps":{"image":"/gallery/next.jpg"}}}</script>
    '''
    adapter = OfficialHtmlAdapter(transport=lambda url, **_: FakeResponse(text=html, url=url))
    result = adapter.collect(item(), SourceRef(url="https://official.example/gallery", domain="official.example", source_type=SourceType.OFFICIAL_SITE), CollectionContext(max_candidates=20))
    urls = {candidate.image_url for candidate in result.candidates}
    assert {"https://official.example/gallery/bg01.jpg", "https://official.example/gallery/bg02.jpg", "https://official.example/gallery/bg03.jpg", "https://official.example/gallery/bg04.jpg", "https://official.example/gallery/json.jpg", "https://official.example/gallery/next.jpg"} <= urls
    assert "https://official.example/gallery/frame.html" not in urls


def test_search_official_result_triggers_same_domain_gallery_discovery():
    from galgame_news.discovery.resolver import DefaultSourceResolver
    pages = {"https://official.example/home": '<a href="/gallery">Gallery</a>'}
    resolver = DefaultSourceResolver(
        same_domain_depth=1,
        same_domain_transport=lambda url, **_: FakeResponse(text=pages[url], url=url),
        search_provider=lambda _news: [SourceRef(url="https://official.example/home", domain="official.example", source_type=SourceType.OFFICIAL_SITE, officiality=0.9, discovered_via=DiscoveryMethod.DDGS)],
    )
    result = resolver.resolve(item(source_urls=[]))
    assert [source.url for source in result] == ["https://official.example/home", "https://official.example/gallery"]


def test_official_crawl_follows_exact_game_portal_then_graphic_chapter_and_keeps_provenance():
    from galgame_news.discovery.resolver import DefaultSourceResolver

    pages = {
        "https://official.example/": '<a href="/unrelated">Other game</a><a href="/product/happy">Happy Weekend</a>',
        "https://official.example/product/happy": '<a href="/news">News</a><a href="/chapters#GRAPHIC"><img alt="GRAPHIC"></a>',
        "https://official.example/chapters#GRAPHIC": "<p>chapter index</p>",
    }
    resolver = DefaultSourceResolver(
        same_domain_depth=3,
        same_domain_transport=lambda url, **_: FakeResponse(text=pages[url], url=url),
        search_provider=lambda _news: [],
    )
    result = resolver.resolve(item(source_urls=["https://official.example/"]))
    discovered = [source for source in result if source.discovered_via is DiscoveryMethod.SAME_DOMAIN]

    assert [source.url for source in discovered] == [
        "https://official.example/product/happy",
        "https://official.example/chapters#GRAPHIC",
    ]
    assert discovered[0].root_url == "https://official.example/"
    assert discovered[0].parent_url == "https://official.example/"
    assert discovered[1].root_url == "https://official.example/"
    assert discovered[1].parent_url == "https://official.example/product/happy"


def test_homepage_portal_matches_canonical_parent_work_from_chapter_game_name():
    from galgame_news.discovery.resolver import DefaultSourceResolver

    pages = {
        "https://official.example/": '<a href="/kanade">花鐘カナデ＊グラム</a><a href="/other">別作品</a>',
        "https://official.example/kanade": '<a href="/kanade/graphic">GRAPHIC</a>',
        "https://official.example/kanade/graphic": "<p>graphic</p>",
    }
    news = item(game_names=["Chapter:4 花鐘カナデ＊グラム"], source_urls=["https://official.example/"])
    resolver = DefaultSourceResolver(
        same_domain_depth=3,
        same_domain_transport=lambda url, **_: FakeResponse(text=pages[url], url=url),
        search_provider=lambda _news: [],
    )

    discovered = [source for source in resolver.resolve(news) if source.discovered_via is DiscoveryMethod.SAME_DOMAIN]

    assert [source.url for source in discovered] == [
        "https://official.example/kanade",
        "https://official.example/kanade/graphic",
    ]


def test_homepage_portal_matches_parent_first_chapter_name_without_decorative_star():
    from galgame_news.discovery.resolver import DefaultSourceResolver

    pages = {
        "https://official.example/": '<a href="/kanade">花鐘カナデグラム</a>',
        "https://official.example/kanade": '<a href="/kanade/graphic">GRAPHIC</a>',
        "https://official.example/kanade/graphic": "<p>graphic</p>",
    }
    news = item(
        game_names=["花鐘カナデ＊グラム Chapter:4 綾世奏"],
        source_urls=["https://official.example/"],
    )
    resolver = DefaultSourceResolver(
        same_domain_depth=3,
        same_domain_transport=lambda url, **_: FakeResponse(text=pages[url], url=url),
        search_provider=lambda _news: [],
    )

    discovered = [source for source in resolver.resolve(news) if source.discovered_via is DiscoveryMethod.SAME_DOMAIN]

    assert [source.url for source in discovered] == [
        "https://official.example/kanade",
        "https://official.example/kanade/graphic",
    ]


def test_official_product_portal_follows_its_numbered_chapters_only():
    from galgame_news.discovery.resolver import DefaultSourceResolver

    pages = {
        "https://official.example/": '<a href="/product/prj06/index.html">花鐘カナデグラム</a>',
        "https://official.example/product/prj06/index.html": (
            '<a href="/product/prj06/chapter4/">Chapter:4 -綾世奏-</a>'
            '<a href="/product/prj06/chapter4">Chapter:4 -綾世奏-</a>'
            '<a href="/product/other/chapter1/">Chapter:1 -Other-</a>'
            '<a href="/product/prj06/chapter4/?id=countdown&no=3">Chapter:4 countdown</a>'
            '<div data-src="/product/prj06/chapter4/?id=countdown&no=3"></div>'
        ),
        "https://official.example/product/prj06/chapter4/": '<a href="#GRAPHIC">GRAPHIC</a>',
    }
    news = item(game_names=["花鐘カナデ＊グラム Chapter:4 綾世奏"], source_urls=["https://official.example/"])
    resolver = DefaultSourceResolver(
        same_domain_depth=3,
        same_domain_transport=lambda url, **_: FakeResponse(text=pages[url], url=url),
        search_provider=lambda _news: [],
    )

    discovered = [source.url for source in resolver.resolve(news) if source.discovered_via is DiscoveryMethod.SAME_DOMAIN]
    assert discovered == [
        "https://official.example/product/prj06/index.html",
        "https://official.example/product/prj06/chapter4/",
        "https://official.example/product/prj06/chapter4/#GRAPHIC",
    ]


def test_same_domain_crawl_does_not_emit_gallery_images_as_pages():
    from galgame_news.discovery.resolver import DefaultSourceResolver

    image_links = "".join(f'<a href="/game/gallery/cg{i:03}.jpg">CG</a>' for i in range(100))
    page_links = "".join(f'<a href="/game/gallery/p{i:03}.html">Gallery {i}</a>' for i in range(30))
    pages = {"https://official.example/game/": image_links + page_links}
    news = item(game_names=["Happy Weekend"], source_urls=["https://official.example/game/"])
    resolver = DefaultSourceResolver(
        same_domain_depth=1,
        same_domain_transport=lambda url, **_: FakeResponse(text=pages[url], url=url),
        search_provider=lambda _news: [],
    )
    discovered = [source.url for source in resolver.resolve(news) if source.discovered_via is DiscoveryMethod.SAME_DOMAIN]
    assert len(discovered) <= 16
    assert all(not url.endswith(".jpg") for url in discovered)


def test_gallery_dom_section_marks_only_images_inside_section():
    from galgame_news.discovery.adapters import OfficialHtmlAdapter

    html = '''<div id="GRAPHIC"><a href="/assets/full.jpg"><img src="/thumb.jpg" alt="Kanade CG"></a></div>
    <div id="news"><a href="/assets/news.jpg"><img src="/thumb-news.jpg" alt="News thumbnail"></a></div>'''
    source = SourceRef(url="https://official.example/game#GRAPHIC", domain="official.example", source_type=SourceType.OFFICIAL_SITE)
    result = OfficialHtmlAdapter(transport=lambda url, **_: FakeResponse(text=html, url=url)).collect(item(), source, CollectionContext())

    by_url = {candidate.image_url.rsplit("/", 1)[-1]: candidate for candidate in result.candidates}
    assert by_url["full.jpg"].signals.get("gallery_path") is True
    assert by_url["full.jpg"].image_alt == "Kanade CG"
    assert by_url["news.jpg"].signals.get("gallery_path") is not True


def test_generic_campaign_graphic_css_does_not_mark_a_quiz_as_cg_gallery():
    from galgame_news.discovery.adapters import OfficialHtmlAdapter

    html = '''<section id="special"><figure class="campaign__graphic"><img src="/images/quiz.jpg"></figure></section>
    <section id="graphic"><a href="/images/cg-full.jpg"><img src="/images/cg-thumb.jpg"></a></section>'''
    source = SourceRef(url="https://official.example/game", domain="official.example", source_type=SourceType.OFFICIAL_SITE)
    result = OfficialHtmlAdapter(transport=lambda url, **_: FakeResponse(text=html, url=url)).collect(item(), source, CollectionContext())
    by_name = {candidate.image_url.rsplit("/", 1)[-1]: candidate for candidate in result.candidates}
    assert by_name["quiz.jpg"].signals.get("gallery_path") is not True
    assert by_name["cg-full.jpg"].signals.get("gallery_path") is True


def test_fragment_section_scopes_gallery_evidence_on_linked_full_image():
    from galgame_news.discovery.adapters import OfficialHtmlAdapter

    html = '''<div id="GALLERY"><a href="/images/chapter.jpg"><img src="/thumb/chapter.jpg" alt="Chapter CG"></a></div>
    <div id="news"><a href="/images/news.jpg"><img src="/thumb/news.jpg" alt="News"></a></div>'''
    source = SourceRef(url="https://official.example/game#GALLERY", domain="official.example", source_type=SourceType.OFFICIAL_SITE)
    result = OfficialHtmlAdapter(transport=lambda url, **_: FakeResponse(text=html, url=url)).collect(item(), source, CollectionContext())

    by_url = {candidate.image_url.rsplit("/", 1)[-1]: candidate for candidate in result.candidates}
    assert by_url["chapter.jpg"].signals.get("gallery_path") is True
    assert by_url["chapter.jpg"].signals.get("alt") == "Chapter CG"
    assert by_url["news.jpg"].signals.get("gallery_path") is not True


def test_official_crawl_fetches_at_most_sixteen_pages_per_news_item():
    from galgame_news.discovery.resolver import DefaultSourceResolver

    requests = []

    def transport(url, **_):
        requests.append(url)
        links = "".join(f'<a href="/gallery/{index}">Gallery</a>' for index in range(20))
        return FakeResponse(text=links, url=url)

    resolver = DefaultSourceResolver(same_domain_depth=3, same_domain_transport=transport, search_provider=lambda _news: [])
    resolver.resolve(item(source_urls=["https://official.example/0"]))

    assert len(requests) <= 16
