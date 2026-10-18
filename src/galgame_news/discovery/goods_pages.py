"""Static, product-local parsers for Japanese goods pages.

This module intentionally performs no network access and never executes page
scripts. It extracts only product/gallery evidence from the supplied HTML.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import html as html_lib
import json
import re
from typing import Any
from urllib.parse import parse_qs, urljoin, urlsplit

from bs4 import BeautifulSoup


@dataclass
class GoodsImage:
    url: str
    thumbnail_url: str = ""
    item_id: str = ""
    title: str = ""
    role: str = "main"
    text: str = ""
    work: str = ""
    maker: str = ""
    activity: str = ""
    stage: str = ""
    container: str = ""
    original: bool = True
    seller: str = ""


@dataclass
class GoodsLink:
    url: str
    title: str
    item_id: str = ""
    kind: str = "detail"


@dataclass
class GoodsPage:
    title: str
    images: list[GoodsImage] = field(default_factory=list)
    links: list[GoodsLink] = field(default_factory=list)
    reason: str = ""
    dynamic: bool = False


_EXCLUDE = re.compile(r"recommend|related|ranking|rank-|sidebar|side-bar|footer|header|breadcrumb|recent|人気|おすすめ|ランキング|ec-titleSelling|ec-related|ec-ranking", re.I)
_ROLE = (("collection", r"collection|全種|全種類|コンプリート"), ("bonus", r"特典|bonus|購入特典"),
         ("design", r"デザイン|design|設定画|ラフ|彩色"), ("sample", r"sample|サンプル|見本"),
         ("cover", r"cover|jacket|package|パッケージ|表紙"), ("reference", r"参考|prototype|試作品|原型|figsoku"))


def goods_site(url: str) -> str:
    try: host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
    except (TypeError, ValueError): return ""
    if host == "eeo.today" or host.endswith(".eeo.today"): return "eeo"
    if host == "getchu.com" or host.endswith(".getchu.com"): return "getchu"
    if host in {"amazon.co.jp", "amazon.jp"} or host.endswith(".amazon.co.jp"): return "amazon_jp"
    if host == "gamers.co.jp" or host.endswith(".gamers.co.jp"): return "gamers"
    if host in {"dmm.co.jp", "dmm.com", "fanza.co.jp", "fanza.jp"} or host.endswith((".dmm.co.jp", ".dmm.com", ".fanza.co.jp", ".fanza.jp")): return "dmm"
    if host == "fantia.jp" or host.endswith(".fantia.jp"): return "fantia"
    if host == "dlsite.com" or host.endswith(".dlsite.com"): return "dlsite"
    if host in {"figsoku.net", "figsoku-b.net"} or host.endswith((".figsoku.net", ".figsoku-b.net")): return "figsoku-b"
    return ""


def _text(node: Any) -> str:
    return re.sub(r"\s+", " ", node.get_text(" ", strip=True))[:500] if node else ""


def _attr(node: Any, *names: str) -> str:
    if node is None: return ""
    for name in names:
        value = node.get(name)
        if value: return str(value)
    return ""


def _url(value: str, base: str) -> str:
    if not isinstance(value, str): return ""
    value = html_lib.unescape(value.strip())
    if not value: return ""
    try:
        result = urljoin(base, value)
        parsed = urlsplit(result)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password: return ""
        return result
    except (TypeError, ValueError): return ""


def _item_id(url: str, site: str) -> str:
    try: p = urlsplit(url); q = parse_qs(p.query)
    except (TypeError, ValueError): return ""
    if site == "amazon_jp":
        m = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})", p.path, re.I); return m.group(1) if m else ""
    for key in ("id", "product_id", "product", "goods_id", "item_id"):
        if q.get(key): return q[key][0]
    if site == "eeo":
        m = re.search(r"/products/(?:detail/)?(\d+)", p.path, re.I)
        if m: return m.group(1)
    if site == "dmm":
        m = re.search(r"/cid=([^/]+)", p.path, re.I)
        if m: return m.group(1)
    if site == "dlsite":
        m = re.search(r"/product_id/([^/]+)", p.path, re.I)
        if m: return m.group(1)
    if site == "eeo":
        m = re.search(r"/products/detail/(\d+)", p.path, re.I)
        if m: return m.group(1)
    m = re.search(r"/(?:products?|posts?)/(\d+)", p.path)
    return m.group(1) if m else ""


def _role(text: str) -> str:
    for role, pattern in _ROLE:
        if re.search(pattern, text, re.I): return role
    return "main"


def _highres(tag: Any, base: str) -> tuple[str, str]:
    """Return (best URL, displayed thumbnail URL), preferring explicit originals."""
    thumb = _url(_attr(tag, "src", "data-src", "data-lazy-src"), base)
    for key in ("data-large", "data-zoom-image", "data-original", "data-full", "data-hires", "data-src", "data-lazy-src"):
        if tag.get(key): return _url(str(tag[key]), base), thumb
    srcset = tag.get("data-srcset") or tag.get("srcset")
    picture = tag.find_parent("picture")
    if picture:
        sources = [s.get("data-srcset") or s.get("srcset") for s in picture.find_all("source")]
        srcset = ",".join(str(s) for s in [srcset, *sources] if s)
    if srcset:
        candidates = []
        for part in str(srcset).split(","):
            bits = part.strip().split()
            if not bits: continue
            desc = bits[1] if len(bits) > 1 else "1x"
            try: score = float(desc[:-1]) * (100000 if desc.endswith("w") else 1)
            except ValueError: score = 0
            candidates.append((score, bits[0]))
        if candidates:
            candidate = max(candidates)[1]
            return _url(candidate, base), thumb
    return thumb, thumb


def _excluded(tag: Any) -> bool:
    for node in [tag, *tag.parents]:
        if getattr(node, "name", None) in {"html", "body", "[document]"}: continue
        ident = " ".join([str(node.get("id") or ""), *map(str, node.get("class") or [])])
        if _EXCLUDE.search(ident) or node.name in {"nav", "footer", "aside"}: return True
        if node.name in {"section", "div", "article"}:
            heading = node.find(re.compile("^h[2-6]$"), recursive=False)
            if heading and re.search(r"ランキング|おすすめ|人気の|関連商品|recommend|ranking", _text(heading), re.I):
                return True
    return False


def _is_detail_page(url: str, site: str) -> bool:
    try: path = urlsplit(url).path.lower()
    except (TypeError, ValueError): return False
    return {
        "eeo": "/products/detail/" in path,
        "getchu": "soft.phtml" in path and bool(parse_qs(urlsplit(url).query).get("id")),
        "amazon_jp": bool(re.search(r"/(?:dp|gp/product)/[a-z0-9]{10}",path)),
        "dlsite": "/work/" in path,
        "fantia": "/posts/" in path or "/products/" in path,
        "dmm": "/goods/" in path or "detail" in path,
        "gamers": "detail" in path or "/p/" in path,
    }.get(site, False)


def _has_scope(tag: Any, pattern: str) -> bool:
    regex = re.compile(pattern, re.I)
    for node in [tag, *tag.parents]:
        if getattr(node, "name", None) in {"html", "body", "[document]"}: continue
        if regex.search(" ".join([str(node.get("id") or ""), *map(str,node.get("class") or [])])): return True
    return False


def parse_goods_page(html: str, page_url: str) -> GoodsPage:
    site = goods_site(page_url)
    soup = BeautifulSoup(html or "", "html.parser")
    title = _text(soup.select_one("h1")) or _text(soup.title)
    page = GoodsPage(title=title, reason=f"{site or 'unknown'} static HTML parser" if site else "unsupported host")
    if not site: return page
    # Amazon has a product-specific JSON object; parse JSON data only.
    if site == "amazon_jp":
        title = _text(soup.select_one("#productTitle")) or title
        page.title = title
        for script in soup.find_all("script"):
            raw = script.string or script.get_text()
            m = re.search(r"(?:'colorImages'|\"colorImages\")\s*:\s*(\{)", raw)
            if m:
                start = m.start(1); depth = 0; quote = None; escaped = False; end = None
                for pos in range(start, len(raw)):
                    ch = raw[pos]
                    if quote:
                        if escaped: escaped = False
                        elif ch == "\\": escaped = True
                        elif ch == quote: quote = None
                    elif ch in "'\"": quote = ch
                    elif ch == "{": depth += 1
                    elif ch == "}":
                        depth -= 1
                        if depth == 0: end = pos + 1; break
                try:
                    chunk = raw[start:end] if end else ""
                    chunk = re.sub(r"(?<![\w\"'])([A-Za-z_$][\w$]*)\s*:", r'"\1":', chunk)
                    chunk = re.sub(r"'([^'\\]*(?:\\.[^'\\]*)*)'", lambda x: json.dumps(x.group(1)), chunk)
                    obj = json.loads(chunk); arr = obj.get("initial", [])
                    for entry in arr:
                        if not isinstance(entry, dict): continue
                        u = entry.get("hiRes") or entry.get("large") or entry.get("main")
                        if u and _url(u,page_url): page.images.append(GoodsImage(_url(u, page_url), title=title, role="main", original=bool(entry.get("hiRes"))))
                except (ValueError, TypeError, AttributeError): pass
        for node in soup.select("#landingImage, #imgTagWrapperId img"):
            raw = node.get("data-a-dynamic-image")
            if raw:
                try:
                    imgs = json.loads(raw)
                    if imgs:
                        valid = [u for u, dims in imgs.items() if isinstance(dims,list) and len(dims)>=2 and all(isinstance(n,(int,float)) for n in dims[:2])]
                        if valid:
                            u = max(valid, key=lambda x: imgs[x][0] * imgs[x][1])
                            page.images.append(GoodsImage(_url(u, page_url), thumbnail_url=_url(_attr(node,"src"),page_url), title=title, original=False))
                except (ValueError, TypeError, KeyError, AttributeError): pass
    # Product structured data is accepted only when it is the page's primary Product.
    for script in soup.select('script[type="application/ld+json"]'):
        try: data = json.loads(script.string or script.get_text())
        except (ValueError, TypeError): continue
        entries = data if isinstance(data, list) else [data]
        graph = [node for item in entries if isinstance(item,dict) for node in (item.get("@graph",[]) if isinstance(item.get("@graph"),list) else [item]) if isinstance(node,dict)]
        products = [entry for entry in graph if entry.get("@type") == "Product"]
        for entry in products:
            if not isinstance(entry, dict): continue
            entry_name = str(entry.get("name") or "")
            # Only accept one product when page heading/name identity supports it.
            identity_ok = len(products) == 1 and (not title or not entry_name or title.casefold() in entry_name.casefold() or entry_name.casefold() in title.casefold())
            if identity_ok and not any(k in entry for k in ("isRelatedTo", "isSimilarTo")):
                title = title or str(entry.get("name") or "")
                if not page.title: page.title = title
                product_id = str(entry.get("sku") or entry.get("productID") or _item_id(str(entry.get("url") or ""),site) or "")
                images = entry.get("image", []); images = [images] if isinstance(images, str) else images
                for u in images if isinstance(images, list) else []:
                    if isinstance(u, str) and _url(u,page_url): page.images.append(GoodsImage(_url(u,page_url),item_id=product_id,title=title,original=False))
    # Link and image extraction uses only scoped product/article/card containers.
    for a in soup.find_all("a", href=True):
        if _excluded(a): continue
        if site == "amazon_jp" and _is_detail_page(page_url, site):
            # Other ASINs on a detail page are recommendations, not navigation
            # within the current news product, even when the work name matches.
            continue
        href = _url(str(a["href"]), page_url)
        if site == "eeo" and "login_redirect" in urlsplit(href).path:
            redirect = parse_qs(urlsplit(href).query).get("url", [""])[0]
            if redirect: href = _url(redirect, page_url)
        text = _text(a) or _attr(a, "title", "aria-label") or _attr(a.find("img"), "alt", "title")
        if not text and not a.find("img"): continue
        if site == "dmm" and "click=" in href:
            target = parse_qs(urlsplit(href).query).get("click", [""])[0]
            if target.startswith("http"): href = target
        path = urlsplit(href).path.lower()
        if site == "eeo" and "pageno" in (a.get("class") or []):
            listing = re.match(r"(/store/\d+/title/\d+)(?:/page/\d+)?/?$", urlsplit(page_url).path)
            if listing and re.fullmatch(re.escape(listing[1]) + r"/page/\d+/?", path):
                page.links.append(GoodsLink(href, text, kind="listing_page"))
                continue
        valid = ((site == "getchu" and "soft.phtml" in path) or (site == "amazon_jp" and bool(re.search(r"/(?:dp|gp/product)/[A-Z0-9]{10}",path,re.I))) or
                 (site == "dlsite" and bool(re.search(r"/(?:product_id|work)/",path))) or
                 (site == "eeo" and "/products/" in path) or (site == "gamers" and ("/p/" in path or "detail" in path)) or
                 (site == "dmm" and ("/goods/" in path or "/kuji/detail/" in path or "cid=" in href)) or
                 (site == "fantia" and ("/posts/" in path or "/products/" in path)))
        if valid:
            item = _item_id(href,site)
            if site == "amazon_jp" and item:
                href = f"https://www.amazon.co.jp/dp/{item}"
            if not any(x.url == href for x in page.links): page.links.append(GoodsLink(href,text,item))
    # Product detail/gallery images: recommendations and general page chrome were filtered above.
    for img in soup.find_all("img"):
        if _excluded(img): continue
        if site == "fantia" and urlsplit(_url(str(img.get("src") or ""), page_url)).path == "/images/market/market_lead_banner.jpg":
            # Site-wide market promotion embedded even inside product-body.
            continue
        if site == "amazon_jp" and not (img.get("id") == "landingImage" or img.find_parent(id="imgTagWrapperId")):
            continue
        container = img.parent
        card = img.find_parent(class_=re.compile(r"product|goods|item|card|prize|gallery|main_image|main-image|ec-productRole__listItem",re.I))
        detail_selectors = {
            "eeo": r"shouhinshousai-slide|ec-sliderItemRole",
            "getchu": r"soft[-_]table|soft_detail|item_detail|product_detail|highslide-gallery",
            "dlsite": r"work_main|work_left|work_image|work_slider|work_sample|sample_picture|product_detail",
            "fantia": r"post[-_](?:content|body|images)|product_detail|product-images",
            "dmm": r"d-item|goods_detail|detail-main|product_detail|prize|kuji",
            "gamers": r"product_detail|productDetail|item-detail",
        }
        detail_scope = bool(detail_selectors.get(site) and _has_scope(img,detail_selectors[site]))
        if site == "eeo" and not (detail_scope or (card and "ec-productRole__listItem" in " ".join(card.get("class",[])))): continue
        list_card = bool(card and re.search(r"product|goods|item|card|ec-productRole__listItem", " ".join(map(str, card.get("class",[]))),re.I))
        amazon_main = site == "amazon_jp" and (img.get("id") == "landingImage" or img.find_parent(id="imgTagWrapperId"))
        article_reference = site == "figsoku-b" and img.find_parent("article")
        if article_reference and soup.select_one("article .entry_text section.product"):
            article_reference = img.find_parent("section", class_="product")
        elif article_reference and soup.select_one("article .entry_text"):
            article_reference = img.find_parent(class_="entry_text")
        if site == "figsoku-b" and not article_reference:
            continue
        generic_gallery = _has_scope(img, r"gallery|main-image|main_image")
        if not (detail_scope or list_card or amazon_main or article_reference or generic_gallery): continue
        anc = img.find_parent("a", href=True)
        local_title = _text(card.select_one("h1,h2,h3,[class*=title],[itemprop=name]")) if card else ""
        if not local_title and (detail_scope or _is_detail_page(page_url,site)): local_title = title
        if not local_title and article_reference and len(soup.select("article .entry_text section.product")) <= 1:
            local_title = title
        if not local_title:
            local_title = _attr(img, "alt", "title")
        local_link = card.find("a",href=True) if card else None
        local_item = _item_id(_url(str(local_link.get("href")),page_url),site) if local_link else _item_id(page_url,site)
        context = " ".join((_text(container), _attr(img,"alt","title"), str(img.get("class") or "")))
        best, thumb = _highres(img,page_url)
        amazon_original = _url(str(img.get("data-old-hires") or ""), page_url) if site == "amazon_jp" else ""
        if amazon_original:
            best = amazon_original
        if not best: continue
        explicit = any(img.get(k) for k in ("data-large","data-zoom-image","data-original","data-full","data-hires","srcset","data-srcset")) or bool(img.find_parent("picture") and img.find_parent("picture").find("source", srcset=True))
        anchor_url = _url(str(anc.get("href")),page_url) if anc else ""
        image_like = bool(anchor_url and re.search(r"\.(?:jpe?g|png|webp|gif)(?:$|[?#])", anchor_url, re.I))
        if image_like and not explicit:
            best = anchor_url
        # A product detail link is never treated as the image's original URL.
        # A broad detail panel can contain a thumbnail strip. Only EEO's
        # verified primary slider, explicit large-resource data and direct
        # image links prove a full resource here; other previews stay manual.
        confirmed_main = site == "eeo" and _has_scope(img, r"shouhinshousai-slide|ec-sliderItemRole")
        original = bool(explicit or image_like or confirmed_main or article_reference or amazon_original)
        if not original and not article_reference and not list_card and not generic_gallery:
            continue
        stage = "prototype" if site == "figsoku-b" and re.search(r"prototype|試作品|原型", context, re.I) else ""
        page.images.append(GoodsImage(best, thumb if best != thumb else "", local_item, local_title,
                                      "reference" if site == "figsoku-b" and stage else _role(context), context[:300], stage=stage, container=container.name if container else "", original=original))
    # Stable de-duplication keeps the best marked original.
    seen: dict[str, GoodsImage] = {}
    for image in page.images:
        prev = seen.get(image.url)
        if prev is None or (image.original and not prev.original): seen[image.url] = image
    page.images = list(seen.values())
    seller = _text(soup.select_one("#sellerProfileTriggerId, #merchant-info, [itemprop=seller]"))
    for image in page.images:
        image.item_id = image.item_id or _item_id(page_url, site)
        image.seller = seller
        # Only explicit, local metadata is propagated. A store title or the
        # news's own target name is not evidence about an individual product.
        image.maker = image.maker or _text(soup.select_one("[itemprop=brand], [itemprop=manufacturer]"))
        if site == "figsoku-b":
            image.role = "reference"
            if re.search(r"原型|灰模|prototype|試作品", title + " " + image.text, re.I):
                image.stage = "prototype"
    page.title = title
    dynamic_shell = soup.select_one("[data-product-gallery], .product-gallery, .product-images, #imageBlock, [data-product-id]")
    page.dynamic = not page.images and bool((site in {"amazon_jp", "fantia"} and _is_detail_page(page_url, site)) or dynamic_shell)
    return page
