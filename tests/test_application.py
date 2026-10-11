from pathlib import Path
from io import BytesIO

from PIL import Image, ImageDraw

from galgame_news.domain import Issue, IssueDraft, NewsDraft, NewsItem, SourceRef, SourceType
from galgame_news.domain import CollectionResult, ReviewReason


def test_application_offline_runs_with_injected_components(tmp_path):
    from galgame_news.application import Application

    fixture = tmp_path / "sample.docx"
    fixture.write_bytes(b"not used")
    app = Application(offline=True)
    result = app.run(fixture, issue_id="1", output_dir=tmp_path / "out")
    assert result.issue.issue_id == "1"
    assert (tmp_path / "out" / "image_index.json").exists()


def test_application_isolates_news_failures(tmp_path):
    from galgame_news.application import Application

    class FailingResolver:
        def resolve(self, news):
            raise RuntimeError("one source failed")

    app = Application(offline=True, resolver=FailingResolver())
    fixture = tmp_path / "sample.docx"
    fixture.write_bytes(b"not used")
    result = app.run(fixture, issue_id="1", output_dir=tmp_path / "out")
    assert result.failures


def test_application_downloads_validates_hashes_and_saves_selected_images(tmp_path):
    from galgame_news.application import Application

    class Parser:
        def parse(self, path, issue_id):
            return IssueDraft(issue_id=issue_id, input_path=str(path), entries=[NewsDraft(sequence=1, section="新作", title="Game", body="CG")])

    class Analyzer:
        def analyze(self, draft):
            return Issue(issue_id=draft.issue_id, input_path=draft.input_path, news_items=[NewsItem(issue_id=draft.issue_id, sequence=1, section="新作", title="Game", body="CG", source_urls=["https://cdn.example/cg.jpg"])])

    class Resolver:
        def resolve(self, news):
            return [
                SourceRef(url="https://cdn.example/cg.jpg", domain="cdn.example", source_type=SourceType.DIRECT_IMAGE),
                SourceRef(url="https://cdn.example/logo.png", domain="cdn.example", source_type=SourceType.DIRECT_IMAGE),
            ]

    stream = BytesIO()
    Image.new("RGB", (800, 600), "red").save(stream, format="JPEG")
    jpeg = stream.getvalue()

    def transport(url, **_):
        return type("Response", (), {"content": jpeg, "headers": {"content-type": "image/jpeg"}, "status_code": 200, "url": url})()

    fixture = tmp_path / "sample.docx"
    fixture.write_bytes(b"fixture")
    result = Application(parser=Parser(), analyzer=Analyzer(), resolver=Resolver(), image_transport=transport).run(fixture, issue_id="259", output_dir=tmp_path / "out")
    assert len(result.candidates) == 1
    selected = result.candidates[0]
    assert selected.sha256 and selected.width == 800 and selected.height == 600
    assert selected.local_path and Path(selected.local_path).is_file()
    assert Path(selected.local_path).parent.name == "未候选"
    assert Path(selected.local_path).name == "x1.u01.png"
    assert selected.selected is False


def test_application_does_not_truncate_gallery_before_filtering_page_assets(tmp_path):
    from galgame_news.application import Application

    class Parser:
        def parse(self, path, issue_id):
            return IssueDraft(issue_id=issue_id, input_path=str(path), entries=[NewsDraft(sequence=1, section="新作", title="Gallery Game", body="CG")])

    class Analyzer:
        def analyze(self, draft):
            return Issue(issue_id=draft.issue_id, input_path=draft.input_path, news_items=[NewsItem(issue_id=draft.issue_id, sequence=1, section="新作", title="Gallery Game", body="CG", source_urls=["https://official.example/game"])])

    class Resolver:
        def resolve(self, news):
            return [SourceRef(url="https://official.example/game", domain="official.example", source_type=SourceType.OFFICIAL_SITE)]

    html = "".join(f'<img src="/assets/logo/logo{index}.png">' for index in range(25))
    html += "".join(f'<img src="/gallery/cg{index}.jpg">' for index in range(6))

    def source_transport(url, **_):
        return type("Response", (), {"text": html, "content": html.encode(), "headers": {"content-type": "text/html"}, "status_code": 200, "url": url})()

    def image_transport(url, **_):
        index = int(url.rsplit("cg", 1)[1].split(".", 1)[0])
        stream = BytesIO()
        image = Image.new("RGB", (800, 600), "black")
        ImageDraw.Draw(image).rectangle((index * 100, 0, index * 100 + 40, 599), fill="white")
        image.save(stream, format="JPEG")
        return type("Response", (), {"content": stream.getvalue(), "headers": {"content-type": "image/jpeg"}, "status_code": 200, "url": url})()

    fixture = tmp_path / "sample.docx"
    fixture.write_bytes(b"fixture")
    result = Application(parser=Parser(), analyzer=Analyzer(), resolver=Resolver(), source_transport=source_transport, image_transport=image_transport).run(
        fixture, issue_id="259", output_dir=tmp_path / "out"
    )
    assert len(result.candidates) == 6
    assert all("/gallery/cg" in candidate.image_url for candidate in result.candidates)
    assert sum(candidate.selected for candidate in result.candidates) == 0


