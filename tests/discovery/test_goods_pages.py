from galgame_news.discovery.goods_pages import GoodsImage, GoodsLink, goods_site, parse_goods_page


def test_site_profiles():
    cases = {
        "https://eeo.today/store/": "eeo", "https://www.getchu.com/soft.phtml?id=1": "getchu",
        "https://amazon.co.jp/dp/B000000001": "amazon_jp", "https://www.gamers.co.jp/pn/pd/1/": "gamers",
        "https://www.dmm.co.jp/mono/goods/-/detail/=/cid=x/": "dmm", "https://fantia.jp/products/1": "fantia",
        "https://www.dlsite.com/maniax/work/=/product_id/RJ123": "dlsite", "https://figsoku.net/blog-entry-1.html": "figsoku-b",
        "https://figsoku-b.net/blog-entry-2.html": "figsoku-b",
    }
    assert {url: goods_site(url) for url in cases} == cases
    assert goods_site("https://example.org") == ""


def test_getchu_detail_prefers_large_image_and_rejects_recommendations():
    html = '''<h1>作品グッズ</h1><div class="gallery"><a href="/images/large.jpg"><img src="/images/thumb.jpg" data-large="/images/large.jpg" alt="メイン"></a></div>
    <div class="recommend"><a href="soft.phtml?id=99"><img src="bad.jpg"></a></div>'''
    result = parse_goods_page(html, "https://www.getchu.com/soft.phtml?id=42")
    assert result.title == "作品グッズ"
    assert result.images[0].url.endswith("large.jpg") and result.images[0].thumbnail_url.endswith("thumb.jpg")
    assert result.images[0].original
    assert result.links == []


def test_listing_card_emits_product_link_and_image_evidence():
    result = parse_goods_page('<div class="ec-productRole__listItem"><a href="/soft.phtml?id=42"><img src="cover.jpg" alt="限定版"></a><b>限定版</b></div>', "https://www.getchu.com/feature/")
    assert result.links == [GoodsLink("https://www.getchu.com/soft.phtml?id=42", "限定版", "42")]
    assert result.images[0].role == "main"


def test_amazon_dynamic_image_json_and_asin():
    html = '''<title>フィギュア</title><img id="landingImage" src="thumb.jpg" data-a-dynamic-image='{"https://img/large.jpg":[1200,1600],"https://img/small.jpg":[400,500]}'><a href="/dp/B000000001">商品</a>'''
    result = parse_goods_page(html, "https://www.amazon.co.jp/dp/B000000001")
    assert any(i.url.endswith("large.jpg") and i.thumbnail_url.endswith("thumb.jpg") for i in result.images)
    assert result.images[0].item_id == "B000000001"
    assert not result.images[0].original  # largest display variant is not proof of an original
    assert result.links == []


def test_dmm_and_dlsite_detail_identifiers():
    dmm = parse_goods_page('<h1>限定商品</h1><div class="d-item"><img data-original="/big.jpg" src="/small.jpg"></div>', "https://www.dmm.co.jp/mono/goods/-/detail/=/cid=abc/")
    dl = parse_goods_page('<h1>作品</h1>', "https://www.dlsite.com/maniax/work/=/product_id/RJ123")
    assert dmm.images[0].original
    assert dl.title == "作品"


def test_figsoku_article_images_are_reference_stage():
    result = parse_goods_page('<article><h1>新作フィギュア 原型公開</h1><div class="article-body"><img src="prototype.jpg" alt="原型"></div><aside class="ranking"><img src="rank.jpg"></aside></article>', "https://figsoku.net/blog-entry-1.html")
    assert len(result.images) == 1
    assert result.images[0].role == "reference"
    assert result.images[0].stage == "prototype"


def test_product_jsonld_does_not_include_recommendation_list():
    html = '''<script type="application/ld+json">{"@type":"Product","name":"Badge","image":"/goods.jpg"}</script><div class="related"><img data-large="/other.jpg"></div>'''
    result = parse_goods_page(html, "https://eeo.today/products/1")
    assert [i.url for i in result.images] == ["https://eeo.today/goods.jpg"]


def test_srcset_chooses_largest_descriptor_not_last():
    result = parse_goods_page('<div class="ec-sliderItemRole"><img src="small.jpg" srcset="large.jpg 1600w, medium.jpg 800w"></div>', "https://example.test/")
    # Unsupported hosts do not parse image evidence.
    assert result.images == []
    result = parse_goods_page('<div class="ec-sliderItemRole"><img src="small.jpg" srcset="large.jpg 1600w, medium.jpg 800w"></div>', "https://eeo.today/products/detail/325773")
    assert result.images[0].url.endswith("large.jpg")


def test_product_anchor_is_not_mistaken_for_image_original_and_local_metadata_is_used():
    result = parse_goods_page('<div class="ec-productRole__listItem"><h2>Blue Badge</h2><a href="/products/detail/325773"><img src="thumb.jpg" alt="Badge"></a></div>', "https://eeo.today/store/")
    assert result.images[0].url.endswith("thumb.jpg")
    assert not result.images[0].original
    assert result.images[0].title == "Blue Badge"
    assert result.images[0].item_id == "325773"


def test_image_file_anchor_is_used_as_original_without_transforming_product_link():
    result = parse_goods_page('<div class="shouhinshousai-slide"><a href="/assets/large.png"><img src="/assets/thumb.jpg"></a></div>', "https://eeo.today/products/detail/325773")
    assert result.images[0].url.endswith("large.png")
    assert result.images[0].original


