from __future__ import annotations

from galgame_news.pipeline.checkpoint import load_checkpoint

import json
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from galgame_news.domain import (
    CollectionResult,
    Issue,
    IssueDraft,
    NewsDraft,
    NewsItem,
    SourceRef,
    SourceType,
)
from galgame_news.pipeline import CancellationToken, PipelineRunner, TaskRequest


def _components(news_count: int = 2):
    class Parser:
        def parse(self, path, issue_id):
            return IssueDraft(
                issue_id=issue_id,
                input_path=str(path),
                entries=[
                    NewsDraft(sequence=i, section="新作", title=f"Game {i}", body="CG")
                    for i in range(1, news_count + 1)
                ],
            )

    class Analyzer:
        def analyze(self, draft):
            return Issue(
                issue_id=draft.issue_id,
                input_path=draft.input_path,
                news_items=[
                    NewsItem(
                        issue_id=draft.issue_id,
                        sequence=entry.sequence,
                        section=entry.section,
                        title=entry.title,
                        body=entry.body,
                    )
                    for entry in draft.entries
                ],
            )

    calls = []

    class Resolver:
        def resolve(self, news):
            calls.append(news.sequence)
            return [
                SourceRef(
                    url=f"https://cdn.example/{news.sequence}.jpg",
                    domain="cdn.example",
                    source_type=SourceType.DIRECT_IMAGE,
                )
            ]

    return Parser(), Analyzer(), Resolver(), calls


def _image_transport(url, **_):
    stream = BytesIO()
    Image.new("RGB", (800, 600), "red").save(stream, format="JPEG")
    return type(
        "Response",
        (),
        {
            "content": stream.getvalue(),
            "headers": {"content-type": "image/jpeg"},
            "status_code": 200,
            "url": url,
        },
    )()


def test_pipeline_runner_emits_structured_events_and_returns_task_result(tmp_path):
    from galgame_news.pipeline import CancellationToken, PipelineRunner, TaskRequest

    parser, analyzer, resolver, _ = _components(1)
    input_path = tmp_path / "sample.docx"
    input_path.write_bytes(b"fixture")
    events = []
    request = TaskRequest(input_path=input_path, issue_id="1", output_dir=tmp_path / "out", offline=False)
    runner = PipelineRunner(
        parser=parser,
        analyzer=analyzer,
        resolver=resolver,
        image_transport=_image_transport,
    )

    result = runner.run(request, events.append, CancellationToken())

    assert result.status == "completed"
    assert result.pipeline_result.issue.issue_id == "1"
    assert any(event.kind == "task_started" for event in events)
    assert any(event.kind == "news_completed" for event in events)
    assert result.output_dir.is_dir()


def test_pipeline_runner_cancellation_persists_checkpoint_and_resume_skips_completed_news(tmp_path):
    from galgame_news.pipeline import CancellationToken, PipelineRunner, TaskRequest

    parser, analyzer, resolver, calls = _components(2)
    input_path = tmp_path / "sample.docx"
    input_path.write_bytes(b"fixture")
    request = TaskRequest(input_path=input_path, issue_id="1", output_dir=tmp_path / "out")
    token = CancellationToken()
    runner = PipelineRunner(parser=parser, analyzer=analyzer, resolver=resolver, image_transport=_image_transport)

    def stop_after_first(event):
        if event.kind == "news_completed" and event.news_index == 1:
            token.cancel()

    cancelled = runner.run(request, stop_after_first, token)
    assert cancelled.status == "cancelled"
    assert cancelled.checkpoint_path.is_file()
    checkpoint = load_checkpoint(cancelled.checkpoint_path).model_dump(mode="json")
    assert checkpoint["completed_news_ids"]

    resumed = runner.run(
        TaskRequest(
            input_path=input_path,
            issue_id="1",
            output_dir=tmp_path / "out",
            task_dir=cancelled.task_dir,
            resume=True,
        ),
        lambda event: None,
        CancellationToken(),
    )
    assert resumed.status == "completed"
    # Cancellation is strict: the news that follows the completed checkpoint
    # is processed only once during resume.
    assert calls == [1, 2]


