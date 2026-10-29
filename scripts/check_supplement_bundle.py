"""Verify the trial executable contains the current supplement implementation."""
from pathlib import Path
import json
import types

from PyInstaller.archive.readers import CArchiveReader


def fingerprint(code):
    if isinstance(code, types.CodeType):
        return (code.co_code, code.co_names, code.co_varnames,
                tuple(fingerprint(item) for item in code.co_consts))
    return code


root = Path(__file__).resolve().parents[1]
bundle = root / "dist/补图模块试验版-20261028/WeeklyImageToolbox"
archive = CArchiveReader(str(bundle / "WeeklyImageToolbox.exe"))
pyz = archive.open_embedded_archive(next(name for name in archive.toc if name.endswith(".pyz")))
modules = [
    "supplement.ledger", "supplement.service", "supplement.providers", "supplement.rules",
    "pipeline.contracts", "pipeline.checkpoint", "pipeline.runner", "pipeline.supplement",
    "pipeline.network_runtime", "desktop.controller", "desktop.new_task_page",
    "desktop.review_page", "desktop.worker", "review.session", "tasks.repository",
    "delivery.output", "localization.vndb", "localization.vndb_client",
]
checks = {}
for suffix in modules:
    name = "galgame_news." + suffix
    path = root / "src" / (name.replace(".", "/") + ".py")
    checks[name] = fingerprint(pyz.extract(name)) == fingerprint(
        compile(path.read_bytes(), str(path), "exec", dont_inherit=True))
report = {"current_source_matches_bundle": all(checks.values()), "modules": checks}
target = root / "output/补图模块试验-20261028/bundle-source-check.json"
target.parent.mkdir(parents=True, exist_ok=True)
target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(report, ensure_ascii=False))
raise SystemExit(0 if all(checks.values()) else 1)
