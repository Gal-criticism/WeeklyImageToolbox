from datetime import datetime, timezone
import sqlite3

from galgame_news.archive.index import ArchiveIndex, normalize_issue_number
from galgame_news.archive.models import ArchiveAsset


def asset(asset_id, *, source="local", game="Game", issue="226", kind="image", date="2026-10-01", path=None, **kwargs):
    return ArchiveAsset(
        asset_id=asset_id,
        source_id=source,
        kind=kind,
        issue_number=issue,
        path=path or f"{issue}/{asset_id}.jpg",
        name=f"{asset_id}.jpg",
        game_names=(game,),
        issue_date=date,
        source_kind="local" if source == "local" else "nextcloud",
        local_path=path,
        **kwargs,
    )


def test_archive_asset_round_trip_preserves_all_fields():
    original = asset("a", original_url="https://cloud.example/a", version="DLC")
    assert ArchiveAsset.from_dict(original.to_dict()) == original


def test_issue_number_normalization_handles_nfkc_and_copy_suffix():
    assert normalize_issue_number("２２６_副本") == "226"
    assert normalize_issue_number("226_副本") == "226"


def test_issue_number_normalization_rejects_embedded_brand_numbers():
    assert normalize_issue_number("TGGA2026") == ""
    assert normalize_issue_number("game2") == ""
    assert normalize_issue_number("brand/226/assets") == ""
    assert normalize_issue_number("第２２６期") == "226"
    assert normalize_issue_number("226周报") == "226"


def test_search_returns_two_latest_earlier_issues_for_game(tmp_path):
    index = ArchiveIndex(tmp_path / "archive.sqlite")
    index.replace_source("local", [
        asset("old", issue="224", date="2026-09-17"),
        asset("near", issue="225", date="2026-09-24"),
        asset("current", issue="226", date="2026-10-01"),
    ])
    found = index.search(("Game",), current_issue="226", now=datetime(2026, 10, 27, tzinfo=timezone.utc))
    assert {a.asset_id for a in found} == {"near", "old"}
    index.close()


def test_no_videos_filters_two_selected_issues_without_falling_back_to_third(tmp_path):
    index = ArchiveIndex(tmp_path / "archive.sqlite")
    index.replace_source("local", [
        asset("old-video", issue="224", kind="video", date="2026-09-17"),
        asset("near-image", issue="225", kind="image", date="2026-09-24"),
        asset("near-video-missing", issue="225", kind="image", date="2026-09-24"),
    ])
    found = index.search(("Game",), current_issue="226", now=datetime(2026, 10, 27, tzinfo=timezone.utc))
    assert {a.asset_id for a in found} == {"old-video", "near-image", "near-video-missing"}
    images = index.search(("Game",), current_issue="226", no_videos=True,
                          now=datetime(2026, 10, 27, tzinfo=timezone.utc))
    assert {a.asset_id for a in images} == {"near-image", "near-video-missing"}
    index.close()


def test_search_range_all_includes_old_issue_and_deduplication_is_per_issue(tmp_path):
    index = ArchiveIndex(tmp_path / "archive.sqlite")
    index.replace_source("local", [
        asset("year-old", issue="224", date="2024-01-01", metadata={"candidate_id": "repeat"}),
        asset("recent", issue="225", date="2026-09-24", metadata={"candidate_id": "repeat"}),
        asset("unrelated", issue="225", game="OtherGame", date="2026-09-24"),
        asset("current", issue="226", date="2026-10-01"),
        asset("future", issue="227", date="2026-10-08"),
    ])
    # Replace the remote source separately so local/remote duplicate selection is exercised.
    index.replace_source("remote", [
        asset("year-old-copy", source="remote", issue="224", date="2024-01-01", metadata={"candidate_id": "repeat"}),
    ])
    now = datetime(2026, 10, 27, tzinfo=timezone.utc)
    assert {item.asset_id for item in index.search(("Game",), "226", now=now)} == {"recent"}
    all_found = index.search(("Game",), "226", days=None, now=now)
    assert [item.asset_id for item in all_found] == ["recent", "year-old"]
    assert [item.issue_number for item in all_found] == ["225", "224"]
    index.close()


