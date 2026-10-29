from galgame_news.domain import EventType, ImageNeed, NewsItem, SearchResult
from galgame_news.discovery.search import DDGSSearchProvider, _search_queries
from galgame_news.discovery.relevance import matches_work, news_relevance


def baaku_item():
    return NewsItem(
        issue_id="255", sequence=5, section="新作",
        title="BA-KU宣布制作《百白》",
        body="本周，BA-KU宣布制作第二作《百白》，更多信息尚未公开。\nhttps://x.com/BA_KU_official/status/2085153886055137749",
        game_names=["百白"], organizations=[], keywords=["百白", "公开"],
        image_need=ImageNeed.EXPLICIT_NEW_IMAGE,
    )


def test_short_work_name_does_not_admit_unrelated_news_page():
    item = baaku_item()
    provider = DDGSSearchProvider(search_fn=lambda *_args, **_kwargs: [SearchResult(
        url="https://star.setn.com/news/1014220",
        title="三立电视剧《針線緣》预告",
        snippet="最新戏剧预告与演员资讯。",
    )])

    assert provider.search(item) == []


def test_short_work_name_requires_work_plus_vendor_or_game_context():
    item = baaku_item()
    vendor_result = SearchResult(
        url="https://store.example/baaku/byaku", title="百白 | BA-KU",
        snippet="BA-KU 第二作游戏官网。",
    )
    provider = DDGSSearchProvider(search_fn=lambda *_args, **_kwargs: [vendor_result])
    assert [source.url for source in provider.search(item)] == [vendor_result.url]

    person_result = SearchResult(
        url="https://people.example/byaku", title="百白人物介绍",
        snippet="演员访谈与个人资料。",
    )
    provider = DDGSSearchProvider(search_fn=lambda *_args, **_kwargs: [person_result])
    assert provider.search(item) == []


def test_long_same_title_without_game_identity_is_not_admitted():
    item = NewsItem(issue_id="x", sequence=1, section="新作", title="《The Night》発表", body="",
                    game_names=["The Night"])
    assert not news_relevance(item, "The Night TV drama starring Alice")
    assert not news_relevance(item, "The Night biography by James Gamez")
    assert news_relevance(item, "The Night visual novel official site")


def test_recommendation_copy_cannot_supply_game_context_for_an_unrelated_page():
    item = baaku_item()
    assert not news_relevance(item, "針線緣 劇情介紹 | Recommended: 百白 game PV")


def test_query_adds_vendor_context_inferred_from_news_title_and_source_url():
    queries = _search_queries(baaku_item())

    assert queries
    assert any("BA-KU" in query for query in queries)
    assert all("百白" in query for query in queries)


def test_specific_work_title_and_legitimate_cg_or_goods_results_remain_usable():
    item = NewsItem(
        issue_id="259", sequence=1, section="新作", title="《One Night After》CG更新",
        body="官网更新了一组 CG 图片。", game_names=["One Night After"],
        image_need=ImageNeed.EXPLICIT_NEW_IMAGE,
    )
    result = SearchResult(
        url="https://official.example/gallery", title="One Night After Official Site",
        snippet="Game CG gallery",
    )
    provider = DDGSSearchProvider(search_fn=lambda *_args, **_kwargs: [result])
    assert [source.url for source in provider.search(item)] == [result.url]


def test_goods_queries_keep_vendor_and_product_terms():
    item = NewsItem(
        issue_id="259", sequence=1, section="z-周边", title="Happy Weekend抱枕发售",
        body="商品开始预约。", game_names=["Happy Weekend"],
        organizations=["HOOKSOFT"], event_type=EventType.GOODS,
    )

    queries = _search_queries(item)
    assert queries and all("Happy Weekend" in query and "HOOKSOFT" in query for query in queries)
    assert all("商品" in query for query in queries)


def test_work_matching_respects_cjk_and_latin_boundaries():
    chinese = baaku_item()
    japanese = NewsItem(
        issue_id="255", sequence=6, section="新作", title="《星の終わり》",
        body="", game_names=["星の終わり"],
    )
    latin = NewsItem(
        issue_id="259", sequence=1, section="新作", title="AIR",
        body="", game_names=["AIR"],
    )

    assert matches_work(chinese, "百白 Official")
    assert not matches_work(chinese, "百白語 Official")
    assert matches_work(japanese, "星の終わり 公式サイト")
    assert not matches_work(japanese, "星の終わり方の紹介")
    assert matches_work(latin, "AIR Official Site")
    assert not matches_work(latin, "FAIRY official site")
    assert news_relevance(latin, "AIR official visual novel game")
