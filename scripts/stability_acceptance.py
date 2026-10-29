"""Run bounded live acceptance without modifying an existing user task."""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict
from datetime import datetime
import hashlib
import json
from multiprocessing import freeze_support
import os
from pathlib import Path
import sys
import time


ROOT = Path(__file__).resolve().parents[1]
ACCEPTANCE = ROOT / "output" / "\u7a33\u5b9a\u6027\u9a8c\u6536-20261029"


def write_json(path, payload):
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def verify_images(candidates, task_root):
    from PIL import Image

    errors = []
    checked = 0

    def verify_file(path, expected_sha256, label):
        path = Path(path or "")
        if not path.is_absolute():
            path = task_root / path
        try:
            with Image.open(path) as image:
                image.verify()
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if expected_sha256 and digest != expected_sha256:
                raise ValueError(f"{label} sha256 mismatch")
            return True, None, path
        except Exception as exc:
            return False, {
                "candidate_id": candidate.id,
                "error": type(exc).__name__,
                "label": label,
                "path": str(path),
            }, path

    for candidate in candidates:
        if candidate.download_status != "downloaded":
            continue
        valid, error, _ = verify_file(
            candidate.local_path,
            candidate.output_sha256 or candidate.sha256,
            "preview",
        )
        if not valid:
            errors.append(error)
        if candidate.original_path:
            original_valid, original_error, _ = verify_file(
                candidate.original_path,
                candidate.original_sha256,
                "original",
            )
            if not original_valid:
                errors.append(original_error)
                valid = False
        if valid:
            checked += 1
    return {"checked": checked, "errors": errors}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--issue", required=True, choices=("255", "259", "261"))
    parser.add_argument("--resume-task", type=Path)
    parser.add_argument("--supplement", action="store_true")
    parser.add_argument("--offline", action="store_true")
    options = parser.parse_args()
    sys.path.insert(0, str(ROOT / "src"))
    from galgame_news.application import Application
    from galgame_news.pipeline import PipelineRunner, TaskRequest

    for name in ("\u89e3\u6790\u5361\u987f\u4fee\u590d\u7248-20261028-v2",
                 "\u89e3\u6790\u5361\u987f\u4fee\u590d\u7248-20261028"):
        bundle = ROOT / "dist" / name / "WeeklyImageToolbox"
        if (bundle / "bin" / "ffmpeg.exe").is_file():
            os.environ.setdefault("FFMPEG_LOCATION", str(bundle / "bin"))
        if (bundle / "browser").is_dir():
            os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(bundle / "browser"))
    ACCEPTANCE.mkdir(parents=True, exist_ok=True)
    if options.resume_task:
        task = options.resume_task.resolve()
        if not task.is_relative_to(ACCEPTANCE.resolve()):
            parser.error("resume is restricted to this acceptance directory")
    run_root = ACCEPTANCE / f"issue-{options.issue}"
    run_root.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    label = f"{stamp}-{'resume' if options.resume_task else 'new'}"
    events_path = run_root / f"{label}-events.jsonl"
    started = time.monotonic()
    events = []
    result = None
    with events_path.open("x", encoding="utf-8") as log:
        def progress(event):
            row = asdict(event)
            row["elapsed_seconds"] = round(time.monotonic() - started, 3)
            log.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
            log.flush()
            events.append(row)
            if (event.kind in {"news_started", "news_completed", "news_failed", "output_started",
                               "output_completed", "task_completed", "task_failed"}
                    or event.kind.endswith("failed")):
                print(f"[{row['elapsed_seconds']:7.1f}s] {options.issue} "
                      f"{event.news_index or '-'}/{event.total_news or '-'} "
                      f"{event.kind}: {event.message}", flush=True)

        app = Application(offline=options.offline)
        runner = PipelineRunner(parser=app.parser, analyzer=app.analyzer,
                                resolver=app.resolver, config=app.config)
        request = TaskRequest(
            input_path=ROOT / "input" / f"{options.issue}.docx", issue_id=options.issue,
            output_dir=run_root, offline=options.offline, no_videos=False,
            use_archive_materials=True, use_image_supplement=options.supplement,
            use_socialdata_x=False, resume=options.resume_task is not None,
            task_dir=options.resume_task,
        )
        try:
            result = runner.run(request, progress)
        except BaseException as exc:
            write_json(run_root / f"{label}-crash.json", {
                "exception_type": type(exc).__name__, "elapsed_seconds": time.monotonic() - started,
                "events": str(events_path), "note": "Exception text omitted to avoid leaking credentials.",
            })
            raise
    pipeline = result.pipeline_result
    rows = []
    for news in pipeline.issue.news_items:
        candidates = [c for c in pipeline.all_candidates if c.news_id == news.id]
        videos = [v for v in pipeline.videos if v.news_id == news.id]
        failures = [f for f in pipeline.failures if f.news_id == news.id]
        rows.append({
            "sequence": news.sequence, "news_id": news.id, "title": news.title,
            "game_names": news.game_names, "candidates": len(candidates),
            "downloaded_images": sum(c.download_status == "downloaded" for c in candidates),
            "selected_images": sum(c.selected for c in candidates),
            "video_candidates": len(videos),
            "downloaded_videos": sum(v.status == "downloaded" for v in videos),
            "failure_codes": dict(Counter(f.code for f in failures)),
        })
    from galgame_news.pipeline.checkpoint import load_checkpoint
    checkpoint = load_checkpoint(result.checkpoint_path) if result.checkpoint_path else None
    # Actual file checks are separate from collection status and relevance.
    verification = verify_images(pipeline.all_candidates, result.task_dir)
    summary = {
        "status": result.status, "elapsed_seconds": round(time.monotonic() - started, 2),
        "task_dir": str(result.task_dir), "events": str(events_path),
        "resumed": options.resume_task is not None, "supplement_enabled": options.supplement,
        "archive_enabled": True, "paid_x_enabled": False,
        "news_count": len(rows),
        "checkpoint_completed_count": len(checkpoint.completed_news_ids) if checkpoint else None,
        "news_started": sum(e["kind"] == "news_started" for e in events),
        "news_completed": sum(e["kind"] == "news_completed" for e in events),
        "news_failed": sum(e["kind"] == "news_failed" for e in events),
        "downloaded_images": sum(row["downloaded_images"] for row in rows),
        "downloaded_videos": sum(row["downloaded_videos"] for row in rows),
        "failure_codes": dict(Counter(f.code for f in pipeline.failures)),
        "image_file_verification": verification, "news": rows,
        "boundary": "File integrity and task termination do not prove visual relevance.",
    }
    write_json(run_root / f"{label}-summary.json", summary)
    write_json(run_root / "latest.json", summary)
    print(json.dumps({k: v for k, v in summary.items() if k != "news"}, ensure_ascii=True), flush=True)
    return 0 if result.status == "completed" and not verification["errors"] else 1


if __name__ == "__main__":
    freeze_support()
    raise SystemExit(main())
