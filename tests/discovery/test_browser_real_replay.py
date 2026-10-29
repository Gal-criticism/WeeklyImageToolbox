"""Real Chromium with every public request served from memory, never the web."""
import asyncio
from io import BytesIO
from types import SimpleNamespace

import pytest
from PIL import Image

from galgame_news.config import BrowserConfig
from galgame_news.discovery.async_http import AsyncHttpClient
from galgame_news.discovery.browser import BrowserRenderer, check_browser_runtime
from galgame_news.discovery.adapters.html import OfficialHtmlAdapter
from galgame_news.domain import CollectionContext, NewsItem, SourceRef, SourceType
from galgame_news.domain import Issue
from galgame_news.pipeline import PipelineRunner, TaskRequest


PAGE = "https://gallery.example/game/"
HTML = """<title>Example Game</title><section id="gallery">
<h2><img src="heading.png" alt="Gallery"></h2>
<img id="galleryPreview" src="placeholder.png"><div id="galleryThumbnailTrack"></div>
<button id="gallery-next" type="button">CG 下一张</button></section>
<script src="app.js"></script>"""
SCRIPT = """const files=['one.png','two.png','three.png']; let i=0;
const p=document.querySelector('#galleryPreview');p.src=files[0];
document.querySelector('#gallery-next').onclick=()=>{i=Math.min(i+1,2);p.src=files[i]};
const track=document.querySelector('#galleryThumbnailTrack');
files.forEach(src=>{const b=document.createElement('button');b.className='gallery-thumbnail';
b.type='button';const img=document.createElement('img');img.src=src;b.append(img);
b.onclick=()=>{p.src=src};track.append(b)});
"""


def test_real_browser_discovers_images_and_routes_only_through_public_client():
    if not check_browser_runtime().available:
        pytest.skip("可选 Chromium 运行环境未安装")

    async def run():
        calls = []
        def transport(url, **kwargs):
            calls.append((url, kwargs))
            body = SCRIPT if url == PAGE + "app.js" else HTML
            return SimpleNamespace(content=body.encode(), url=url, status_code=200,
                                   headers={"content-type": "application/javascript" if url.endswith('.js') else "text/html; charset=utf-8"})
        async with AsyncHttpClient(transport=transport, resolver=lambda _: ["8.8.8.8"]) as client:
            async with BrowserRenderer(client, BrowserConfig()) as browser:
                result = await browser.render(PAGE, "n1", HTML)
                assert not result.diagnostics
                news = NewsItem(issue_id="263", sequence=1, section="新作", title="Example Game CG 更新", body="CG", game_names=["Example Game"], source_urls=[PAGE])
                ref = SourceRef(url=PAGE, root_url=PAGE, domain="gallery.example", source_type=SourceType.OFFICIAL_SITE)
                parser = OfficialHtmlAdapter()
                photos = {}
                for snapshot in result.snapshots:
                    parsed = parser.parse(news, ref, CollectionContext(), snapshot.html, method="browser")
                    assert not parsed.failures
                    photos.update({c.image_url: c for c in parsed.candidates})
                for filename in ("one.png", "two.png", "three.png"):
                    assert photos[PAGE+filename].evidence[0].role == "game_cg"
                assert photos[PAGE+"heading.png"].evidence[0].role == "decorative"
                assert browser._operations_by_page[("n1", PAGE)] <= 40
                assert calls and {url for url, _ in calls} == {PAGE+"app.js"}
                assert all("authorization" not in kwargs.get("headers", {}) for _, kwargs in calls)
    asyncio.run(run())


@pytest.mark.parametrize("supplement_mode", [False, True])
def test_default_pipeline_renders_downloads_and_publishes_dynamic_gallery(tmp_path, supplement_mode):
    if not check_browser_runtime().available:
        pytest.skip("可选 Chromium 运行环境未安装")
    news = NewsItem(issue_id="263", sequence=1, section="新作", title="Example Game CG 更新", body="CG", game_names=["Example Game"],source_urls=[PAGE])
    ref = SourceRef(url=PAGE,root_url=PAGE,domain="gallery.example",
        source_type=SourceType.UNVERIFIED if supplement_mode else SourceType.OFFICIAL_SITE,
        officiality=0.4 if supplement_mode else 1)
    path=tmp_path/"fixture.docx"
    path.write_bytes(b"frozen pipeline fixture")
    class Parser:
        def parse(self,*_): return None
    class Analyzer:
        def analyze(self,*_): return Issue(issue_id="263",input_path=str(path),news_items=[news])
    class Resolver:
        def resolve(self,*_): return [ref]
    def page_transport(url,**_):
        body=SCRIPT if url.endswith("app.js") else HTML
        return SimpleNamespace(content=body.encode(),url=url,status_code=200,
            headers={"content-type":"application/javascript" if url.endswith('.js') else "text/html; charset=utf-8"})
    def image_transport(url,**_):
        output=BytesIO()
        colors={"one.png":"red","two.png":"green","three.png":"blue","heading.png":"white"}
        Image.new("RGB",(960,540),colors.get(url.rsplit('/',1)[-1],"black")).save(output,format="PNG")
        return SimpleNamespace(content=output.getvalue(),url=url,status_code=200,headers={"content-type":"image/png"})
    class FixtureRunner(PipelineRunner):
        async def _run_async_body(self,*args):
            self._supplement_mode = supplement_mode
            self._network_runtime.page_client.resolver=lambda _: ["8.8.8.8"]
            self._network_runtime.image_client.resolver=lambda _: ["8.8.8.8"]
            return await super()._run_async_body(*args)
    runner=FixtureRunner(parser=Parser(),analyzer=Analyzer(),resolver=Resolver(),source_transport=page_transport,image_transport=image_transport)
    result=runner.run(TaskRequest(input_path=path,issue_id="263",output_dir=tmp_path/"output",no_videos=True))
    assert result.status == "completed"
    photos={c.image_url:c for c in result.result.all_candidates}
    for filename in ("one.png","two.png","three.png"):
        c=photos[PAGE+filename]
        assert c.download_status == "downloaded"
        if not supplement_mode:
            assert c.selected
        else:
            assert c.source_type is SourceType.UNVERIFIED
        assert c.evidence[0].method == "browser"
        with Image.open(c.local_path) as image:
            image.load()
            assert image.format == "PNG"
    assert not photos[PAGE+"heading.png"].selected
    assert runner.network_metrics["socialdata_requests"] == 0
    assert "browser_render" in runner.network_metrics["stage_seconds"]
    assert not list(result.task_dir.rglob("*.part"))
