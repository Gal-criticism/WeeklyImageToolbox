"""OOXML DOCX parser and news-entry segmentation."""

from __future__ import annotations

from dataclasses import dataclass
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from ..domain import IssueDraft, NewsDraft


URL_RE = re.compile(r"https?://", re.IGNORECASE)
HEADING_RE = re.compile(r"^\s*(?P<title>[^#\n]+?)\s*#\s*(?P<author>[^#\n]+?)\s*#?\s*$")
SECTION_HINTS = {
    "新作", "旧作", "舊作", "动画", "動畫", "周边", "周邊", "业界", "業界", "其他", "其它", "汉化", "漢化",
}
PREAMBLE_HINTS = {"目录", "目錄", "索引", "内容", "內容", "周报目录", "周報目錄"}
_WORD_NAMESPACE = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


@dataclass(frozen=True)
class _ParagraphInfo:
    text: str
    hyperlink_urls: tuple[str, ...]
    all_runs_bold: bool
    font_sizes: frozenset[int]


def _paragraph_infos(path: Path | str) -> list[_ParagraphInfo]:
    try:
        with zipfile.ZipFile(path) as archive:
            xml = archive.read("word/document.xml")
            try:
                rels_root = ET.fromstring(archive.read("word/_rels/document.xml.rels"))
            except KeyError:
                rels_root = None
    except (OSError, zipfile.BadZipFile, KeyError) as exc:
        raise ValueError(f"无法读取 DOCX：{exc}") from exc
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise ValueError(f"DOCX XML 无效：{exc}") from exc

    relationships = {}
    if rels_root is not None:
        relationships = {rel.get("Id"): rel.get("Target", "") for rel in rels_root}
    paragraphs: list[_ParagraphInfo] = []
    for paragraph in root.iter(_WORD_NAMESPACE + "p"):
        pieces: list[str] = []
        hyperlink_urls: list[str] = []
        for node in paragraph.iter():
            if node.tag == _WORD_NAMESPACE + "t":
                pieces.append(node.text or "")
            elif node.tag == _WORD_NAMESPACE + "tab":
                pieces.append("\t")
            elif node.tag in {_WORD_NAMESPACE + "br", _WORD_NAMESPACE + "cr"}:
                pieces.append("\n")
            elif node.tag == _WORD_NAMESPACE + "hyperlink":
                target = relationships.get(node.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"), "")
                if target.lower().startswith(("http://", "https://")):
                    hyperlink_urls.append(target)
        text = "".join(pieces)
        text = text.strip()
        if not text:
            continue
        runs = []
        for run in paragraph.iter(_WORD_NAMESPACE + "r"):
            run_text = "".join(node.text or "" for node in run.iter(_WORD_NAMESPACE + "t"))
            if not run_text:
                continue
            properties = run.find(_WORD_NAMESPACE + "rPr")
            bold = False
            size: int | None = None
            if properties is not None:
                bold_node = properties.find(_WORD_NAMESPACE + "b")
                if bold_node is not None:
                    value = bold_node.get(_WORD_NAMESPACE + "val", "true").casefold()
                    bold = value not in {"0", "false", "off", "no"}
                size_node = properties.find(_WORD_NAMESPACE + "sz")
                if size_node is not None:
                    try:
                        size = int(size_node.get(_WORD_NAMESPACE + "val", ""))
                    except (TypeError, ValueError):
                        size = None
            runs.append((bold, size))
        paragraphs.append(
            _ParagraphInfo(
                text=text,
                hyperlink_urls=tuple(hyperlink_urls),
                all_runs_bold=bool(runs) and all(bold for bold, _ in runs),
                font_sizes=frozenset(size for _, size in runs if size is not None),
            )
        )
    return paragraphs


def parse_docx_paragraphs(path: Path | str) -> list[str]:
    return [paragraph.text for paragraph in _paragraph_infos(path)]


def split_urls(text: str) -> list[str]:
    # Keep literal schemes used as query values (for example lurl=https://...),
    # while recognizing pasted URLs even when they touch the preceding URL.
    starts = [
        match.start()
        for match in re.finditer(r"https?://", text, re.IGNORECASE)
        if match.start() == 0 or text[match.start() - 1] != "="
    ]
    if not starts:
        return []
    result: list[str] = []
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(text)
        value = text[start:end].strip()
        value = re.split(r"[\s，。；;、！？]", value, maxsplit=1)[0].rstrip(")]】")
        if value:
            result.append(value)
    return result


def _heading(line: str) -> re.Match[str] | None:
    return HEADING_RE.match(line) if "#" in line and not URL_RE.search(line) else None


def _section_label(line: str) -> bool:
    cleaned = line.strip()
    return bool(
        cleaned
        and len(cleaned) <= 16
        and cleaned not in PREAMBLE_HINTS
        and "#" not in cleaned
        and not URL_RE.search(cleaned)
        and not re.search(r"[。！？.!?]$", cleaned)
        and (cleaned in SECTION_HINTS or re.fullmatch(r"[汉漢]化(?:信息|情報|新闻|新聞)?", cleaned))
    )


def _styled_title_candidate(paragraphs: list[_ParagraphInfo], index: int) -> bool:
    """Recognize the stable 261 title style without treating all bold text as a title."""

    paragraph = paragraphs[index]
    if (
        not paragraph.all_runs_bold
        or paragraph.font_sizes != frozenset({30})
        or "#" in paragraph.text
        or URL_RE.search(paragraph.text)
    ):
        return False
    if index + 1 >= len(paragraphs):
        return False
    following = paragraphs[index + 1]
    return not following.all_runs_bold and following.font_sizes == frozenset({22})


class DocxDocumentParser:
    """Parse an editorial DOCX without requiring Microsoft Word."""

    def parse(self, path: Path, issue_id: str) -> IssueDraft:
        input_path = Path(path)
        if input_path.suffix.lower() != ".docx" or not input_path.is_file():
            raise ValueError("输入必须是存在的 .docx 文件")
        paragraphs = _paragraph_infos(input_path)
        entries: list[NewsDraft] = []
        section = "新作"
        section_active = False
        current: dict | None = None
        for index, paragraph in enumerate(paragraphs):
            line = paragraph.text
            heading = _heading(line)
            styled_heading = section_active and _styled_title_candidate(paragraphs, index)
            if heading or styled_heading:
                if current:
                    entries.append(NewsDraft(**current))
                current = {
                    "sequence": len(entries) + 1,
                    "section": section,
                    "title": heading.group("title").strip() if heading else line,
                    "author": heading.group("author").strip() or None if heading else None,
                    "body": "",
                    "source_urls": [],
                }
                continue
            next_is_heading = index + 1 < len(paragraphs) and (
                _heading(paragraphs[index + 1].text)
                or _styled_title_candidate(paragraphs, index + 1)
            )
            if next_is_heading and _section_label(line):
                if current:
                    entries.append(NewsDraft(**current))
                    current = None
                section = line.strip()
                section_active = True
                continue
            if current is None:
                # Preamble and unrecognised standalone labels are not news.
                continue
            current["body"] = f"{current['body']}\n{line}".strip()
            current["source_urls"].extend(split_urls(line))
            current["source_urls"].extend(paragraph.hyperlink_urls)
        if current:
            entries.append(NewsDraft(**current))
        for entry in entries:
            entry.source_urls = list(dict.fromkeys(entry.source_urls))
        return IssueDraft(issue_id=issue_id, input_path=str(input_path), entries=entries)
