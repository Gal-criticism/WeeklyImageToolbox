from types import SimpleNamespace

import pytest

from galgame_news.discovery.goods_context import association, item_identity, is_goods_news, unwrap_goods_url
from galgame_news.discovery.goods_service import GoodsSourceService
from galgame_news.domain import CollectionContext, EventType, NewsItem, SourceRef, SourceType


def news(**changes):
    values = dict(issue_id="265", sequence=13, section="周边", title="《Work Alpha》ASMR商品公开",
        body="Circle A公开商品", game_names=["Work Alpha"], organizations=["Circle A"],
        event_type=EventType.GOODS, source_urls=["https://eeo.today/store/101/products/detail/325773"])
    return NewsItem(**{**values, **changes})


def test_nested_tracking_and_namespaced_product_identity():
    target = "https://www.dmm.co.jp/mono/goods/-/detail/=/cid=alpha/"
    wrapped = "https://rcv.ixd.dmm.com/api/surl?urid=abc&lurl=" + target
    assert unwrap_goods_url(wrapped) == target
    assert item_identity(wrapped) == "alpha"
    n = news()
    assert association(n, url="https://getchu.com/soft.phtml?id=325773", title="Other", item_id="325773")[0] == "uncertain"
    assert association(n, url=n.source_urls[0], title="Japanese alias", item_id="325773")[0] == "matched"
    assert association(n, url=n.source_urls[0].replace("325773", "325774"), title="Work Alpha", item_id="325774")[0] == "conflict"


@pytest.mark.parametrize("section", ["新作", "汉化"])
def test_goods_news_dispatch_never_changes_other_sections(section):
    assert not is_goods_news(news(section=section))


def test_item_metadata_lazy_original_and_seller_not_cg():
    url = "https://amazon.co.jp/dp/B000000001"
    n = news(source_urls=[url])
    source = SourceRef(url=url, root_url=url, domain="amazon.co.jp", source_type=SourceType.OFFICIAL_SITE)
    html = '''<h1>Work Alpha ASMR cover</h1><div id="sellerProfileTriggerId">Circle A</div>
      <img id="landingImage" src="thumb.jpg" data-a-dynamic-image='{"https://img.example/original.jpg":[1600,1200]}'>
      <aside class="recommend"><img data-original="bad.jpg"></aside>'''
    result = GoodsSourceService(None).parse(n, source, CollectionContext(), html)
    assert len(result.candidates) == 1
    c = result.candidates[0]
    assert c.image_url == "https://img.example/original.jpg"
    assert c.signals["goods_association"] == "matched"
    assert c.evidence[0].seller == "Circle A"
    assert c.evidence[0].variant_of.endswith("thumb.jpg")
    assert c.evidence[0].item_id.casefold() == "b000000001"
    assert c.evidence[0].role == "goods"


def test_explicit_age_entry_is_navigable_but_verification_meta_is_not_gate():
    url = "https://www.dmm.co.jp/goods/"
    n = news(source_urls=[url])
    s = SourceRef(url=url, domain="www.dmm.co.jp", source_type=SourceType.OFFICIAL_SITE)
    assert GoodsSourceService.links(n, s, '<p>18歳以上ですか</p><a id="yes" href="/goods/main">はい</a>', url)[0][0].endswith("/goods/main")
    parsed = GoodsSourceService(None).parse(n, s, CollectionContext(), '<meta name="google-site-verification" content="abc">')
    assert not any(f.code == "goods_age_confirmation" for f in parsed.failures)


def test_failed_source_retains_url_without_fake_image():
    url = "https://www.getchu.com/soft.phtml?id=42"
    class Client:
        def get(self, _):
            return SimpleNamespace(url=url, text="denied", status_code=403)
    source = SourceRef(url=url, domain="www.getchu.com", source_type=SourceType.OFFICIAL_SITE)
    result = GoodsSourceService(Client()).collect(news(), source, CollectionContext())
    assert result.candidates == []
    assert result.failures[0].source_url == url
    assert not result.failures[0].retryable