def test_application_dispatches_video_and_dynamic_sources_to_specialized_adapters(tmp_path, monkeypatch):
    from galgame_news import application as module
    from galgame_news.application import Application

    class Parser:
        def parse(self, path, issue_id):
            return IssueDraft(issue_id=issue_id, input_path=str(path), entries=[
                NewsDraft(sequence=1, section="其他", title="Video", body=""),
            ])

    class Analyzer:
        def analyze(self, draft):
            item = NewsItem(issue_id=draft.issue_id, sequence=1, section="其他", title="Video", body="")
            return Issue(issue_id=draft.issue_id, input_path=draft.input_path, news_items=[item])

    class Resolver:
        def resolve(self, news):
            return [
                SourceRef(url="https://youtu.be/abc", domain="youtu.be", source_type=SourceType.VIDEO),
                SourceRef(url="https://official.example/app", domain="official.example", source_type=SourceType.OFFICIAL_SITE, requires_review=True, review_reasons=[ReviewReason.DYNAMIC_PAGE]),
            ]

    calls = []
    class Video:
        def __init__(self, **kwargs): pass
        def collect(self, news, source, context):
            calls.append(("video", source.source_type)); return CollectionResult()
    class Dynamic:
        def __init__(self, **kwargs): pass
        def collect(self, news, source, context):
            calls.append(("dynamic", source.source_type)); return CollectionResult()
    class Official:
        def __init__(self, **kwargs): pass
        def collect(self, news, source, context):
            calls.append(("official", source.source_type)); return CollectionResult()
    monkeypatch.setattr(module, "VideoAdapter", Video)
    monkeypatch.setattr(module, "DynamicPageAdapter", Dynamic)
    monkeypatch.setattr(module, "OfficialHtmlAdapter", Official)

    fixture = tmp_path / "sample.docx"; fixture.write_bytes(b"fixture")
    Application(parser=Parser(), analyzer=Analyzer(), resolver=Resolver()).run(fixture, issue_id="1", output_dir=tmp_path / "out")
    # Independent source adapters run concurrently; dispatch order is unspecified.
    assert sorted(calls) == sorted([("video", SourceType.VIDEO), ("dynamic", SourceType.OFFICIAL_SITE)])


def test_application_records_news_result_in_history(tmp_path):
    from galgame_news.application import Application

    class Parser:
        def parse(self, path, issue_id):
            return IssueDraft(issue_id=issue_id, input_path=str(path), entries=[NewsDraft(sequence=1, section="新作", title="Game", body="")])
    class Analyzer:
        def analyze(self, draft):
            return Issue(issue_id=draft.issue_id, input_path=draft.input_path, news_items=[NewsItem(issue_id=draft.issue_id, sequence=1, section="新作", title="Game", body="")])
    class Resolver:
        def resolve(self, news): return []
    class History:
        def __init__(self): self.results = []
        def sources_for(self, news): return []
        def known_image(self, sha256, perceptual_hash): return None
        def record_news_result(self, result): self.results.append(result)
    history = History()
    fixture = tmp_path / "sample.docx"; fixture.write_bytes(b"fixture")
    Application(parser=Parser(), analyzer=Analyzer(), resolver=Resolver(), history=history).run(fixture, issue_id="1", output_dir=tmp_path / "out")
    assert len(history.results) == 1