def test_pipeline_runner_merges_duplicate_failures_and_video_candidates(tmp_path):
    from galgame_news.domain import FailureRecord, FailureStage, VideoCandidate
    from galgame_news.pipeline import CancellationToken, PipelineRunner, TaskRequest

    parser, analyzer, _, _ = _components(1)

    class Resolver:
        def resolve(self, news):
            return [
                SourceRef(url="https://video.example/a", domain="video.example", source_type=SourceType.VIDEO),
                SourceRef(url="https://video.example/b", domain="video.example", source_type=SourceType.VIDEO),
            ]

    class Adapter:
        def collect(self, news, source, context):
            candidate = VideoCandidate(news_id=news.id, source_url=source.url, video_url="https://cdn.example/same.mp4")
            failure = FailureRecord(stage=FailureStage.COLLECT, news_id=news.id, code="same", message="same", source_url=source.url)
            return CollectionResult(video_candidates=[candidate], failures=[failure])

    input_path = tmp_path / "sample.docx"
    input_path.write_bytes(b"fixture")
    request = TaskRequest(input_path=input_path, issue_id="1", output_dir=tmp_path / "out", no_videos=True)
    runner = PipelineRunner(parser=parser, analyzer=analyzer, resolver=Resolver(), adapter_factory=lambda *_: Adapter())
    result = runner.run(request, lambda event: None, CancellationToken())

    assert len(result.pipeline_result.videos) == 1
    duplicates = [failure for failure in result.pipeline_result.failures if failure.code == "same"]
    assert len(duplicates) == 2


def test_pipeline_runner_rejects_resume_when_input_hash_changed(tmp_path):
    from galgame_news.pipeline import CancellationToken, PipelineRunner, TaskRequest

    parser, analyzer, resolver, _ = _components(1)
    input_path = tmp_path / "sample.docx"
    input_path.write_bytes(b"fixture")
    runner = PipelineRunner(parser=parser, analyzer=analyzer, resolver=resolver, image_transport=_image_transport)

    initial = runner.run(
        TaskRequest(input_path=input_path, issue_id="1", output_dir=tmp_path / "out"),
        lambda event: None,
        CancellationToken(),
    )
    input_path.write_bytes(b"changed")

    resumed = runner.run(
        TaskRequest(
            input_path=input_path,
            issue_id="1",
            output_dir=tmp_path / "out",
            task_dir=initial.task_dir,
            resume=True,
        ),
        lambda event: None,
        CancellationToken(),
    )

    assert resumed.status == "failed"
    assert any(failure.code == "checkpoint_input_mismatch" for failure in resumed.failures)


def test_pipeline_runner_rejects_resume_when_effective_config_changed(tmp_path):
    from galgame_news.config import load_config
    from galgame_news.pipeline import CancellationToken, PipelineRunner, TaskRequest

    parser, analyzer, resolver, _ = _components(1)
    input_path = tmp_path / "sample.docx"
    input_path.write_bytes(b"fixture")
    initial_runner = PipelineRunner(parser=parser, analyzer=analyzer, resolver=resolver, image_transport=_image_transport)
    initial = initial_runner.run(
        TaskRequest(input_path=input_path, issue_id="1", output_dir=tmp_path / "out"),
        lambda event: None,
        CancellationToken(),
    )
    changed = load_config()
    changed.selection.minimum_score = min(100.0, changed.selection.minimum_score + 1.0)
    resumed = PipelineRunner(parser=parser, analyzer=analyzer, resolver=resolver, config=changed).run(
        TaskRequest(
            input_path=input_path,
            issue_id="1",
            output_dir=tmp_path / "out",
            task_dir=initial.task_dir,
            resume=True,
        ),
        lambda event: None,
        CancellationToken(),
    )

    assert resumed.status == "failed"
    assert any(failure.code == "checkpoint_config_mismatch" for failure in resumed.failures)


def test_pipeline_runner_reports_parse_failure_without_writing_partial_output(tmp_path):
    class FailingParser:
        def parse(self, path, issue_id):
            raise ValueError("bad DOCX")

    parser, analyzer, resolver, _ = _components(1)
    input_path = tmp_path / "sample.docx"
    input_path.write_bytes(b"fixture")
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    sentinel = output_dir / "keep.txt"
    sentinel.write_text("preserve", encoding="utf-8")
    runner = PipelineRunner(parser=FailingParser(), analyzer=analyzer, resolver=resolver)

    result = runner.run(
        TaskRequest(input_path=input_path, issue_id="1", output_dir=output_dir),
        lambda event: None,
        CancellationToken(),
    )

    assert result.status == "failed"
    assert any(failure.code == "parse_error" for failure in result.failures)
    assert sentinel.read_text(encoding="utf-8") == "preserve"


