from datetime import datetime, timezone
from pathlib import Path
import zipfile
from xml.etree import ElementTree as ET

import pytest

from galgame_news.domain import EventType, ImageNeed, NewsDraft
from galgame_news.ingestion.analyzer import OpenAINewsAnalyzer, RuleBasedNewsAnalyzer, detect_image_need
from galgame_news.ingestion.docx_parser import DocxDocumentParser, split_urls


def make_docx(path: Path, paragraphs: list[str]) -> None:
    ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    body = "".join(f'<w:p><w:r><w:t>{text.replace("&", "&amp;").replace("<", "&lt;")}</w:t></w:r></w:p>' for text in paragraphs)
    document = f'<w:document xmlns:w="{ns}"><w:body>{body}</w:body></w:document>'
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", document)


def make_styled_docx(path: Path, paragraphs: list[tuple[str, int, bool]]) -> None:
    namespace = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    ET.register_namespace("w", namespace)
    root = ET.Element(f"{{{namespace}}}document")
    body = ET.SubElement(root, f"{{{namespace}}}body")
    for text, size, bold in paragraphs:
        paragraph = ET.SubElement(body, f"{{{namespace}}}p")
        run = ET.SubElement(paragraph, f"{{{namespace}}}r")
        properties = ET.SubElement(run, f"{{{namespace}}}rPr")
        if bold:
            ET.SubElement(properties, f"{{{namespace}}}b")
        ET.SubElement(properties, f"{{{namespace}}}sz", {f"{{{namespace}}}val": str(size)})
        ET.SubElement(run, f"{{{namespace}}}t").text = text
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", ET.tostring(root, encoding="unicode"))


def test_docx_parser_keeps_all_news_sections_and_splits_concatenated_urls(tmp_path: Path):
    source = tmp_path / "weekly.docx"
    make_docx(
        source,
        [
            "目录",
            "新作",
            "《Happy Weekend》主机板制作完成#编辑#",
            "正文 https://official.example/news https://x.com/studio/status/1",
            "旧作",
            "《Old Game》更新#编辑#",
            "旧作正文",
        ],
    )

    issue = DocxDocumentParser().parse(source, "259")

    assert [entry.section for entry in issue.entries] == ["新作", "旧作"]
    assert issue.entries[0].title == "《Happy Weekend》主机板制作完成"
    assert issue.entries[0].source_urls == ["https://official.example/news", "https://x.com/studio/status/1"]
    assert issue.entries[1].title == "《Old Game》更新"


def test_split_urls_preserves_nested_dmm_url_and_splits_adjacent_pastes():
    dmm = "https://rcv.ixd.dmm.com/api/surl?urid=VRLXIjtX&lurl=https://www.dmm.co.jp/kuji/detail/schooldays/"
    assert split_urls(f"{dmm} https://example.com/a") == [dmm, "https://example.com/a"]
    assert split_urls("https://a.com/ahttps://b.com/b") == ["https://a.com/a", "https://b.com/b"]
    assert split_urls("https://a.com/x\n说明后续内容") == ["https://a.com/x"]
    assert split_urls("https://a.com/x，后续说明") == ["https://a.com/x"]


def test_docx_parser_preserves_breaks_tabs_and_relationship_hyperlinks(tmp_path: Path):
    w = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    r = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    pkg = "http://schemas.openxmlformats.org/package/2006/relationships"
    root = ET.Element(f"{{{w}}}document")
    body = ET.SubElement(root, f"{{{w}}}body")
    for text in ["作品#作者#"]:
        p = ET.SubElement(body, f"{{{w}}}p")
        run = ET.SubElement(p, f"{{{w}}}r")
        ET.SubElement(run, f"{{{w}}}t").text = text
    p = ET.SubElement(body, f"{{{w}}}p")
    run = ET.SubElement(p, f"{{{w}}}r")
    ET.SubElement(run, f"{{{w}}}t").text = "正文"
    ET.SubElement(run, f"{{{w}}}br")
    ET.SubElement(run, f"{{{w}}}tab")
    link = ET.SubElement(p, f"{{{w}}}hyperlink", {f"{{{r}}}id": "rId1"})
    linkrun = ET.SubElement(link, f"{{{w}}}r")
    ET.SubElement(linkrun, f"{{{w}}}t").text = "点击查看"
    source = tmp_path / "links.docx"
    rels = ET.Element(f"{{{pkg}}}Relationships")
    ET.SubElement(rels, f"{{{pkg}}}Relationship", {"Id": "rId1", "Type": "hyperlink", "Target": "https://example.com/news", "TargetMode": "External"})
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("word/document.xml", ET.tostring(root, encoding="unicode"))
        archive.writestr("word/_rels/document.xml.rels", ET.tostring(rels, encoding="unicode"))
    issue = DocxDocumentParser().parse(source, "259")
    assert issue.entries[0].body == "正文\n\t点击查看"
    assert issue.entries[0].source_urls == ["https://example.com/news"]


