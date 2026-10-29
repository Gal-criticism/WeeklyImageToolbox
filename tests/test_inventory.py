from pathlib import Path

from PIL import Image

from galgame_news.inventory import (
    FileRecord, InventoryManifest, ScanRoot, save_manifest, scan_inventory,
)


def _png(path: Path, color=(255, 0, 0)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (3, 2), color).save(path)


def _unique_png(path: Path, index: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (index + 1, 1), (index % 256, (index // 256) % 256, 17)).save(path)


def test_scan_covers_growth_and_validates_images(tmp_path):
    root = tmp_path / "source"
    for i in range(118):
        _unique_png(root / f"{i:03}.png", i)
    first = scan_inventory([ScanRoot(root, "originals")])
    assert first.complete and first.stats.files == 118
    for i in range(118, 322):
        _unique_png(root / f"{i:03}.png", i)
    second = scan_inventory([ScanRoot(root, "originals")], previous=first.manifest)
    assert second.complete and second.stats.files == 322
    assert len(second.changes.added) == 204
    assert second.stats.usable_files == 322


def test_same_size_replacement_detected_by_full_hash(tmp_path):
    path = tmp_path / "same.png"
    _png(path, (1, 2, 3))
    prior = scan_inventory([ScanRoot(tmp_path, "originals")]).manifest
    original_size = path.stat().st_size
    path.write_bytes(b"B" * original_size)
    assert path.stat().st_size == original_size
    result = scan_inventory([ScanRoot(tmp_path, "originals")], previous=prior)
    assert result.changes.modified == [str(path.resolve())]


def test_content_move_is_reported_without_remove_or_add(tmp_path):
    old = tmp_path / "old.png"
    new = tmp_path / "new.png"
    _png(old)
    prior = scan_inventory([ScanRoot(tmp_path, "originals")]).manifest
    old.rename(new)
    result = scan_inventory([ScanRoot(tmp_path, "originals")], previous=prior)
    assert result.changes.moved == [(str(old.resolve()), str(new.resolve()))]
    assert not result.changes.removed and not result.changes.added


def test_partial_cancel_cannot_replace_baseline(tmp_path):
    for i in range(5):
        _png(tmp_path / f"{i}.png", (i, 0, 0))
    calls = 0

    def cancel():
        nonlocal calls
        calls += 1
        return calls > 2

    result = scan_inventory([ScanRoot(tmp_path, "review")], cancel=cancel)
    assert not result.complete and result.cancelled
    try:
        save_manifest(tmp_path / "manifest.json", result.manifest, complete=result.complete)
    except ValueError:
        pass
    else:
        raise AssertionError("partial scan replaced baseline")


def test_roles_remain_distinct_but_physical_content_is_unique(tmp_path):
    path = tmp_path / "asset.png"
    _png(path)
    result = scan_inventory([ScanRoot(tmp_path, "originals"), ScanRoot(tmp_path, "review")])
    assert result.stats.files == 2
    assert result.stats.physical_unique == 1
    assert result.stats.content_unique == 1
    assert result.stats.usable_unique == 1
    assert result.stats.content_unique_by_role == {"originals": 1, "review": 1}
    assert {record.role for record in result.manifest.records} == {"originals", "review"}


def test_skip_cache_previews_and_symlink_directories(tmp_path):
    root = tmp_path / "root"
    for name in ("keep", "cache", "previews", "thumbnails", "tmp"):
        (root / name).mkdir(parents=True)
        _png(root / name / "file.png")
    (root / "keep" / "metadata.json").write_text("{}")
    (root / "keep" / "draft.tmp.png").write_bytes(b"partial")
    result = scan_inventory([ScanRoot(root, "final")])
    assert [Path(record.path).parent.name for record in result.manifest.records] == ["keep"]


def test_partial_scan_does_not_report_unscanned_files_removed(tmp_path):
    paths = [tmp_path / f"{i}.png" for i in range(4)]
    for i, path in enumerate(paths):
        _png(path, (i, 1, 2))
    previous = scan_inventory([ScanRoot(tmp_path, "originals")]).manifest
    (tmp_path / "0.png").unlink()
    calls = 0

    def cancel():
        nonlocal calls
        calls += 1
        return calls > 2

    result = scan_inventory([ScanRoot(tmp_path, "originals")], previous=previous, cancel=cancel)
    assert not result.complete
    assert result.changes.removed == []


def test_identical_existing_path_is_not_misreported_as_move(tmp_path):
    old, existing, new = (tmp_path / name for name in ("old.png", "existing.png", "new.png"))
    _png(old)
    _png(existing)
    prior = scan_inventory([ScanRoot(tmp_path, "originals")]).manifest
    old.unlink()
    _png(new)
    result = scan_inventory([ScanRoot(tmp_path, "originals")], previous=prior)
    assert not result.changes.moved
    assert str(old.resolve()) in result.changes.ambiguous
    assert str(new.resolve()) in result.changes.ambiguous


def test_atomic_manifest_round_trip(tmp_path):
    path = tmp_path / "manifest.json"
    result = scan_inventory([ScanRoot(tmp_path, "originals")])
    save_manifest(path, result.manifest, complete=True)
    from galgame_news.inventory import load_manifest
    assert load_manifest(path) == result.manifest