def test_pipeline_runner_fails_when_analyzer_returns_no_news(tmp_path):
    class EmptyParser:
        def parse(self, path, issue_id):
            return IssueDraft(issue_id=issue_id, input_path=str(path), entries=[])

    class EmptyAnalyzer:
        def analyze(self, draft):
            return Issue(issue_id=draft.issue_id, input_path=draft.input_path, news_items=[])

    input_path = tmp_path / "empty.docx"
    input_path.write_bytes(b"fixture")
    events = []

    result = PipelineRunner(parser=EmptyParser(), analyzer=EmptyAnalyzer()).run(
        TaskRequest(input_path=input_path, issue_id="empty", output_dir=tmp_path / "out"),
        events.append,
        CancellationToken(),
    )

    assert result.status == "failed"
    assert any(failure.code == "no_news_items" for failure in result.failures)
    assert any(event.kind == "analyze_failed" for event in events)
    assert not any(event.kind == "task_completed" for event in events)


def test_pipeline_runner_isolates_event_sink_failures(tmp_path):
    parser, analyzer, resolver, _ = _components(1)
    input_path = tmp_path / "sample.docx"
    input_path.write_bytes(b"fixture")
    seen = []

    def flaky_sink(event):
        seen.append(event.kind)
        if event.kind == "news_started":
            raise RuntimeError("UI disconnected")

    runner = PipelineRunner(parser=parser, analyzer=analyzer, resolver=resolver, image_transport=_image_transport)
    result = runner.run(
        TaskRequest(input_path=input_path, issue_id="1", output_dir=tmp_path / "out"),
        flaky_sink,
        CancellationToken(),
    )

    assert result.status == "completed"
    assert "news_started" in seen
    assert any(failure.code == "event_sink_failed" for failure in result.failures)


def test_pipeline_runner_new_task_never_overwrites_existing_output(tmp_path):
    parser, analyzer, resolver, _ = _components(1)
    input_path = tmp_path / "sample.docx"
    input_path.write_bytes(b"fixture")
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    sentinel = output_dir / "existing.txt"
    sentinel.write_text("old result", encoding="utf-8")
    runner = PipelineRunner(parser=parser, analyzer=analyzer, resolver=resolver, image_transport=_image_transport)

    result = runner.run(
        TaskRequest(input_path=input_path, issue_id="1", output_dir=output_dir),
        lambda event: None,
        CancellationToken(),
    )

    assert result.status == "completed"
    assert result.task_dir != output_dir
    assert sentinel.read_text(encoding="utf-8") == "old result"
    assert result.output_dir.name == "raw"


def test_resume_uses_serialized_checkpoint_issue_without_reanalyzing(tmp_path):
    parser, analyzer, resolver, _ = _components(2)
    input_path = tmp_path / "sample.docx"
    input_path.write_bytes(b"fixture")
    token = CancellationToken()
    runner = PipelineRunner(parser=parser, analyzer=analyzer, resolver=resolver, image_transport=_image_transport)

    cancelled = runner.run(
        TaskRequest(input_path=input_path, issue_id="1", output_dir=tmp_path / "out"),
        lambda event: token.cancel() if event.kind == "news_completed" and event.news_index == 1 else None,
        token,
    )

    class ExplodingParser:
        def parse(self, path, issue_id):
            raise AssertionError("resume must not parse DOCX again")

    class ExplodingAnalyzer:
        def analyze(self, draft):
            raise AssertionError("resume must not analyze DOCX again")

    resumed = PipelineRunner(
        parser=ExplodingParser(),
        analyzer=ExplodingAnalyzer(),
        resolver=resolver,
        image_transport=_image_transport,
    ).run(
        TaskRequest(
            input_path=input_path,
            issue_id="1",
            output_dir=tmp_path / "out",
            task_dir=cancelled.task_dir,
            resume=True,
        ),
        lambda event: None,
        CancellationToken(),
    )

    assert resumed.status == "completed"
    assert resumed.issue.news_items[0].title == "Game 1"