def test_search_prefers_local_copy_across_sources(tmp_path):
    index = ArchiveIndex(tmp_path / "archive.sqlite")
    index.replace_source("remote", [asset("same-id", source="remote", issue="225", date="2026-09-24",
                                            metadata={"candidate_id": "same-candidate"})])
    index.replace_source("local", [asset("same-id", issue="225", date="2026-09-24",
                                          metadata={"candidate_id": "same-candidate"})])
    found = index.search(("Game",), current_issue="226", now=datetime(2026, 10, 27, tzinfo=timezone.utc))
    assert [a.source_id for a in found] == ["local"]
    assert len(index.list_assets()) == 2
    index.close()


def test_suspicious_old_date_is_retained_but_default_range_filters_it(tmp_path):
    index = ArchiveIndex(tmp_path / "archive.sqlite")
    old = asset("old", issue="225", date="2024-01-01")
    index.replace_source("local", [old])
    all_assets = index.list_assets()
    assert len(all_assets) == 1
    assert all_assets[0].date_suspect is False
    assert index.search(("Game",), current_issue="226", now=datetime(2026, 10, 27, tzinfo=timezone.utc)) == []
    index.close()


def test_explicit_suspect_date_is_retained_even_when_outside_search_range(tmp_path):
    index = ArchiveIndex(tmp_path / "archive.sqlite")
    old = asset("old-uncertain", issue="225", date="2020-01-01", date_suspect=True)
    index.replace_source("local", [old])
    found = index.search(("Game",), current_issue="226", now=datetime(2026, 10, 27, tzinfo=timezone.utc))
    assert [a.asset_id for a in found] == ["old-uncertain"]
    assert index.list_assets()[0].date_suspect is True
    index.close()


def test_replace_source_marks_date_order_conflict_without_rewriting_dates(tmp_path):
    index = ArchiveIndex(tmp_path / "archive.sqlite")
    older_issue = asset("issue-225", issue="225", date="2026-10-10")
    newer_issue = asset("issue-226", issue="226", date="2026-10-01")
    index.replace_source("local", [older_issue, newer_issue])
    indexed = {item.asset_id: item for item in index.list_assets()}
    assert indexed["issue-226"].date_suspect is True
    assert indexed["issue-226"].issue_date == "2026-10-01"
    index.close()


def test_search_uses_issue_date_not_filesystem_mtime(tmp_path):
    index = ArchiveIndex(tmp_path / "archive.sqlite")
    index.replace_source("local", [asset("a", issue="225", date="2026-09-24", path="unused")])
    found = index.search(("Game",), current_issue="226", now=datetime(2026, 10, 27, tzinfo=timezone.utc))
    assert [a.asset_id for a in found] == ["a"]
    index.close()


def test_search_matches_nfkc_alias_with_same_version_only(tmp_path):
    index = ArchiveIndex(tmp_path / "archive.sqlite")
    index.replace_source("local", [
        asset("deluxe", issue="225", date="2026-09-24", game="Visual Novel", version="Deluxe"),
        asset("standard", issue="225", date="2026-09-24", game="Visual Novel", version="Standard"),
    ])
    found = index.search(("Ｖｉｓｕａｌ　Ｎｏｖｅｌ（Ｄｅｌｕｘｅ）",), current_issue="226", now=datetime(2026, 10, 27, tzinfo=timezone.utc))
    assert [a.asset_id for a in found] == ["deluxe"]
    index.close()


def test_version_matching_uses_token_boundaries(tmp_path):
    index = ArchiveIndex(tmp_path / "archive.sqlite")
    index.replace_source("local", [
        asset("dlc", issue="225", date="2026-09-24", game="Game", version="DLC"),
        asset("dlc2", issue="225", date="2026-09-24", game="Game", version="DLC2"),
    ])
    found = index.search(("Game DLC2",), current_issue="226", now=datetime(2026, 10, 27, tzinfo=timezone.utc))
    assert [a.asset_id for a in found] == ["dlc2"]
    index.close()


