from __future__ import annotations

import asyncio

from galgame_news.domain import Issue, NewsItem
from galgame_news.pipeline import PipelineRunner, TaskRequest


def _runner_fixture(tmp_path):
    source = tmp_path / "source.docx"
    source.write_bytes(b"fixture")

    class Parser:
        def parse(self, *_):
            return None

    class Analyzer:
        def analyze(self, _):
            return Issue(issue_id="issue-1", input_path=str(source), news_items=[
                NewsItem(issue_id="issue-1", sequence=1, section="新作",
                         title="Game title", body="Update", game_names=["Game title"])
            ])

    return source, PipelineRunner(parser=Parser(), analyzer=Analyzer())


def test_pipeline_reconciles_at_start_and_end_with_resolved_task_root(tmp_path):
    source, runner = _runner_fixture(tmp_path)
    calls = []

    async def record(root, phase, *, cancelled=None):
        calls.append((root, phase, callable(cancelled)))
        return {"complete": True}

    runner._reconcile_task_media = record
    result = asyncio.run(runner.run_async(TaskRequest(
        input_path=source, issue_id="issue-1", output_dir=tmp_path / "tasks", no_videos=True,
    )))

    assert result.status == "completed"
    assert calls == [(result.task_dir, "start", True),
                     (result.task_dir, "end", True)]


def test_cancelled_pipeline_still_runs_terminal_inventory_hook(tmp_path):
    source, runner = _runner_fixture(tmp_path)
    calls = []

    async def record(root, phase, *, cancelled=None):
        calls.append((root, phase))
        return {"complete": True}

    runner._reconcile_task_media = record

    class CancelImmediately:
        is_cancelled = True
        is_paused = False

        async def wait_if_paused_async(self):
            from galgame_news.pipeline import CancellationRequested
            raise CancellationRequested("cancelled for test")

    result = asyncio.run(runner.run_async(
        TaskRequest(input_path=source, issue_id="issue-1", output_dir=tmp_path / "tasks", no_videos=True),
        cancellation_token=CancelImmediately(),
    ))

    assert result.status == "cancelled"
    assert calls[0] == (result.task_dir, "start")
    assert calls[-1] == (result.task_dir, "end")


def test_review_inventory_summary_accepts_change_lists(qtbot):
    from galgame_news.desktop.review_page import ReviewPage

    page = ReviewPage()
    qtbot.addWidget(page)
    page.show_inventory_report({
        "complete": True, "last_checked_utc": "2026-10-29T01:02:03+00:00",
        "files": 7, "physical_unique": 6, "usable_unique": 5,
        "changes": {"added": ["a"], "removed": [], "moved": ["b"], "modified": ["c"]},
    })

    text = page.export_status.text()
    assert "扫描 7 个文件" in text
    assert "物理唯一 6 个" in text
    assert "可用 5 个" in text
    assert "变化 3 项" in text
    assert "2026-10-29T01:02:03+00:00" in text