def test_completed_resume_is_idempotent_and_preserves_published_files(tmp_path):
    parser, analyzer, resolver, _ = _components(1)
    input_path = tmp_path / "sample.docx"
    input_path.write_bytes(b"fixture")
    runner = PipelineRunner(parser=parser, analyzer=analyzer, resolver=resolver, image_transport=_image_transport)
    initial = runner.run(
        TaskRequest(input_path=input_path, issue_id="1", output_dir=tmp_path / "out"),
        lambda event: None,
        CancellationToken(),
    )
    images = [
        *initial.task_dir.joinpath("assets").rglob("*.jpg"),
        *initial.task_dir.joinpath("assets").rglob("*.png"),
    ]
    assert images
    before = {path.relative_to(initial.task_dir): path.read_bytes() for path in images}

    class ExplodingParser:
        def parse(self, path, issue_id):
            raise AssertionError("completed resume must not rerun parser")

    resumed = PipelineRunner(parser=ExplodingParser(), resolver=resolver).run(
        TaskRequest(
            input_path=input_path,
            issue_id="1",
            output_dir=tmp_path / "out",
            task_dir=initial.task_dir,
            resume=True,
        ),
        lambda event: None,
        CancellationToken(),
    )

    assert resumed.status == "completed"
    resumed_images = [
        *resumed.task_dir.joinpath("assets").rglob("*.jpg"),
        *resumed.task_dir.joinpath("assets").rglob("*.png"),
    ]
    assert {path.relative_to(resumed.task_dir): path.read_bytes() for path in resumed_images} == before
    initial_originals = {path.relative_to(initial.output_dir): path.read_bytes() for path in (initial.output_dir / "originals").rglob("*") if path.is_file()}
    resumed_originals = {path.relative_to(resumed.output_dir): path.read_bytes() for path in (resumed.output_dir / "originals").rglob("*") if path.is_file()}
    assert resumed_originals == initial_originals


@pytest.mark.parametrize(
    "request_kwargs",
    [
        {"issue_id": "../escape"},
        {"issue_id": "nested/name"},
    ],
)
def test_new_task_rejects_unsafe_issue_id_without_escaping_output(tmp_path, request_kwargs):
    parser, analyzer, resolver, _ = _components(1)
    input_path = tmp_path / "sample.docx"
    input_path.write_bytes(b"fixture")
    output_root = tmp_path / "out"
    result = PipelineRunner(parser=parser, analyzer=analyzer, resolver=resolver).run(
        TaskRequest(input_path=input_path, output_dir=output_root, **request_kwargs),
        lambda event: None,
        CancellationToken(),
    )

    assert result.status == "failed"
    assert not (tmp_path / "escape").exists()


def test_new_task_accepts_unicode_issue_id_without_changing_published_id(tmp_path):
    parser, analyzer, resolver, _ = _components(1)
    input_path = tmp_path / "sample.docx"
    input_path.write_bytes(b"fixture")
    issue_id = "第259期"

    result = PipelineRunner(
        parser=parser,
        analyzer=analyzer,
        resolver=resolver,
        image_transport=_image_transport,
    ).run(
        TaskRequest(input_path=input_path, issue_id=issue_id, output_dir=tmp_path / "out"),
        lambda event: None,
        CancellationToken(),
    )

    assert result.status == "completed"
    assert result.issue.issue_id == issue_id
    assert result.task_dir.parent == (tmp_path / "out").resolve()
    payload = json.loads((result.output_dir / "image_index.json").read_text(encoding="utf-8"))
    assert payload["issue_id"] == issue_id


def test_new_task_rejects_arbitrary_task_and_checkpoint_paths(tmp_path):
    parser, analyzer, resolver, _ = _components(1)
    input_path = tmp_path / "sample.docx"
    input_path.write_bytes(b"fixture")
    runner = PipelineRunner(parser=parser, analyzer=analyzer, resolver=resolver)

    task_result = runner.run(
        TaskRequest(
            input_path=input_path,
            issue_id="1",
            output_dir=tmp_path / "out",
            task_dir=tmp_path / "outside-task",
        ),
        lambda event: None,
        CancellationToken(),
    )
    checkpoint_result = runner.run(
        TaskRequest(
            input_path=input_path,
            issue_id="1",
            output_dir=tmp_path / "out2",
            checkpoint_path=tmp_path / "outside.json",
        ),
        lambda event: None,
        CancellationToken(),
    )

    assert task_result.status == "failed"
    assert checkpoint_result.status == "failed"
    assert not (tmp_path / "outside.json").exists()