def test_replace_source_is_atomic_and_updates_timestamp(tmp_path):
    index = ArchiveIndex(tmp_path / "archive.sqlite")
    index.replace_source("local", [asset("original")])
    first_updated = index.last_updated
    try:
        index.replace_source("local", [asset("same"), asset("same")])
    except sqlite3.IntegrityError:
        pass
    else:
        raise AssertionError("invalid replacement should fail")
    assert [a.asset_id for a in index.list_assets()] == ["original"]
    assert index.last_updated >= first_updated
    index.close()


def test_public_weekly_intro_outro_are_not_game_matches(tmp_path):
    index = ArchiveIndex(tmp_path / "archive.sqlite")
    index.replace_source("local", [
        asset("intro", game="weeklyintro", issue="225"),
        asset("outro", game="weeklyoutro", issue="225"),
        asset("real", game="Game", issue="225"),
    ])
    found = index.search(("weeklyintro", "weeklyoutro"), current_issue="226", now=datetime(2026, 10, 27, tzinfo=timezone.utc))
    assert found == []
    index.close()


def test_same_issue_prefers_selected_then_natural_filename_and_local_same_candidate(tmp_path):
    index = ArchiveIndex(tmp_path / "archive.sqlite")
    index.replace_source("remote", [
        ArchiveAsset("ten", "remote", "image", "225", "10.jpg", "10.jpg", ("Game",), "2026-09-24", source_kind="nextcloud", metadata={"candidate_id": "ten-candidate"}),
        ArchiveAsset("two", "remote", "image", "225", "2.jpg", "2.jpg", ("Game",), "2026-09-24", source_kind="nextcloud", metadata={}),
        ArchiveAsset("z-selected", "remote", "image", "225", "10.jpg", "10.jpg", ("Game",), "2026-09-24", source_kind="nextcloud", metadata={"candidate_id": "ten-candidate", "selected": True}),
    ])
    found = index.search(("Game",), current_issue="226", now=datetime(2026, 10, 27, tzinfo=timezone.utc))
    assert [asset.asset_id for asset in found] == ["z-selected", "two"]
    index.replace_source("remote", [
        ArchiveAsset("ten", "remote", "image", "225", "10.jpg", "10.jpg", ("Game",), "2026-09-24", source_kind="nextcloud", metadata={"candidate_id": "ten-candidate"}),
        ArchiveAsset("two", "remote", "image", "225", "2.jpg", "2.jpg", ("Game",), "2026-09-24", source_kind="nextcloud", metadata={"candidate_id": "two-candidate"}),
    ])
    assert [asset.asset_id for asset in index.search(("Game",), current_issue="226", now=datetime(2026, 10, 27, tzinfo=timezone.utc))] == ["two", "ten"]
    index.replace_source("local", [ArchiveAsset("two-local", "local", "image", "225", "2.jpg", "2.jpg", ("Game",), "2026-09-24", source_kind="local", metadata={"candidate_id": "two"})])
    index.replace_source("remote", [ArchiveAsset("two", "remote", "image", "225", "2.jpg", "2.jpg", ("Game",), "2026-09-24", source_kind="nextcloud", metadata={"candidate_id": "two"})])
    assert [asset.asset_id for asset in index.search(("Game",), current_issue="226", now=datetime(2026, 10, 27, tzinfo=timezone.utc))] == ["two-local"]
    index.close()


