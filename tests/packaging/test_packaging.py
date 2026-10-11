from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]


def _build_module():
    spec = importlib.util.spec_from_file_location("galgame_news_packaging_build", ROOT / "packaging" / "build.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_pyinstaller_spec_is_syntax_valid_and_targets_portable_executable():
    spec = ROOT / "packaging" / "GalgameNewsToolbox.spec"
    tree = ast.parse(spec.read_text(encoding="utf-8"), filename=str(spec))
    assert tree
    source = spec.read_text(encoding="utf-8")
    assert "WeeklyImageToolbox" in source
    assert "WeeklyImageToolbox.exe" in source
    assert "bin" in source
    assert "config" in source
    assert '"desktop_entry.py"' in source
    assert "console=False" in source
    assert 'contents_directory="."' in source


def test_pyinstaller_spec_excludes_unneeded_bindings_and_dev_science_stacks():
    source = (ROOT / "packaging" / "GalgameNewsToolbox.spec").read_text(encoding="utf-8")
    expected_excludes = {
        "PyQt5", "PyQt6", "PySide2", "tkinter", "pytest", "IPython",
        "sphinx", "black", "jedi", "docutils", "nbformat", "matplotlib", "numpy",
    }
    assert "excludes=EXCLUDED_OPTIONAL_MODULES" in source
    assert expected_excludes <= set(source.split('EXCLUDED_OPTIONAL_MODULES =', 1)[1].split(']', 1)[0].replace('"', '').replace(',', '').split())
    assert '"PySide6"' not in source


def test_desktop_build_entry_uses_absolute_import():
    source = (ROOT / "packaging" / "desktop_entry.py").read_text(encoding="utf-8")
    assert "from galgame_news.desktop.app import main" in source


def test_desktop_entry_has_writable_app_data_fallback():
    source = (ROOT / "packaging" / "desktop_entry.py").read_text(encoding="utf-8")
    assert "_prepare_writable_app_data" in source
    assert 'os.environ["LOCALAPPDATA"]' in source
    assert "tempfile.gettempdir" in source


def test_build_entry_is_importable_without_pyinstaller():
    source = (ROOT / "packaging" / "build.py").read_text(encoding="utf-8")
    tree = ast.parse(source, filename="build.py")
    assert tree
    assert "PyInstaller" in source
    assert "download" not in source.casefold()


def test_publish_bundle_rolls_back_only_runtime_files_on_copy_failure(monkeypatch, tmp_path):
    build = _build_module()
    staged = tmp_path / "staged"
    target = tmp_path / "dist" / "GalgameNewsToolbox"
    staged.mkdir()
    target.mkdir(parents=True)
    (staged / "a_new.txt").write_text("new", encoding="utf-8")
    (staged / "b_existing.txt").write_text("replacement", encoding="utf-8")
    (target / "b_existing.txt").write_text("original", encoding="utf-8")
    (target / "unknown.txt").write_text("keep", encoding="utf-8")
    output = target / "output"
    output.mkdir()
    (output / "user-data.txt").write_text("keep", encoding="utf-8")

    original_copy2 = build.shutil.copy2
    staged_copies = 0

    def flaky_copy2(source, destination, *args, **kwargs):
        nonlocal staged_copies
        if Path(source).resolve().is_relative_to(staged.resolve()):
            staged_copies += 1
            if staged_copies == 2:
                raise OSError("simulated runtime copy failure")
        return original_copy2(source, destination, *args, **kwargs)

    monkeypatch.setattr(build.shutil, "copy2", flaky_copy2)
    with pytest.raises(OSError, match="simulated runtime copy failure"):
        build.publish_bundle(staged, target)

    assert not (target / "a_new.txt").exists()
    assert (target / "b_existing.txt").read_text(encoding="utf-8") == "original"
    assert (target / "unknown.txt").read_text(encoding="utf-8") == "keep"
    assert (output / "user-data.txt").read_text(encoding="utf-8") == "keep"


def test_publish_bundle_replaces_stale_internal_runtime_but_preserves_output(tmp_path):
    build = _build_module()
    staged = tmp_path / "staged"
    target = tmp_path / "dist" / "GalgameNewsToolbox"
    (staged / "_internal" / "PySide6").mkdir(parents=True)
    (target / "_internal" / "PySide6").mkdir(parents=True)
    (target / "output").mkdir(parents=True)

    (staged / "_internal" / "PySide6" / "fresh.dll").write_text("new", encoding="utf-8")
    (target / "_internal" / "PySide6" / "stale.dll").write_text("old", encoding="utf-8")
    (target / "output" / "user-data.txt").write_text("keep", encoding="utf-8")

    build.publish_bundle(staged, target)

    assert (target / "_internal" / "PySide6" / "fresh.dll").read_text(encoding="utf-8") == "new"
    assert not (target / "_internal" / "PySide6" / "stale.dll").exists()
    assert (target / "output" / "user-data.txt").read_text(encoding="utf-8") == "keep"


def test_publish_bundle_restores_internal_runtime_when_copy_fails(monkeypatch, tmp_path):
    build = _build_module()
    staged = tmp_path / "staged"
    target = tmp_path / "dist" / "GalgameNewsToolbox"
    (staged / "_internal" / "PySide6").mkdir(parents=True)
    (target / "_internal" / "PySide6").mkdir(parents=True)
    (target / "output").mkdir(parents=True)

    (staged / "_internal" / "PySide6" / "fresh.dll").write_text("new", encoding="utf-8")
    (target / "_internal" / "PySide6" / "stale.dll").write_text("old", encoding="utf-8")
    (target / "output" / "user-data.txt").write_text("keep", encoding="utf-8")

    original_copy2 = build.shutil.copy2

    def fail_staged_copy(source, destination, *args, **kwargs):
        if Path(source).resolve().is_relative_to(staged.resolve()):
            raise OSError("simulated runtime copy failure")
        return original_copy2(source, destination, *args, **kwargs)

    monkeypatch.setattr(build.shutil, "copy2", fail_staged_copy)
    with pytest.raises(OSError, match="simulated runtime copy failure"):
        build.publish_bundle(staged, target)

    assert (target / "_internal" / "PySide6" / "stale.dll").read_text(encoding="utf-8") == "old"
    assert not (target / "_internal" / "PySide6" / "fresh.dll").exists()
    assert (target / "output" / "user-data.txt").read_text(encoding="utf-8") == "keep"


def test_publish_bundle_restores_internal_runtime_when_replacement_delete_fails(monkeypatch, tmp_path):
    build = _build_module()
    staged = tmp_path / "staged"
    target = tmp_path / "dist" / "GalgameNewsToolbox"
    (staged / "_internal" / "PySide6").mkdir(parents=True)
    (target / "_internal" / "PySide6").mkdir(parents=True)
    (target / "output").mkdir(parents=True)

    (staged / "_internal" / "PySide6" / "fresh.dll").write_text("new", encoding="utf-8")
    stale = target / "_internal" / "PySide6" / "stale.dll"
    stale.write_text("old", encoding="utf-8")
    (target / "output" / "user-data.txt").write_text("keep", encoding="utf-8")

    original_rmtree = build.shutil.rmtree
    failed_once = False

    def fail_once_after_partial_delete(path, *args, **kwargs):
        nonlocal failed_once
        if Path(path).resolve() == (target / "_internal").resolve() and not failed_once:
            failed_once = True
            stale.unlink()
            raise OSError("simulated runtime replacement delete failure")
        return original_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(build.shutil, "rmtree", fail_once_after_partial_delete)
    with pytest.raises(OSError, match="simulated runtime replacement delete failure"):
        build.publish_bundle(staged, target)

    assert stale.read_text(encoding="utf-8") == "old"
    assert not (target / "_internal" / "PySide6" / "fresh.dll").exists()
    assert (target / "output" / "user-data.txt").read_text(encoding="utf-8") == "keep"