def test_request_config_path_changes_effective_configuration_hash(tmp_path):
    parser, analyzer, resolver, _ = _components(1)
    input_path = tmp_path / "sample.docx"
    input_path.write_bytes(b"fixture")
    custom_config = tmp_path / "custom.toml"
    custom_config.write_text("[selection]\nminimum_score = 99.0\n", encoding="utf-8")
    result = PipelineRunner(parser=parser, analyzer=analyzer, resolver=resolver, image_transport=_image_transport).run(
        TaskRequest(
            input_path=input_path,
            issue_id="1",
            output_dir=tmp_path / "out",
            config_path=custom_config,
        ),
        lambda event: None,
        CancellationToken(),
    )
    checkpoint = load_checkpoint(result.checkpoint_path).model_dump(mode="json")

    assert result.status == "completed"
    assert checkpoint["config_sha256"] != PipelineRunner(parser=parser, analyzer=analyzer, resolver=resolver)._config_hash()
    assert not any(candidate.selected for candidate in result.pipeline_result.candidates)


def test_invalid_checkpoint_associations_fail_closed(tmp_path):
    parser, analyzer, resolver, _ = _components(1)
    input_path = tmp_path / "sample.docx"
    input_path.write_bytes(b"fixture")
    token = CancellationToken()
    cancelled = PipelineRunner(parser=parser, analyzer=analyzer, resolver=resolver).run(
        TaskRequest(input_path=input_path, issue_id="1", output_dir=tmp_path / "out"),
        lambda event: token.cancel() if event.kind == "news_completed" else None,
        token,
    )
    checkpoint = load_checkpoint(cancelled.checkpoint_path).model_dump(mode="json")
    checkpoint["candidates"].append(
        {
            "news_id": "not-in-issue",
            "image_url": "https://cdn.example/invalid.jpg",
            "source_url": "https://cdn.example/page",
            "fetched_at": "2026-01-01T00:00:00Z",
        }
    )
    from galgame_news.tasks.repository import TaskRepository
    TaskRepository(cancelled.task_dir).save_checkpoint(checkpoint)

    resumed = PipelineRunner(parser=parser, analyzer=analyzer, resolver=resolver).run(
        TaskRequest(
            input_path=input_path,
            issue_id="1",
            output_dir=tmp_path / "out",
            task_dir=cancelled.task_dir,
            resume=True,
        ),
        lambda event: None,
        CancellationToken(),
    )

    assert resumed.status == "failed"
    assert any(failure.code == "checkpoint_invalid" for failure in resumed.failures)


def test_cancellation_from_final_news_completed_stays_cancelled(tmp_path):
    parser, analyzer, resolver, _ = _components(1)
    input_path = tmp_path / "sample.docx"
    input_path.write_bytes(b"fixture")
    token = CancellationToken()
    result = PipelineRunner(parser=parser, analyzer=analyzer, resolver=resolver, image_transport=_image_transport).run(
        TaskRequest(input_path=input_path, issue_id="1", output_dir=tmp_path / "out"),
        lambda event: token.cancel() if event.kind == "news_completed" else None,
        token,
    )

    assert result.status == "cancelled"
    assert load_checkpoint(result.checkpoint_path).model_dump(mode="json")["status"] == "cancelled"


def test_duplicate_failures_have_occurrence_count(tmp_path):
    parser, analyzer, _, _ = _components(1)

    class Resolver:
        def resolve(self, news):
            return [
                SourceRef(url="https://video.example/a", domain="video.example", source_type=SourceType.VIDEO),
                SourceRef(url="https://video.example/a", domain="video.example", source_type=SourceType.VIDEO),
            ]

    class Adapter:
        def collect(self, news, source, context):
            from galgame_news.domain import FailureRecord, FailureStage
            return CollectionResult(
                failures=[
                    FailureRecord(
                        stage=FailureStage.COLLECT,
                        news_id=news.id,
                        code="duplicate",
                        message="same",
                        source_url=source.url,
                    )
                ]
            )

    input_path = tmp_path / "sample.docx"
    input_path.write_bytes(b"fixture")
    result = PipelineRunner(
        parser=parser,
        analyzer=analyzer,
        resolver=Resolver(),
        adapter_factory=lambda *_: Adapter(),
    ).run(
        TaskRequest(input_path=input_path, issue_id="1", output_dir=tmp_path / "out", no_videos=True),
        lambda event: None,
        CancellationToken(),
    )

    duplicate = [failure for failure in result.failures if failure.code == "duplicate"]
    assert len(duplicate) == 1
    assert duplicate[0].occurrences == 2


