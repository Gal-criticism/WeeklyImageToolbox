"""Exercise 118-to-322 growth on a NEW COPY of the saved 261 task."""
import argparse
import json
from pathlib import Path
import shutil
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from PIL import Image
from galgame_news.media_sync import reconcile_task
from galgame_news.tasks.repository import TaskRepository


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    if args.destination.exists() or args.destination.resolve().is_relative_to(args.source.resolve()):
        parser.error("destination must be a new separate directory")
    shutil.copytree(args.source, args.destination)
    root = args.destination
    first = reconcile_task(root, "acceptance_before", timeout=120)
    repo = TaskRepository(root, create=False)
    image_payload = repo.export_review_payloads()[0]
    news = image_payload["news_items"][0]["news_id"]
    manual = root / "assets" / "originals" / news
    manual.mkdir(parents=True, exist_ok=True)
    for i in range(204):
        Image.new("RGB", (640, 480), (i, 17, 200)).save(manual / f"manual-{i+1}.png")
    second = reconcile_task(root, "acceptance_after_add", timeout=120)
    again = reconcile_task(root, "acceptance_repeat", timeout=120)
    rows = list(repo.iter_candidates(news_id=news))
    manual_rows = [r for r in rows if (r.get("signals") or {}).get("media_sync")]
    report = {"source_preserved": str(args.source.resolve()),
              "copy": str(root.resolve()), "before": first, "after_add": second,
              "repeat": again, "manual_candidates": len(manual_rows)}
    (root.parent / "growth-verification.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    assert first["complete"] and second["complete"] and again["complete"]
    assert first["by_role"]["originals"] == 118
    assert second["by_role"]["originals"] == 322
    assert second["imported"] == 204
    assert again["imported"] == 0
    assert len(manual_rows) == 204
    assert all(not r["selected"] for r in manual_rows)
    print("PASS: 118 -> 322 originals; 204 pending imports; repeat imports 0")


if __name__ == "__main__":
    main()