def test_application_preserves_manual_review_reasons_without_candidates(tmp_path):
    from galgame_news.application import Application
    class Parser:
        def parse(self, path, issue_id):
            return IssueDraft(issue_id=issue_id, input_path=str(path), entries=[NewsDraft(sequence=1, section="其他", title="Video", body="")])
    class Analyzer:
        def analyze(self, draft):
            return Issue(issue_id=draft.issue_id, input_path=draft.input_path, news_items=[NewsItem(issue_id=draft.issue_id, sequence=1, section="其他", title="Video", body="")])
    class Resolver:
        def resolve(self, news): return [SourceRef(url="https://youtu.be/abc", domain="youtu.be", source_type=SourceType.VIDEO)]
    fixture = tmp_path / "sample.docx"; fixture.write_bytes(b"fixture")
    result = Application(parser=Parser(), analyzer=Analyzer(), resolver=Resolver()).run(fixture, issue_id="1", output_dir=tmp_path / "out")
    assert any(f.code == "manual_review_required" and "dynamic_page" in f.message for f in result.failures)


def test_application_continues_after_one_source_adapter_raises(tmp_path, monkeypatch):
    from galgame_news import application as module
    from galgame_news.application import Application
    class Parser:
        def parse(self, path, issue_id): return IssueDraft(issue_id=issue_id, input_path=str(path), entries=[NewsDraft(sequence=1, section="其他", title="Game", body="")])
    class Analyzer:
        def analyze(self, draft):
            item=NewsItem(issue_id=draft.issue_id, sequence=1, section="其他", title="Game", body="")
            return Issue(issue_id=draft.issue_id,input_path=draft.input_path,news_items=[item])
    class Resolver:
        def resolve(self, news): return [SourceRef(url="https://bad.example",domain="bad.example",source_type=SourceType.OFFICIAL_SITE), SourceRef(url="https://good.example",domain="good.example",source_type=SourceType.OFFICIAL_SITE)]
    class Adapter:
        def __init__(self, **kwargs): pass
        def collect(self, news, source, context):
            if source.domain == "bad.example": raise RuntimeError("fixture failure")
            return CollectionResult()
    monkeypatch.setattr(module, "OfficialHtmlAdapter", Adapter)
    fixture=tmp_path/"sample.docx"; fixture.write_bytes(b"fixture")
    result=Application(parser=Parser(),analyzer=Analyzer(),resolver=Resolver()).run(fixture,issue_id="1",output_dir=tmp_path/"out")
    assert any(f.code == "source_failed" for f in result.failures)


def test_application_legacy_wrapper_reruns_existing_output_dir(tmp_path):
    from galgame_news.application import Application

    class Parser:
        def parse(self, path, issue_id):
            return IssueDraft(issue_id=issue_id, input_path=str(path), entries=[
                NewsDraft(sequence=1, section="新作", title="Game", body="CG"),
            ])

    class Analyzer:
        def analyze(self, draft):
            return Issue(issue_id=draft.issue_id, input_path=draft.input_path, news_items=[
                NewsItem(issue_id=draft.issue_id, sequence=1, section="新作", title="Game", body="CG"),
            ])

    class Resolver:
        def resolve(self, news):
            return []

    fixture = tmp_path / "sample.docx"
    fixture.write_bytes(b"fixture")
    output_dir = tmp_path / "out"
    app = Application(parser=Parser(), analyzer=Analyzer(), resolver=Resolver())
    app.run(fixture, issue_id="1", output_dir=output_dir)

    rerun = app.run(fixture, issue_id="1", output_dir=output_dir)

    assert not any(failure.code == "setup_error" for failure in rerun.failures)


def test_application_legacy_failure_preserves_existing_images(tmp_path):
    from galgame_news.application import Application

    class FailingParser:
        def parse(self, path, issue_id):
            raise ValueError("bad DOCX")

    class Analyzer:
        def analyze(self, draft):
            raise AssertionError("analyzer must not run")

    class Resolver:
        def resolve(self, news):
            return []

    fixture = tmp_path / "sample.docx"
    fixture.write_bytes(b"fixture")
    output_dir = tmp_path / "out"
    existing_image = output_dir / "images" / "x1" / "keep.jpg"
    existing_image.parent.mkdir(parents=True)
    existing_image.write_bytes(b"old image")

    result = Application(
        parser=FailingParser(), analyzer=Analyzer(), resolver=Resolver(),
    ).run(fixture, issue_id="1", output_dir=output_dir)

    assert result.failures
    assert existing_image.read_bytes() == b"old image"