def test_runner_emits_stage_lifecycle_events(tmp_path):
    parser, analyzer, resolver, _ = _components(1)
    input_path = tmp_path / "sample.docx"
    input_path.write_bytes(b"fixture")
    events = []
    PipelineRunner(parser=parser, analyzer=analyzer, resolver=resolver, image_transport=_image_transport).run(
        TaskRequest(input_path=input_path, issue_id="1", output_dir=tmp_path / "out"),
        events.append,
        CancellationToken(),
    )

    for stage in ("parse", "analyze", "resolve", "collect", "download", "curate", "output"):
        assert f"{stage}_started" in [event.kind for event in events]
        assert f"{stage}_completed" in [event.kind for event in events]


def test_missing_input_returns_failed_result_and_task_failed_event(tmp_path):
    events = []
    result = PipelineRunner().run(
        TaskRequest(input_path=tmp_path / "missing.docx", issue_id="1", output_dir=tmp_path / "out"),
        events.append,
        CancellationToken(),
    )

    assert result.status == "failed"
    assert any(failure.code == "input_missing" for failure in result.failures)
    assert "task_failed" in [event.kind for event in events]


def test_output_io_failure_returns_failed_result(monkeypatch, tmp_path):
    parser, analyzer, resolver, _ = _components(1)
    input_path = tmp_path / "sample.docx"
    input_path.write_bytes(b"fixture")

    class BrokenOutput:
        def write(self, result, output_dir):
            raise OSError("disk full")

    monkeypatch.setattr("galgame_news.pipeline.runner.OutputManager", BrokenOutput)
    result = PipelineRunner(parser=parser, analyzer=analyzer, resolver=resolver).run(
        TaskRequest(input_path=input_path, issue_id="1", output_dir=tmp_path / "out"),
        lambda event: None,
        CancellationToken(),
    )

    assert result.status == "failed"
    assert any(failure.code == "output_error" for failure in result.failures)


def test_legacy_output_reuses_existing_directory_without_clearing_unmanaged_files(tmp_path):
    from galgame_news.application import Application

    parser, analyzer, resolver, _ = _components(1)
    fixture = tmp_path / "sample.docx"
    fixture.write_bytes(b"fixture")
    output = tmp_path / "existing-output"
    output.mkdir()
    sentinel = output / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")

    result = Application(
        parser=parser, analyzer=analyzer, resolver=resolver,
        no_videos=True,
    ).run(fixture, issue_id="1", output_dir=output)

    assert result.issue.issue_id == "1"
    assert not any(failure.code == "setup_error" for failure in result.failures)
    assert (output / "image_index.json").is_file()
    assert result.issue.issue_id == "1"
    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert (output / "image_index.json").is_file()


def test_task_failure_occurrences_are_not_doubled(tmp_path):
    class FailingParser:
        def parse(self, path, issue_id):
            raise ValueError("bad DOCX")

    input_path = tmp_path / "sample.docx"
    input_path.write_bytes(b"fixture")
    result = PipelineRunner(parser=FailingParser()).run(
        TaskRequest(input_path=input_path, issue_id="1", output_dir=tmp_path / "out"),
        lambda event: None,
        CancellationToken(),
    )

    parse_failures = [failure for failure in result.failures if failure.code == "parse_error"]
    assert len(parse_failures) == 1
    assert parse_failures[0].occurrences == 1


def test_terminal_event_sink_failure_is_in_result_and_checkpoint(tmp_path):
    parser, analyzer, resolver, _ = _components(1)
    input_path = tmp_path / "sample.docx"
    input_path.write_bytes(b"fixture")

    def sink(event):
        if event.kind == "task_completed":
            raise RuntimeError("terminal UI disconnected")

    result = PipelineRunner(
        parser=parser, analyzer=analyzer, resolver=resolver,
        image_transport=_image_transport,
    ).run(
        TaskRequest(input_path=input_path, issue_id="1", output_dir=tmp_path / "out"),
        sink,
        CancellationToken(),
    )

    assert result.status == "completed"
    assert any(failure.code == "event_sink_failed" for failure in result.failures)
    checkpoint = load_checkpoint(result.checkpoint_path).model_dump(mode="json")
    assert any(
        failure["code"] == "event_sink_failed"
        for failure in checkpoint["failures"]
    )
    assert any(
        failure["code"] == "event_sink_failed"
        for failure in checkpoint["result"]["failures"]
    )


