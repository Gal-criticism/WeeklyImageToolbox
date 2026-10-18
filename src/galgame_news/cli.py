from __future__ import annotations

import argparse
import json
from pathlib import Path

from .application import Application

DEFAULT_OUTPUT_ROOT = Path(r"E:\project\galgame news\output")

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="galgame_news")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="process one DOCX issue")
    run.add_argument("input", type=Path)
    run.add_argument("--issue", required=True)
    run.add_argument("--output", type=Path)
    run.add_argument("--offline", action="store_true")
    run.add_argument("--no-videos", action="store_true", help="disable video discovery and downloading")
    run.add_argument("--config", type=Path)
    run.add_argument("--history-db", type=Path)
    run.add_argument("--llm-provider")
    run.add_argument("--llm-model")
    run.add_argument("--max-images", type=int)
    migrate = sub.add_parser("migrate-storage", help="migrate one legacy task's storage to SQLite")
    migrate.add_argument("taskdir", type=Path)
    check = sub.add_parser("check-storage", help="check one task's SQLite storage")
    check.add_argument("taskdir", type=Path)
    backup = sub.add_parser("backup-storage", help="back up a task and its files")
    backup.add_argument("taskdir", type=Path)
    backup.add_argument("destination", type=Path)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "run":
        from .pipeline import CancellationToken, PipelineRunner, TaskRequest
        app = Application(offline=args.offline, no_videos=args.no_videos, config_path=args.config,
                          history_db=args.history_db, max_images=args.max_images,
                          llm_provider=args.llm_provider, llm_model=args.llm_model)
        runner = PipelineRunner(parser=app.parser, analyzer=app.analyzer, resolver=app.resolver,
                                history=app.history, config=app.config,
                                source_transport=app.source_transport, image_transport=app.image_transport,
                                video_downloader_factory=app.video_downloader_factory,
                                video_ffmpeg_detector=app.video_ffmpeg_detector)
        request = TaskRequest(input_path=args.input, issue_id=args.issue,
                              output_dir=args.output or DEFAULT_OUTPUT_ROOT,
                              offline=args.offline, no_videos=args.no_videos,
                              config_path=args.config, history_db=args.history_db,
                              max_images=args.max_images, llm_provider=args.llm_provider,
                              llm_model=args.llm_model)
        result = runner.run(request, cancellation_token=CancellationToken())
        print(f"[galgame_news] {result.status}: {result.task_dir}")
        return 0 if result.status == "completed" else 1
    elif args.command == "migrate-storage":
        from .tasks.migration import migrate_task
        report = migrate_task(args.taskdir)
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        return 0 if report.get("migrated") else 1
    elif args.command == "check-storage":
        from .tasks.repository import TaskRepository
        repo = TaskRepository(args.taskdir, create=False)
        report = {"task_root": str(repo.root), "authoritative": TaskRepository.is_authoritative(repo.root), **repo.integrity_check()}
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        return 0 if report.get("ok") else 1
    elif args.command == "backup-storage":
        from .tasks.repository import TaskRepository
        repo = TaskRepository(args.taskdir, create=False)
        destination = repo.backup(args.destination)
        print(json.dumps({"task_root": str(repo.root), "backup": str(destination)}, ensure_ascii=False, indent=2))
    return 0