def test_docx_parser_ignores_preamble_and_url_fragments_as_headings(tmp_path: Path):
    source = tmp_path / "weekly.docx"
    make_docx(source, ["周报目录", "新作", "作品#作者#", "https://official.example/news#gallery"])

    issue = DocxDocumentParser().parse(source, "259")

    assert len(issue.entries) == 1
    assert issue.entries[0].title == "作品"
    assert issue.entries[0].source_urls == ["https://official.example/news#gallery"]


def test_docx_parser_recognizes_styled_titles_without_hash_author(tmp_path: Path):
    source = tmp_path / "styled.docx"
    make_styled_docx(
        source,
        [
            ("261期 20260912-0918", 52, True),
            ("《Game》正式发售", 22, False),
            ("新作", 32, True),
            ("《Game》正式发售", 30, True),
            ("正文 https://official.example/news", 22, False),
            ("汉化", 32, True),
            ("《Translated Game》汉化发布", 30, True),
            ("汉化正文 https://translation.example/news", 22, False),
        ],
    )

    issue = DocxDocumentParser().parse(source, "261")

    assert [entry.section for entry in issue.entries] == ["新作", "汉化"]
    assert issue.entries[0].title == "《Game》正式发售"
    assert issue.entries[0].author is None
    assert issue.entries[0].source_urls == ["https://official.example/news"]
    assert issue.entries[1].author is None
    assert issue.entries[1].source_urls == ["https://translation.example/news"]


def test_rule_analyzer_extracts_entities_event_date_and_image_need():
    draft = type("Draft", (), {})()
    draft.issue_id = "259"
    draft.input_path = "input/259.docx"
    draft.published_at = None
    draft.entries = [
        NewsDraft(
            sequence=1,
            section="新作",
            title="《Happy Weekend》主机板制作完成",
            body="HOOKSOFT于2026年9月4日公布了两张CG，由原画綾瀬はづき绘制。",
            source_urls=["https://official.example/news"],
        )
    ]

    issue = RuleBasedNewsAnalyzer().analyze(draft)
    item = issue.news_items[0]

    assert item.game_names == ["Happy Weekend"]
    assert "HOOKSOFT" in item.organizations
    assert "綾瀬はづき" in item.people
    assert item.event_type is EventType.UPDATE
    assert item.event_at == datetime(2026, 9, 4, tzinfo=timezone.utc)
    assert item.image_need is ImageNeed.EXPLICIT_NEW_IMAGE
    assert item.keywords
    assert item.importance > 0


def test_rule_analyzer_does_not_treat_future_feature_cg_as_new_image():
    draft = type("Draft", (), {})()
    draft.issue_id = "259"
    draft.input_path = "weekly.docx"
    draft.published_at = None
    draft.entries = [
        NewsDraft(
            sequence=1,
            section="新作",
            title="《Future Game》发售日公布",
            body="预计于2026年11月发售，游戏包含20张CG。",
        )
    ]

    item = RuleBasedNewsAnalyzer().analyze(draft).news_items[0]

    assert item.event_type is EventType.RELEASE
    assert item.image_need is ImageNeed.UNKNOWN


def test_special_scene_cg_publication_is_an_explicit_image_need():
    assert detect_image_need("官方公布了一张特殊场景CG，现已在官网开放查看。") == (ImageNeed.EXPLICIT_NEW_IMAGE, 1)


def test_optional_llm_without_configuration_returns_rule_result_without_network():
    draft = type("Draft", (), {})()
    draft.issue_id = "259"
    draft.input_path = "weekly.docx"
    draft.published_at = None
    draft.entries = [NewsDraft(sequence=1, section="新作", title="《Game》更新", body="官方更新。")]

    fallback = RuleBasedNewsAnalyzer()
    result = OpenAINewsAnalyzer(fallback=fallback).analyze(draft)

    assert result.news_items[0].game_names == ["Game"]
    assert result.news_items[0].event_type is EventType.UPDATE
    assert result.news_items[0].id == fallback.analyze(draft).news_items[0].id


def test_optional_llm_invalid_structured_output_falls_back_and_records_failure():
    draft = type("Draft", (), {})()
    draft.issue_id = "259"
    draft.input_path = "weekly.docx"
    draft.published_at = None
    draft.entries = [NewsDraft(sequence=1, section="新作", title="《Game》更新", body="官方更新。")]

    analyzer = OpenAINewsAnalyzer(
        fallback=RuleBasedNewsAnalyzer(),
        provider="openai",
        model="test-model",
        api_key="test-key",
        invoker=lambda _: {"not": "a valid result"},
    )
    result = analyzer.analyze(draft)

    assert result.news_items[0].event_type is EventType.UPDATE
    assert analyzer.last_failures and analyzer.last_failures[0].code == "llm_invalid_output"