@pytest.mark.parametrize("phase", ["parse", "analyze"])
def test_task_stage_failure_does_not_double_failure_occurrences(tmp_path, phase):
    parser, analyzer, resolver, _ = _components(1)
    if phase == "parse":
        class FailingParser:
            def parse(self, path, issue_id):
                raise ValueError("stage failed")

        parser = FailingParser()
    else:
        class FailingAnalyzer:
            def analyze(self, draft):
                raise ValueError("stage failed")

        analyzer = FailingAnalyzer()

    input_path = tmp_path / "sample.docx"
    input_path.write_bytes(b"fixture")
    result = PipelineRunner(parser=parser, analyzer=analyzer, resolver=resolver).run(
        TaskRequest(input_path=input_path, issue_id="1", output_dir=tmp_path / "out"),
        lambda event: None,
        CancellationToken(),
    )

    failure = next(failure for failure in result.failures if failure.code == f"{phase}_error")
    assert failure.occurrences == 1


def test_curate_failure_does_not_double_failure_occurrences(monkeypatch, tmp_path):
    parser, analyzer, resolver, _ = _components(1)
    input_path = tmp_path / "sample.docx"
    input_path.write_bytes(b"fixture")
    runner = PipelineRunner(
        parser=parser,
        analyzer=analyzer,
        resolver=resolver,
        image_transport=_image_transport,
    )

    def fail_curate(*_args, **_kwargs):
        raise ValueError("curation failed")

    monkeypatch.setattr(runner, "_curate_result", fail_curate)
    result = runner.run(
        TaskRequest(input_path=input_path, issue_id="1", output_dir=tmp_path / "out"),
        lambda event: None,
        CancellationToken(),
    )

    failure = next(failure for failure in result.failures if failure.code == "curate_error")
    assert failure.occurrences == 1


def test_outer_task_failure_does_not_double_failure_occurrences(monkeypatch, tmp_path):
    parser, analyzer, resolver, _ = _components(1)
    input_path = tmp_path / "sample.docx"
    input_path.write_bytes(b"fixture")
    runner = PipelineRunner(
        parser=parser,
        analyzer=analyzer,
        resolver=resolver,
        image_transport=_image_transport,
    )
    original_write_checkpoint = runner._write_checkpoint

    def fail_final_checkpoint(path, *, status, **kwargs):
        if status == "completed":
            raise OSError("final checkpoint failed")
        return original_write_checkpoint(path, status=status, **kwargs)

    monkeypatch.setattr(runner, "_write_checkpoint", fail_final_checkpoint)
    result = runner.run(
        TaskRequest(input_path=input_path, issue_id="1", output_dir=tmp_path / "out"),
        lambda event: None,
        CancellationToken(),
    )

    failure = next(failure for failure in result.failures if failure.code == "output_error")
    assert failure.occurrences == 1


@pytest.mark.parametrize("event_kind", ["output_completed", "task_completed"])
def test_completion_event_sink_failure_is_returned_and_checkpointed(tmp_path, event_kind):
    parser, analyzer, resolver, _ = _components(1)
    input_path = tmp_path / "sample.docx"
    input_path.write_bytes(b"fixture")

    def flaky_sink(event):
        if event.kind == event_kind:
            raise RuntimeError(f"{event_kind} sink failed")

    result = PipelineRunner(
        parser=parser,
        analyzer=analyzer,
        resolver=resolver,
        image_transport=_image_transport,
    ).run(
        TaskRequest(input_path=input_path, issue_id="1", output_dir=tmp_path / "out"),
        flaky_sink,
        CancellationToken(),
    )

    assert result.status == "completed"
    assert any(failure.code == "event_sink_failed" for failure in result.failures)
    checkpoint = load_checkpoint(result.checkpoint_path).model_dump(mode="json")
    assert any(failure["code"] == "event_sink_failed" for failure in checkpoint["failures"])


def test_task_request_does_not_expose_legacy_output_flag(tmp_path):
    request = TaskRequest(
        input_path=tmp_path / "sample.docx",
        issue_id="1",
        output_dir=tmp_path / "out",
    )

    assert "legacy_output" not in TaskRequest.__dataclass_fields__
    assert not hasattr(request, "legacy_output")