def test_candidate_ids_do_not_merge_distinct_news_directories(tmp_path):
    index = ArchiveIndex(tmp_path / "archive.sqlite")
    index.replace_source("remote", [
        ArchiveAsset("am-1", "remote", "image", "225", "225/AMBITIOUS MISSION/1.png", "1.png",
                     ("AMBITIOUS MISSION",), "2026-09-24", news_title="AMBITIOUS MISSION campaign",
                     source_kind="nextcloud", metadata={"candidate_id": "reused-id"}),
        ArchiveAsset("am-2", "remote", "image", "225", "225/AMBITIOUS MISSION/other/1.png", "1.png",
                     ("AMBITIOUS MISSION",), "2026-09-24", news_title="AMBITIOUS MISSION official update",
                     source_kind="nextcloud", metadata={"candidate_id": "reused-id"}),
        ArchiveAsset("am-3", "remote", "image", "225", "225/AMBITIOUS MISSION/third/1.png", "1.png",
                     ("AMBITIOUS MISSION",), "2026-09-24", news_title="AMBITIOUS MISSION campaign",
                     source_kind="nextcloud", metadata={"candidate_id": "reused-id"}),
        ArchiveAsset("am-4", "remote", "image", "225", "225/AMBITIOUS MISSION/fourth/1.png", "1.png",
                     ("AMBITIOUS MISSION",), "2026-09-24", news_title="",
                     source_kind="nextcloud", metadata={"candidate_id": "reused-id"}),
        ArchiveAsset("am-5", "remote", "image", "225", "225/AMBITIOUS MISSION/fifth/1.png", "1.png",
                     ("AMBITIOUS MISSION",), "2026-09-24", news_title="",
                     source_kind="nextcloud", metadata={"candidate_id": "reused-id"}),
        ArchiveAsset("sl-1", "remote", "image", "225", "225/Sugar Lies/1.png", "1.png",
                     ("Sugar Lies",), "2026-09-24", news_title="Sugar Lies update",
                     source_kind="nextcloud", metadata={"candidate_id": "reused-id"}),
    ])

    ambitious = index.search(("ＡＭＢＩＴＩＯＵＳ　ＭＩＳＳＩＯＮ",), "226",
                             now=datetime(2026, 10, 27, tzinfo=timezone.utc))
    sugar_lies = index.search(("Sugar Lies",), "226",
                              now=datetime(2026, 10, 27, tzinfo=timezone.utc))

    assert {item.asset_id for item in ambitious} == {"am-1", "am-2", "am-3", "am-4", "am-5"}
    assert [item.asset_id for item in sugar_lies] == ["sl-1"]
    index.close()


