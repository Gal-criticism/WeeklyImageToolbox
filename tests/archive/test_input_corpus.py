from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from galgame_news.archive.index import ArchiveIndex, normalize_issue_number
from galgame_news.archive.models import ArchiveAsset
from galgame_news.ingestion.docx_parser import DocxDocumentParser


INPUT_ROOT = Path(__file__).resolve().parents[2] / "input"


def test_local_input_corpus_has_22_parseable_weeklies_and_500_observed_news():
    files = sorted(INPUT_ROOT.glob("*.docx"))
    if not files:
        pytest.skip("local input DOCX corpus is unavailable in CI")

    assert len(files) == 22
    parser = DocxDocumentParser()
    total_news = 0
    for source in files:
        issue = normalize_issue_number(source.stem)
        parsed = parser.parse(source, issue)
        assert parsed.issue_id == issue
        total_news += len(parsed.entries)

    # Corpus observation for parser regression only; this is not an accuracy score.
    assert total_news == 500


def test_issue_copy_suffix_normalization_is_used_by_corpus_loader():
    assert normalize_issue_number("226_副本") == "226"
    assert normalize_issue_number("228_副本") == "228"


def test_real_issue_231_riruka_uses_latest_two_matching_issues_without_future(tmp_path):
    current_path = INPUT_ROOT / "231.docx"
    if not current_path.is_file():
        pytest.skip("local input DOCX corpus is unavailable in CI")
    parsed = DocxDocumentParser().parse(current_path, "231")
    news = next(entry for entry in parsed.entries if "リルカ" in entry.title)
    game = news.title.removesuffix("制作完成")
    index = ArchiveIndex(tmp_path / "archive.sqlite")
    index.replace_source("local", [
        ArchiveAsset("img-228", "local", "image", "228", "228/a.png", "a.png", (game,), "2026-10-01"),
        ArchiveAsset("vid-228", "local", "video", "228", "228/a.mp4", "a.mp4", (game,), "2026-10-01"),
        ArchiveAsset("img-229", "local", "image", "229", "229/a.png", "a.png", (game,), "2026-10-10"),
        ArchiveAsset("vid-229", "local", "video", "229", "229/a.mp4", "a.mp4", (game,), "2026-10-10"),
        ArchiveAsset("img-230", "local", "image", "230", "230/a.png", "a.png", (game,), "2026-10-20"),
        ArchiveAsset("img-232", "local", "image", "232", "232/a.png", "a.png", (game,), "2026-11-01"),
        ArchiveAsset("other-img-230", "local", "image", "230", "230/b.png", "b.png", ("OtherGame",), "2026-10-20"),
        ArchiveAsset("other-video-230", "local", "video", "230", "230/b.mp4", "b.mp4", ("OtherGame",), "2026-10-20"),
    ])

    found = index.search((game,), current_issue="231", now=datetime(2026, 10, 27, tzinfo=timezone.utc))
    found_without_videos = index.search((game,), current_issue="231", no_videos=True,
                                        now=datetime(2026, 10, 27, tzinfo=timezone.utc))

    assert found[0].asset_id == "img-230"
    assert {asset.asset_id for asset in found} == {"img-230", "img-229", "vid-229"}
    assert [asset.asset_id for asset in found_without_videos] == ["img-230", "img-229"]
    assert all(asset.issue_number != "232" for asset in found)
    assert all(asset.issue_number != "228" for asset in found)
    other_without_video = index.search(("OtherGame",), current_issue="231", no_videos=True,
                                       now=datetime(2026, 10, 27, tzinfo=timezone.utc))
    assert [(asset.kind, asset.issue_number) for asset in other_without_video] == [("image", "230")]
    index.close()
