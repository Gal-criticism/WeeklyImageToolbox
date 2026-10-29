"""Check the timeout-fix portable executable against its current Python sources."""
from pathlib import Path
import hashlib
import json
import marshal
import types

from PyInstaller.archive.readers import CArchiveReader


def fingerprint(code):
    if isinstance(code, types.CodeType):
        return (code.co_code, code.co_names, code.co_varnames,
                tuple(fingerprint(item) for item in code.co_consts))
    return code


root = Path(__file__).resolve().parents[1]
executable = root / "dist/解析卡顿修复版-20261028-v2/WeeklyImageToolbox/WeeklyImageToolbox.exe"
archive = CArchiveReader(str(executable))
pyz = archive.open_embedded_archive(next(name for name in archive.toc if name.endswith(".pyz")))
modules = [
    "concurrency", "config", "curation.download", "delivery.history",
    "discovery.async_http", "discovery.async_resolver", "discovery.resolver", "discovery.search", "discovery.search_worker",
    "pipeline.network_runtime", "pipeline.runner", "pipeline.goods_collection", "desktop.progress_page",
]
checks = {}
for suffix in modules:
    name = "galgame_news." + suffix
    source = root / "src" / (name.replace(".", "/") + ".py")
    checks[name] = fingerprint(pyz.extract(name)) == fingerprint(
        compile(source.read_bytes(), str(source), "exec", dont_inherit=True))
entry = root / "packaging/desktop_entry.py"
checks["desktop_entry"] = fingerprint(marshal.loads(archive.extract("desktop_entry"))) == fingerprint(
    compile(entry.read_bytes(), str(entry), "exec", dont_inherit=True))
report = {"all_match": all(checks.values()), "modules_match_current_source": checks,
          "executable": str(executable), "exe_sha256": hashlib.sha256(executable.read_bytes()).hexdigest()}
target = root / "output/解析卡顿修复-20261028/bundle-check-v2.json"
target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(report, ensure_ascii=False))
raise SystemExit(0 if report["all_match"] else 1)
