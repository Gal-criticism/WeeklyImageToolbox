"""Create and verify the official Windows x64 v1.1.0 release ZIP."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
VERSION = "1.1.0"
BUNDLE = "WeeklyImageToolbox"
ARCHIVE = f"{BUNDLE}-v{VERSION}-windows-x64.zip"
FFMPEG_VERSION = "8.1.2"
FFMPEG_URL = f"https://www.gyan.dev/ffmpeg/builds/packages/ffmpeg-{FFMPEG_VERSION}-essentials_build.zip"
FFMPEG_SHA256 = "db580001caa24ac104c8cb856cd113a87b0a443f7bdf47d8c12b1d740584a2ec"
FFMPEG_LICENSE_URL = "https://www.gyan.dev/ffmpeg/builds/"
PRIVATE_PATTERNS = {
    "socialdata-private-key": re.compile(rb"\d{4,10}\|[A-Za-z0-9]{35,}"),
    "github-token": re.compile(rb"gh[pousr]_[A-Za-z0-9]{20,}"),
    "secret-key-prefix": re.compile(rb"\bsk-(?:proj-)?[A-Za-z0-9]{32,}\b"),
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _download_ffmpeg(destination: Path) -> Path:
    archive = destination / "ffmpeg.zip"
    request = urllib.request.Request(FFMPEG_URL, headers={"User-Agent": "WeeklyImageToolbox-release/1.1.0"})
    with urllib.request.urlopen(request, timeout=120) as response, archive.open("wb") as output:
        shutil.copyfileobj(response, output)
    actual = sha256(archive)
    if actual.lower() != FFMPEG_SHA256:
        raise RuntimeError(f"FFmpeg SHA-256 mismatch: expected {FFMPEG_SHA256}, got {actual}")
    extracted = destination / "ffmpeg-extracted"
    extracted.mkdir()
    with zipfile.ZipFile(archive) as package:
        for member in package.infolist():
            member_path = PurePosixPath(member.filename)
            if member_path.is_absolute() or ".." in member_path.parts:
                raise RuntimeError("FFmpeg archive contains an unsafe path")
        package.extractall(extracted)
    binaries = list(extracted.rglob("ffmpeg.exe"))
    if len(binaries) != 1 or not (binaries[0].parent / "ffprobe.exe").is_file():
        raise RuntimeError("FFmpeg archive did not contain the expected ffmpeg.exe and ffprobe.exe")
    return binaries[0].parent


def _run(command: list[str], *, cwd: Path = ROOT, env: dict[str, str] | None = None) -> None:
    subprocess.run(command, cwd=cwd, env=env, check=True)


def _collect_files(bundle: Path, output: Path) -> list[Path]:
    """Copy only declared release paths; reject unexpected top-level content."""
    if not (bundle / f"{BUNDLE}.exe").is_file() or not (bundle / "_internal").is_dir():
        raise RuntimeError("PyInstaller onedir output is incomplete")
    release = output / BUNDLE
    release.mkdir(parents=True)
    shutil.copy2(bundle / f"{BUNDLE}.exe", release / f"{BUNDLE}.exe")
    shutil.copytree(bundle / "_internal", release / "_internal")
    shutil.copytree(bundle / "browser", release / "browser")
    shutil.copytree(bundle / "bin", release / "bin")
    shutil.copy2(ROOT / "packaging" / "THIRD_PARTY_NOTICES.md", release / "THIRD_PARTY_NOTICES.md")
    (release / "README.txt").write_text(
        "Weekly Image Toolbox v1.1.0 (Windows x64)\n\n"
        "Extract the complete folder, then run WeeklyImageToolbox.exe. Keep the browser, bin, and _internal folders beside the executable.\n"
        "The package includes Playwright Chromium and FFmpeg/ffprobe for offline browser and video support.\n"
        "See THIRD_PARTY_NOTICES.md and licenses/ for third-party licenses and notices.\n"
        "SHA256SUMS.json records the source revision, pinned build dependencies, and SHA-256 hashes for packaged files.\n",
        encoding="utf-8",
    )
    return sorted(path for path in release.rglob("*") if path.is_file())


def scan_release(root: Path) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        relative = path.relative_to(root).as_posix()
        if path.name == ".private-socialdata-key":
            findings.append({"path": relative, "rule": "private-key-file"})
            continue
        overlap = b""
        try:
            with path.open("rb") as stream:
                while block := stream.read(1024 * 1024):
                    data = overlap + block
                    for rule, pattern in PRIVATE_PATTERNS.items():
                        if pattern.search(data):
                            findings.append({"path": relative, "rule": rule})
                    overlap = data[-128:]
        except OSError:
            findings.append({"path": path.relative_to(root).as_posix(), "rule": "unreadable-file"})
    return findings


def _collect_python_licenses(root: Path) -> None:
    lock = ROOT / "packaging" / "requirements-release.lock"
    requested = {line.split("==", 1)[0].lower().replace("_", "-") for line in lock.read_text(encoding="utf-8").splitlines() if "==" in line and not line.lstrip().startswith("#")}
    destination = root / "licenses" / "python"
    destination.mkdir(parents=True, exist_ok=True)
    for distribution in importlib.metadata.distributions():
        name = (distribution.metadata.get("Name") or "").lower().replace("_", "-")
        if name not in requested:
            continue
        for relative in distribution.files or ():
            filename = Path(str(relative)).name
            if not re.search(r"(license|copying|notice)(\.txt|\.md)?$", filename, re.I):
                continue
            source = Path(distribution.locate_file(relative))
            if source.is_file():
                target = destination / (name + "-" + filename)
                shutil.copy2(source, target)


def build_release(output_dir: Path) -> Path:
    import tomllib
    with (ROOT / "pyproject.toml").open("rb") as project_file:
        project_version = tomllib.load(project_file)["project"]["version"]
    if project_version != VERSION:
        raise RuntimeError(f"release script is pinned to {VERSION}, project version is {project_version}")
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="weekly-image-release-") as temp_name:
        temp = Path(temp_name)
        dist, work = temp / "dist", temp / "build"
        _run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--distpath", str(dist), "--workpath", str(work), str(ROOT / "packaging" / "GalgameNewsToolbox.spec")])
        built = dist / BUNDLE
        if not built.is_dir():
            raise RuntimeError("PyInstaller did not produce the expected onedir folder")
        ffmpeg_bin = _download_ffmpeg(temp)
        (built / "bin").mkdir(exist_ok=True)
        shutil.copy2(ffmpeg_bin / "ffmpeg.exe", built / "bin" / "ffmpeg.exe")
        shutil.copy2(ffmpeg_bin / "ffprobe.exe", built / "bin" / "ffprobe.exe")
        license_dir = built / "licenses"
        license_dir.mkdir(exist_ok=True)
        with zipfile.ZipFile(temp / "ffmpeg.zip") as package:
            readmes = [item for item in package.namelist() if PurePosixPath(item).name.lower() in {"readme.txt", "readme.md", "license", "license.txt", "copying", "copying.txt"}]
            if not readmes:
                raise RuntimeError("FFmpeg package is missing its upstream license/build notices")
            for name in readmes:
                (license_dir / ("FFmpeg-" + PurePosixPath(name).name)).write_bytes(package.read(name))
        (license_dir / "FFmpeg-Gyan-Licensing.txt").write_text(
            "Gyan.dev FFmpeg Windows Essentials build, version 8.1.2.\n"
            "Package SHA-256: " + FFMPEG_SHA256 + "\nSource: " + FFMPEG_URL + "\n"
            "The upstream build README is included beside this file. This Essentials build is GPLv3.\n"
            "Build information: " + FFMPEG_LICENSE_URL + "\n",
            encoding="utf-8",
        )
        browser_path = built / "browser"
        env = os.environ.copy()
        env["PLAYWRIGHT_BROWSERS_PATH"] = str(browser_path)
        _run([sys.executable, "-m", "playwright", "install", "chromium"], env=env)
        assembled = _collect_files(built, temp / "assembled")
        license_dst = temp / "assembled" / BUNDLE / "licenses"
        shutil.copytree(license_dir, license_dst)
        root = temp / "assembled" / BUNDLE
        _collect_python_licenses(root)
        findings = scan_release(root)
        if findings:
            # Rule names and paths only; never print matched bytes.
            raise RuntimeError("release scan failed: " + json.dumps(findings, ensure_ascii=False))
        source_revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, check=True, capture_output=True, text=True).stdout.strip()
        lock_entries = [line.strip() for line in (ROOT / "packaging" / "requirements-release.lock").read_text(encoding="utf-8").splitlines() if line.strip() and not line.lstrip().startswith("#")]
        manifest = {
            "product": BUNDLE,
            "version": VERSION,
            "platform": "windows-x64",
            "source_revision": source_revision,
            "dependencies": lock_entries,
            "files": [{"path": p.relative_to(root).as_posix(), "sha256": sha256(p)} for p in sorted(root.rglob("*")) if p.is_file()],
        }
        (root / "SHA256SUMS.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        smoke_env = os.environ.copy()
        smoke_env["LOCALAPPDATA"] = str(temp / "isolated-app-data")
        smoke_env["APPDATA"] = str(temp / "isolated-roaming-data")
        smoke_env["QT_QPA_PLATFORM"] = "offscreen"
        subprocess.run([str(root / f"{BUNDLE}.exe"), "--self-check"], cwd=root, env=smoke_env, check=True, timeout=180)
        report_path = root / "package_self_check.json"
        if not report_path.is_file():
            raise RuntimeError("frozen executable self-check did not write its report")
        report = json.loads(report_path.read_text(encoding="utf-8"))
        if not report or not all(value is True for value in report.values()):
            raise RuntimeError("frozen executable self-check failed: " + json.dumps({key: value is True for key, value in report.items()}))
        report_path.unlink()
        archive_path = output_dir / ARCHIVE
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for path in sorted(root.rglob("*")):
                if path.is_file():
                    archive.write(path, (PurePosixPath(BUNDLE) / path.relative_to(root).as_posix()).as_posix())
    verify_archive(archive_path)
    (output_dir / (ARCHIVE + ".sha256")).write_text(f"{sha256(archive_path)}  {ARCHIVE}\n", encoding="ascii")
    return archive_path


def verify_archive(path: Path) -> None:
    with zipfile.ZipFile(path) as archive:
        bad = archive.testzip()
        if bad:
            raise RuntimeError(f"ZIP CRC validation failed: {bad}")
        names = archive.namelist()
        expected_root = BUNDLE + "/"
        if not any(name.startswith(expected_root) for name in names):
            raise RuntimeError("ZIP does not contain the expected bundle root")
        for required in (f"{expected_root}{BUNDLE}.exe", f"{expected_root}bin/ffmpeg.exe", f"{expected_root}bin/ffprobe.exe", f"{expected_root}THIRD_PARTY_NOTICES.md", f"{expected_root}SHA256SUMS.json"):
            if required not in names:
                raise RuntimeError(f"ZIP is missing required file: {required}")
        manifest = json.loads(archive.read(f"{expected_root}SHA256SUMS.json"))
        for item in manifest["files"]:
            member = expected_root + item["path"]
            if member not in names or hashlib.sha256(archive.read(member)).hexdigest() != item["sha256"]:
                raise RuntimeError(f"ZIP manifest validation failed: {item['path']}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", "--output-dir", dest="output_dir", type=Path, default=ROOT / "release-output")
    parser.add_argument("--verify", type=Path, help="verify an already-created release ZIP")
    args = parser.parse_args()
    if args.verify:
        verify_archive(args.verify)
        return 0
    print(build_release(args.output_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