def test_game_versions_and_multigame_news_titles_are_not_fuzzy_matches(tmp_path):
    index = ArchiveIndex(tmp_path / "archive.sqlite")
    index.replace_source("local", [
        ArchiveAsset("am-main", "local", "image", "225", "225/am/main.png", "main.png",
                     ("AMBITIOUS MISSION",), "2026-09-24", news_title="AMBITIOUS MISSION update"),
        ArchiveAsset("am-fd", "local", "image", "225", "225/am-fd/fd.png", "fd.png",
                     ("AMBITIOUS MISSION",), "2026-09-24", news_title="AMBITIOUS MISSION FD",
                     version="FD"),
        ArchiveAsset("am-after", "local", "image", "225", "225/am-after/after.png", "after.png",
                     ("AMBITIOUS MISSION",), "2026-09-24", news_title="AMBITIOUS MISSION after story",
                     version="afterstory"),
        ArchiveAsset("am2-fd", "local", "image", "225", "225/am2-fd/fd.png", "fd.png",
                     ("AMBITIOUS MISSION",), "2026-09-24", news_title="AMBITIOUS MISSION 2 FD",
                     version="FD"),
        ArchiveAsset("sl-remake-missing-version", "local", "image", "225", "225/sl-remake/1.png", "1.png",
                     ("Sugar Lies",), "2026-09-24", news_title="Sugar Lies remake announcement"),
        ArchiveAsset("sl-remake-path", "local", "image", "225", "225/Sugar Lies Remake/2.png", "2.png",
                     ("Sugar Lies",), "2026-09-24", news_title=""),
        ArchiveAsset("am-after-missing-version", "local", "image", "225", "225/am-after/2.png", "2.png",
                     ("AMBITIOUS MISSION",), "2026-09-24", news_title="AMBITIOUS MISSION afterstory"),
        ArchiveAsset("sl-fd", "local", "image", "225", "225/sugar-fd/fd.png", "fd.png",
                     ("Sugar Lies",), "2026-09-24", news_title="Sugar Lies FD", version="FD"),
        # A real multi-work news entry is ambiguous unless this asset has its own assignment.
        ArchiveAsset("multi", "local", "image", "225", "225/multi/shared.png", "shared.png",
                     ("AMBITIOUS MISSION", "Sugar Lies"), "2026-09-24",
                     news_title="AMBITIOUS MISSION × Sugar Lies collaboration"),
        ArchiveAsset("multi-assigned", "local", "image", "225", "225/multi/am.png", "am.png",
                     ("AMBITIOUS MISSION", "Sugar Lies"), "2026-09-24",
                     news_title="AMBITIOUS MISSION × Sugar Lies collaboration",
                     metadata={"asset_game_name": "AMBITIOUS MISSION"}),
        ArchiveAsset("generic-folder", "local", "image", "225", "225/am-generic/1.png", "1.png",
                     ("AMBITIOUS MISSION",), "2026-09-24", news_title="AMBITIOUS MISSION news"),
    ])

    now = datetime(2026, 10, 27, tzinfo=timezone.utc)
    assert {item.asset_id for item in index.search(("AMBITIOUS MISSION",), "226", now=now)} == {
        "am-main", "generic-folder", "multi-assigned"
    }
    assert [item.asset_id for item in index.search(("AMBITIOUS MISSION FD",), "226", now=now)] == ["am-fd"]
    assert {item.asset_id for item in index.search(("AMBITIOUS MISSION afterstory",), "226", now=now)} == {
        "am-after", "am-after-missing-version"
    }
    assert [item.asset_id for item in index.search(("Sugar Lies FD",), "226", now=now)] == ["sl-fd"]
    assert [item.asset_id for item in index.search(("AMBITIOUS MISSION 2 FD",), "226", now=now)] == ["am2-fd"]
    assert index.search(("Sugar Lies",), "226", now=now) == []  # Known FD/remake entries do not count as the base work.
    assert [item.asset_id for item in index.search(("AMBITIOUS MISSION",), "226", now=now)
            if item.asset_id == "multi-assigned"] == ["multi-assigned"]
    assert {item.asset_id for item in index.search(("AMBITIOUS MISSION",), "226", days=None, now=now)} == {
        "am-main", "generic-folder", "multi-assigned"
    }
    index.close()


def test_japanese_edition_qualifiers_do_not_leak_into_the_base_work(tmp_path):
    index = ArchiveIndex(tmp_path / "archive.sqlite")
    index.replace_source("local", [
        ArchiveAsset("base", "local", "image", "225", "225/base/1.png", "1.png",
                     ("星の終わり",), "2026-09-24", news_title="『星の終わり』公式更新"),
        ArchiveAsset("fan-disc", "local", "image", "225", "225/fd/1.png", "1.png",
                     ("星の終わり",), "2026-09-24", news_title="『星の終わり』ファンディスク発売"),
        ArchiveAsset("after", "local", "image", "225", "225/after/1.png", "1.png",
                     ("星の終わり",), "2026-09-24", news_title="星の終わり アフターストーリー公開"),
        ArchiveAsset("remake", "local", "image", "225", "225/remake/1.png", "1.png",
                     ("星の終わり",), "2026-09-24", news_title="星の終わり リメイク決定"),
    ])
    now = datetime(2026, 10, 27, tzinfo=timezone.utc)

    assert [item.asset_id for item in index.search(("星の終わり",), "226", now=now)] == ["base"]
    assert [item.asset_id for item in index.search(("星の終わり FD",), "226", now=now)] == ["fan-disc"]
    assert [item.asset_id for item in index.search(("星の終わり afterstory",), "226", now=now)] == ["after"]
    assert [item.asset_id for item in index.search(("星の終わり remake",), "226", now=now)] == ["remake"]
    index.close()


