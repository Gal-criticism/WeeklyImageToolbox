"""Read-only before/after inventory evidence for an existing local task."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from galgame_news.inventory import scan_inventory, save_manifest
from galgame_news.media_sync import task_media_roots


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task", type=Path)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()
    if args.evidence.resolve().is_relative_to(args.task.resolve()):
        parser.error("evidence must be outside the source task")
    args.evidence.mkdir(parents=True, exist_ok=True)
    roots = task_media_roots(args.task)
    if not roots:
        parser.error("no known media directories found")
    before = scan_inventory(roots, timeout=120)
    after = scan_inventory(roots, previous=before.manifest, timeout=120)
    report = {"source": str(args.task.resolve()), "read_only_source": True,
              "before": asdict(before), "after": asdict(after)}
    for name, result in (("before", before), ("after", after)):
        if result.complete:
            save_manifest(args.evidence / f"{name}.json", result.manifest, complete=True)
    (args.evidence / "verification.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"before": asdict(before.stats), "after": asdict(after.stats),
                      "complete": before.complete and after.complete}, ensure_ascii=True))
    return 0 if before.complete and after.complete else 1


if __name__ == "__main__":
    raise SystemExit(main())
