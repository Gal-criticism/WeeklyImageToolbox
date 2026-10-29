"""Live issue 255 regression; preserves existing tasks and records all events."""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict
from datetime import datetime
import argparse
import json
from multiprocessing import freeze_support
from pathlib import Path
import sys
import time
import os


def main():
    arguments = argparse.ArgumentParser()
    arguments.add_argument("--resume-task", type=Path)
    options = arguments.parse_args()
    from galgame_news.application import Application
    from galgame_news.pipeline import PipelineRunner, TaskRequest

    root = Path(__file__).resolve().parents[1]
    bundled_runtime = root / "dist/解析卡顿修复版-20261028/WeeklyImageToolbox"
    if (bundled_runtime / "bin/ffmpeg.exe").is_file():
        os.environ.setdefault("FFMPEG_LOCATION", str(bundled_runtime / "bin"))
    if (bundled_runtime / "browser").is_dir():
        os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(bundled_runtime / "browser"))
    output = root / "output" / "解析卡顿修复-255-实网-20261028"
    output.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%H%M%S")
    events_path = output / f"events-{stamp}.jsonl"
    started = time.monotonic()
    events = []
    with events_path.open("x", encoding="utf-8") as log:
        def progress(event):
            data = asdict(event)
            data["elapsed_seconds"] = round(time.monotonic() - started, 2)
            log.write(json.dumps(data, ensure_ascii=False, default=str) + "\n")
            log.flush()
            events.append(data)
            if (event.kind in {"news_started", "news_completed", "news_failed", "resolve_progress"}
                    or event.kind.endswith("failed")):
                print(f"[{data['elapsed_seconds']:7.1f}s] {event.news_index or '-'}/{event.total_news or '-'} "
                      f"{event.kind}: {event.message}", flush=True)

        app = Application()
        runner = PipelineRunner(parser=app.parser, analyzer=app.analyzer,
                                resolver=app.resolver, config=app.config)
        request = TaskRequest(input_path=root / "input" / "255.docx", issue_id="255",
                              output_dir=output, no_videos=False,
                              resume=options.resume_task is not None,
                              task_dir=options.resume_task)
        result = runner.run(request, progress)
    pipeline = result.pipeline_result
    all_candidates = pipeline.all_candidates
    rows = []
    for news in pipeline.issue.news_items:
        candidates = [c for c in all_candidates if c.news_id == news.id]
        videos = [v for v in pipeline.videos if v.news_id == news.id]
        failures = [f for f in pipeline.failures if f.news_id == news.id]
        rows.append({"sequence": news.sequence, "news_id": news.id, "title": news.title,
                     "candidates": len(candidates), "downloaded_images": sum(c.download_status == "downloaded" for c in candidates),
                     "selected_images": sum(c.selected for c in candidates),
                     "video_candidates": len(videos),
                     "downloaded_videos": sum(v.status == "downloaded" for v in videos),
                     "failure_codes": dict(Counter(f.code for f in failures)),
                     "timeouts": [f.model_dump(mode="json") for f in failures if "timeout" in f.code]})
    summary = {"status": result.status, "elapsed_seconds": round(time.monotonic() - started, 2),
               "task_dir": str(result.task_dir), "checkpoint": str(result.checkpoint_path),
               "events": str(events_path), "news_count": len(rows),
               "news_started": sum(e["kind"] == "news_started" for e in events),
               "news_completed": sum(e["kind"] == "news_completed" for e in events),
               "news_failed": sum(e["kind"] == "news_failed" for e in events),
               "downloaded_images": sum(r["downloaded_images"] for r in rows),
               "video_candidates": len(pipeline.videos),
               "downloaded_videos": sum(r["downloaded_videos"] for r in rows),
               "resumed": options.resume_task is not None,
               "failure_codes": dict(Counter(f.code for f in pipeline.failures)),
               "news": rows}
    summary_path = output / f"summary-{stamp}.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# 第255期实网回归", "", f"状态：{result.status}；耗时：{summary['elapsed_seconds']} 秒。",
             f"新闻 {len(rows)} 条，完成 {summary['news_completed']}，超时跳过 {summary['news_failed']}。",
             f"下载图片 {summary['downloaded_images']} 张；视频候选 {len(pipeline.videos)} 条。", "",
             f"任务目录：{result.task_dir}", f"事件记录：{events_path}", "",
             "|序号|新闻|已下载图片|候选图片|失败码|", "|---|---|---:|---:|---|"]
    lines.extend(f"|{r['sequence']}|{r['title']}|{r['downloaded_images']}|{r['candidates']}|{r['failure_codes']}|" for r in rows)
    (output / f"report-{stamp}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in summary.items() if k != "news"}, ensure_ascii=False, indent=2), flush=True)
    return 0 if result.status == "completed" and len(rows) == 23 else 1


if __name__ == "__main__":
    freeze_support()
    sys.exit(main())