def test_nextcloud_archive_url_is_not_reloaded_as_an_original_source():
    archive_url = "https://cloud.example/remote.php/dav/files/user/archive/226/game/1.png"
    legacy = ArchiveAsset(
        "legacy", "nextcloud", "image", "226", "/archive/226/game/1.png", "1.png",
        ("Game",), source_kind="nextcloud", original_url=archive_url,
        original_source_url=archive_url,
    )

    assert legacy.original_source_url is None
    assert legacy.original_url == archive_url


def test_search_returns_all_assets_from_two_latest_matching_issues_without_other_games(tmp_path):
    index = ArchiveIndex(tmp_path / "archive.sqlite")
    index.replace_source("local", [
        ArchiveAsset("old-image", "local", "image", "224", "224/Game/cover.png", "cover.png", ("Game",), "2026-09-17"),
        ArchiveAsset("old-video", "local", "video", "224", "224/Game/pv.mp4", "pv.mp4", ("Game",), "2026-09-17"),
        ArchiveAsset("same-name-a", "local", "image", "225", "225/Game/news-a/1.jpg", "1.jpg", ("Game",), "2026-09-24"),
        ArchiveAsset("same-name-b", "local", "image", "225", "225/Game/news-b/1.jpg", "1.jpg", ("Game",), "2026-09-24"),
        ArchiveAsset("punctuation-a", "local", "image", "225", "225/Game/news-a/a-b.jpg", "a-b.jpg", ("Game",), "2026-09-24"),
        ArchiveAsset("punctuation-b", "local", "image", "225", "225/Game/news-a/ab.jpg", "ab.jpg", ("Game",), "2026-09-24"),
        ArchiveAsset("image-2", "local", "image", "225", "225/Game/news-a/2.jpg", "2.jpg", ("Game",), "2026-09-24"),
        ArchiveAsset("video-1", "local", "video", "225", "225/Game/news-a/1.mp4", "1.mp4", ("Game",), "2026-09-24"),
        ArchiveAsset("video-2", "local", "video", "225", "225/Game/news-b/2.mp4", "2.mp4", ("Game",), "2026-09-24"),
        ArchiveAsset("other-game", "local", "image", "225", "225/OtherGame/1.jpg", "1.jpg", ("OtherGame",), "2026-09-24"),
        ArchiveAsset("current", "local", "image", "226", "226/Game/current.jpg", "current.jpg", ("Game",), "2026-10-01"),
    ])

    found = index.search(("Game",), current_issue="226", now=datetime(2026, 10, 27, tzinfo=timezone.utc))

    assert {asset.asset_id for asset in found} == {
        "old-image", "old-video",
        "same-name-a", "same-name-b", "punctuation-a", "punctuation-b",
        "image-2", "video-1", "video-2",
    }
    index.close()


def test_search_returns_all_latest_images_when_no_videos_is_selected(tmp_path):
    index = ArchiveIndex(tmp_path / "archive.sqlite")
    index.replace_source("local", [
        ArchiveAsset("image-1", "local", "image", "225", "225/Game/1.jpg", "1.jpg", ("Game",), "2026-09-24"),
        ArchiveAsset("image-2", "local", "image", "225", "225/Game/2.jpg", "2.jpg", ("Game",), "2026-09-24"),
        ArchiveAsset("video-1", "local", "video", "225", "225/Game/1.mp4", "1.mp4", ("Game",), "2026-09-24"),
        ArchiveAsset("video-2", "local", "video", "225", "225/Game/2.mp4", "2.mp4", ("Game",), "2026-09-24"),
    ])

    found = index.search(("Game",), current_issue="226", no_videos=True,
                         now=datetime(2026, 10, 27, tzinfo=timezone.utc))

    assert {asset.asset_id for asset in found} == {"image-1", "image-2"}
    index.close()