def test_amazon_product_specific_color_images_json():
    html = '''<title>Figure</title><script>var data = { "colorImages": {"initial":[{"hiRes":"https://img/hi.jpg","large":"https://img/lg.jpg"}]}, "colorToAsin": {}};</script>'''
    result = parse_goods_page(html, "https://www.amazon.co.jp/dp/B000000001")
    assert any(i.url.endswith("hi.jpg") for i in result.images)


def test_jsonld_multiple_products_does_not_poison_page():
    html = '''<h1>Main Goods</h1><script type="application/ld+json">[{"@type":"Product","name":"Main Goods","image":"/main.jpg"},{"@type":"Product","name":"Recommended","image":"/rec.jpg"}]</script>'''
    result = parse_goods_page(html, "https://eeo.today/products/detail/325773")
    assert result.images == []


def test_eeo_public_dom_profile_keeps_only_product_slider():
    html = '''<h1>Public Product</h1>
    <div class="ec-sliderItemRole swiper swiper-shouhinshousai"><div class="swiper-slide shouhinshousai-slide"><img src="/store/img/main.jpg" alt="Public Product"></div></div>
    <div class="ec-titleSellingNewItemRole__list"><img src="/store/img/recommended.jpg"></div>
    <footer><img src="/store/img/footer.jpg"></footer>'''
    result = parse_goods_page(html, "https://eeo.today/store/101/products/detail/325773")
    assert [image.url for image in result.images] == ["https://eeo.today/store/img/main.jpg"]
    assert result.images[0].item_id == "325773"


def test_eeo_detail_slider_plain_images_are_original_and_keep_page_title():
    result = parse_goods_page('<h1>EEO Detail Title</h1><div class="ec-sliderItemRole"><div class="shouhinshousai-slide"><img src="plain.jpg"></div></div>', "https://eeo.today/store/101/products/detail/325773")
    assert result.images[0].original
    assert result.images[0].title == "EEO Detail Title"
    assert result.images[0].item_id == "325773"


def test_empty_anchor_and_malformed_urls_are_safe():
    result = parse_goods_page('<div class="shouhinshousai-slide"><a href=""><img data-original="image.jpg"></a></div>', "https://eeo.today/products/detail/325773")
    assert result.images[0].url.endswith("image.jpg")
    assert goods_site("http://[invalid") == ""


def test_jsonld_identity_can_supply_title_and_item_id():
    html = '<script type="application/ld+json">{"@type":"Product","name":"Structured Title","sku":"SKU-7","image":"/large.jpg"}</script>'
    result = parse_goods_page(html, "https://amazon.co.jp/dp/B000000001")
    assert result.images[0].title == "Structured Title"
    assert result.images[0].item_id == "SKU-7"
    assert result.images[0].original is False


def test_eeo_explicit_filtered_pagination_is_retained():
    html = '<h1>ALcot goods</h1><a class="pageno" href="/store/101/title/8161/page/2">次へ</a><a class="pageno" href="/store/101/title/999/page/2">Other</a>'
    result = parse_goods_page(html, "https://eeo.today/store/101/title/8161")
    assert [(link.url, link.kind) for link in result.links] == [("https://eeo.today/store/101/title/8161/page/2", "listing_page")]


def test_figsoku_primary_product_excludes_main_recommendations_and_article_teaser():
    html = '<main><div class="group"><img src="recommend.jpg"></div><article><h1>Work figure</h1><div class="entry_text"><table><img src="other-post.jpg"></table><section class="product"><a href="original.jpg"><img src="thumb.jpg"></a></section></div></article></main>'
    result = parse_goods_page(html, "https://figsoku-b.net/blog-entry-13167")
    assert [image.url for image in result.images] == ["https://figsoku-b.net/original.jpg"]
    assert result.images[0].role == "reference"


def test_amazon_detail_excludes_recommended_products_and_their_images():
    html = """<h1 id="productTitle">Main figure</h1>
    <img id="landingImage" src="small.jpg" data-old-hires="/main-original.jpg">
    <div id="sims-consolidated-1_feature_div"><a href="/dp/B000000002">
    <div class="product"><img data-large="/other.jpg" alt="Other figure"></div></a></div>"""
    result = parse_goods_page(html, "https://www.amazon.co.jp/dp/B000000001")
    assert [i.url for i in result.images] == ["https://www.amazon.co.jp/main-original.jpg"]
    assert result.images[0].original
    assert result.links == []


def test_amazon_product_links_are_canonicalized_on_listing_only():
    result = parse_goods_page('<a href="/a-title/dp/B000000001/ref=abc?tag=tracking">Figure</a>',
                              "https://www.amazon.co.jp/s?me=shop")
    assert [link.url for link in result.links] == ["https://www.amazon.co.jp/dp/B000000001"]


def test_fantia_site_market_promotion_inside_product_body_is_not_a_product_image():
    html = """<h1>Product</h1><div class="product-body"><div class="product-images">
    <img data-original="/uploads/product/main.jpg" src="/uploads/product/thumb.jpg"></div>
    <img src="/images/market/market_lead_banner.jpg" alt="FantiaMarket"></div>"""
    page = parse_goods_page(html, "https://fantia.jp/products/118217")
    assert [i.url for i in page.images] == ["https://fantia.jp/uploads/product/main.jpg"]
