from __future__ import annotations

import hashlib
import importlib.util
import json
import zipfile
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]


def _release_module():
    spec = importlib.util.spec_from_file_location("weekly_release", ROOT / "packaging" / "release.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_release_metadata_is_explicit_and_dependency_versions_are_locked():
    release = _release_module()
    assert release.VERSION == "1.0.0"
    assert release.ARCHIVE == "WeeklyImageToolbox-v1.0.0-windows-x64.zip"
    assert len(release.FFMPEG_SHA256) == 64
    requirements = (ROOT / "packaging" / "requirements-release.lock").read_text(encoding="utf-8")
    for package in ("PyInstaller", "PySide6", "playwright", "pytest", "httpx", "pydantic"):
        assert any(line.lower().startswith(package.lower() + "==") for line in requirements.splitlines())
    assert not any(line.strip() and not line.lstrip().startswith("#") and "==" not in line for line in requirements.splitlines())


def test_scan_release_reports_only_paths_and_rule_names(tmp_path):
    release = _release_module()
    secret = "FAKE_PRIVATE_TEST_SEED_1234567890"
    target = tmp_path / "nested" / "artifact.bin"
    target.parent.mkdir()
    target.write_text(secret, encoding="utf-8")
    findings = release.scan_release(tmp_path)
    assert findings == []  # the self-check field name and seed placeholder are not credentials
    target.write_bytes(b"Bearer " + b"12345|" + b"A" * 41)
    findings = release.scan_release(tmp_path)
    assert findings == [{"path": "nested/artifact.bin", "rule": "socialdata-private-key"}]
    assert secret not in json.dumps(findings)


def test_verify_archive_checks_required_files_and_manifest(tmp_path):
    release = _release_module()
    zip_path = tmp_path / release.ARCHIVE
    root = "WeeklyImageToolbox/"
    contents = {
        root + "WeeklyImageToolbox.exe": b"exe",
        root + "bin/ffmpeg.exe": b"ffmpeg",
        root + "bin/ffprobe.exe": b"ffprobe",
        root + "THIRD_PARTY_NOTICES.md": b"notices",
    }
    manifest = {"files": [{"path": p.removeprefix(root), "sha256": hashlib.sha256(data).hexdigest()} for p, data in contents.items()]}
    contents[root + "SHA256SUMS.json"] = json.dumps(manifest).encode()
    with zipfile.ZipFile(zip_path, "w") as archive:
        for name, data in contents.items():
            archive.writestr(name, data)
    release.verify_archive(zip_path)

    contents[root + "bin/ffmpeg.exe"] = b"tampered"
    with zipfile.ZipFile(zip_path, "w") as archive:
        for name, data in contents.items():
            archive.writestr(name, data)
    with pytest.raises(RuntimeError, match="manifest validation"):
        release.verify_archive(zip_path)


def test_workflow_gates_on_version_runs_tests_and_publishes_latest_after_verification():
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert "branches: [main]" in workflow
    assert "workflow_dispatch" in workflow
    assert "project']['version']" in workflow
    assert "python -m pytest" in workflow
    assert "--draft" in workflow
    assert "--verify" in workflow
    assert "--draft=false --latest" in workflow
    assert "OPENAI_API_KEY" not in workflow
    assert "QT_QPA_PLATFORM: offscreen" in workflow
    assert "--target $env:GITHUB_SHA" in workflow
