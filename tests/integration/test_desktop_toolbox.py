from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

from galgame_news.desktop.controller import DesktopController
from galgame_news.domain import (
    CollectionResult,
    DiscoveryMethod,
    FailureRecord,
    FailureStage,
    ImageCandidate,
    ImageNeed,
    ImageType,
    Issue,
    IssueDraft,
    NewsDraft,
    NewsItem,
    PipelineResult,
    ScoreBreakdown,
    SourceRef,
    SourceType,
    VideoCandidate,
)
from galgame_news.pipeline import CancellationToken, PipelineRunner
from galgame_news.settings import CredentialStore, InMemoryCredentialBackend
from galgame_news.tasks import TaskStore


def _image_response(url: str, **_kwargs):
    stream = BytesIO()
    Image.new("RGB", (800, 600), "purple").save(stream, format="JPEG")
    return SimpleNamespace(
        content=stream.getvalue(),
        headers={"content-type": "image/jpeg"},
        status_code=200,
        url=url,
    )


class _OfflineFixture:
    def __init__(self, root: Path, count: int = 3):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.input_path = root / "issue.docx"
        self.input_path.write_bytes(b"offline desktop fixture")
        self.assets: dict[int, Path] = {}
        for sequence in range(1, count + 1):
            asset = root / f"asset-{sequence}.jpg"
            Image.new("RGB", (800, 600), "purple").save(asset, format="JPEG")
            self.assets[sequence] = asset
        self.resolved: list[int] = []
        self.parsed = 0
        self.analyzed = 0

        fixture = self

        class Parser:
            def parse(self, path, issue_id):
                fixture.parsed += 1
                return IssueDraft(
                    issue_id=issue_id,
                    input_path=str(path),
                    entries=[
                        NewsDraft(
                            sequence=sequence,
                            section="新作",
                            title=f"Game {sequence}",
                            body="公开游戏 CG 图片",
                        )
                        for sequence in range(1, count + 1)
                    ],
                )

        class Analyzer:
            def analyze(self, draft):
                fixture.analyzed += 1
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
                            game_names=[entry.title],
                            source_urls=[
                                f"https://cdn.example/{entry.sequence}.jpg"
                            ],
                            image_need=ImageNeed.EXPLICIT_NEW_IMAGE,
                        )
                        for entry in draft.entries
                    ],
                )

        class Resolver:
            def resolve(self, news):
                fixture.resolved.append(news.sequence)
                return [
                    SourceRef(
                        url=f"https://cdn.example/{news.sequence}.jpg",
                        domain="cdn.example",
                        source_type=SourceType.DIRECT_IMAGE,
                        discovered_via=DiscoveryMethod.DOCUMENT,
                        officiality=1.0,
                    )
                ]

        class Adapter:
            def collect(self, news, source, context):
                candidate = ImageCandidate(
                    news_id=news.id,
                    image_url=source.url,
                    source_url=source.url,
                    source_type=SourceType.DIRECT_IMAGE,
                    image_type=ImageType.GAME_CG,
                    fetched_at=context.now,
                    width=800,
                    height=600,
                    mime_type="image/jpeg",
                    downloadable=True,
                    local_path=str(fixture.assets[news.sequence]),
                    selected=True,
                    signals={
                        "page_title": f"{news.title} official gallery",
                        "cg_match": 1.0,
                    },
                    score=ScoreBreakdown(
                        relevance=0.9,
                        freshness=0.9,
                        source_trust=0.9,
                        quality=0.9,
                        type_match=0.9,
                        total=90.0,
                    ),
                )
                return CollectionResult(candidates=[candidate])

        self.parser = Parser()
        self.analyzer = Analyzer()
        self.resolver = Resolver()
        self.adapter = Adapter()

    def runner(self) -> PipelineRunner:
        return PipelineRunner(
            parser=self.parser,
            analyzer=self.analyzer,
            resolver=self.resolver,
            adapter_factory=lambda *_args: self.adapter,
            image_transport=_image_response,
        )


def _controller(*, runner_factory, app_data: Path, task_store: TaskStore | None = None):
    return DesktopController(
        runner_factory=runner_factory,
        task_store=task_store,
        app_data=app_data,
        credential_store=CredentialStore(InMemoryCredentialBackend()),
    )


def test_desktop_controller_runs_three_news_offline(qtbot, tmp_path: Path):
    fixture = _OfflineFixture(tmp_path / "fixture")
    events = []
    controller = _controller(
        runner_factory=fixture.runner,
        app_data=tmp_path / "app",
    )
    qtbot.addWidget(controller.progress_page)
    controller.progress_event.connect(events.append)

    try:
        assert controller.start_task(
            input_path=fixture.input_path,
            issue_id="offline-3",
            output_dir=tmp_path / "runs",
            offline=True,
            no_videos=True,
        )
        qtbot.waitUntil(lambda: not controller.is_running, timeout=5000)

        assert controller.last_result.status == "completed"
        completed = [
            event
            for event in events
            if event.kind == "news_completed"
        ]
        assert len(completed) == 3
        assert [event.news_index for event in completed] == [1, 2, 3]
        assert fixture.resolved == [1, 2, 3]
        assert fixture.parsed == 1
        assert fixture.analyzed == 1
        assert (controller.last_result.output_dir / "image_index.json").is_file()
    finally:
        controller.close()
        controller.task_store.close()


def test_desktop_controller_accepts_unicode_issue_id_and_keeps_raw_output_id(
    qtbot, tmp_path: Path
):
    fixture = _OfflineFixture(tmp_path / "fixture")
    controller = _controller(
        runner_factory=fixture.runner,
        app_data=tmp_path / "app",
    )
    qtbot.addWidget(controller.progress_page)
    issue_id = "第259期"

    try:
        assert controller.start_task(
            input_path=fixture.input_path,
            issue_id=issue_id,
            output_dir=tmp_path / "runs",
            offline=True,
            no_videos=True,
        )
        qtbot.waitUntil(
            lambda: controller.last_result is not None and not controller.is_running,
            timeout=5000,
        )

        assert controller.last_result.status == "completed"
        assert controller.last_result.issue.issue_id == issue_id
        payload = json.loads(
            (controller.last_result.output_dir / "image_index.json").read_text(
                encoding="utf-8"
            )
        )
        assert payload["issue_id"] == issue_id
    finally:
        controller.close()
        controller.task_store.close()


def test_desktop_controller_rejects_issue_id_path_traversal(qtbot, tmp_path: Path):
    fixture = _OfflineFixture(tmp_path / "fixture")
    controller = _controller(
        runner_factory=fixture.runner,
        app_data=tmp_path / "app",
    )
    qtbot.addWidget(controller.progress_page)
    failed_results = []
    controller.task_failed.connect(failed_results.append)

    try:
        assert controller.start_task(
            input_path=fixture.input_path,
            issue_id="../escape",
            output_dir=tmp_path / "runs",
            offline=True,
            no_videos=True,
        )
        qtbot.waitUntil(lambda: not controller.is_running, timeout=5000)

        assert failed_results and failed_results[0].status == "failed"
        assert any("unsafe issue_id" in failure.message for failure in failed_results[0].failures)
        assert not (tmp_path / "escape").exists()
    finally:
        controller.close()
        controller.task_store.close()


def test_desktop_controller_resume_after_news_two_does_not_repeat_news_one(
    qtbot, tmp_path: Path
):
    fixture = _OfflineFixture(tmp_path / "fixture")
    run_number = 0

    class InterruptingRunner:
        def __init__(self, cancel_on_news_two: bool):
            self.delegate = fixture.runner()
            self.cancel_on_news_two = cancel_on_news_two

        def run(self, request, event_sink=None, cancellation_token=None):
            token = cancellation_token or CancellationToken()

            def sink(event):
                if event_sink is not None:
                    event_sink(event)
                if (
                    self.cancel_on_news_two
                    and event.kind == "news_started"
                    and event.news_index == 2
                ):
                    token.cancel()

            return self.delegate.run(request, sink, token)

    def factory():
        nonlocal run_number
        runner = InterruptingRunner(cancel_on_news_two=run_number == 0)
        run_number += 1
        return runner

    events = []
    controller = _controller(runner_factory=factory, app_data=tmp_path / "app")
    qtbot.addWidget(controller.progress_page)
    controller.progress_event.connect(events.append)

    try:
        assert controller.start_task(
            input_path=fixture.input_path,
            issue_id="resume-3",
            output_dir=tmp_path / "runs",
            offline=True,
            no_videos=True,
        )
        qtbot.waitUntil(lambda: not controller.is_running, timeout=5000)
        assert controller.last_result.status == "cancelled"
        record = controller.task_store.active_task
        assert record is not None

        assert controller.resume_task(record)
        qtbot.waitUntil(
            lambda: controller.last_result is not None and not controller.is_running,
            timeout=5000,
        )
        assert controller.last_result.status == "completed"
        assert fixture.resolved == [1, 2, 3]
        assert fixture.parsed == 1
        assert fixture.analyzed == 1
        starts = [
            event
            for event in events
            if event.kind == "news_started"
        ]
        assert [event.news_index for event in starts].count(1) == 1
        # News 2 was cancelled after its start event, so resuming it emits a
        # second start.  Only completed news 1 must be skipped.
        assert [event.news_index for event in starts].count(2) == 2
        assert [event.news_index for event in starts].count(3) == 1
        assert any(event.kind == "news_skipped" and event.news_index == 1 for event in events)
    finally:
        controller.close()
        controller.task_store.close()


def test_single_news_retry_appends_candidates_without_overwriting_raw(
    qtbot, tmp_path: Path
):
    input_path = tmp_path / "issue.docx"
    input_path.write_bytes(b"retry fixture")
    task_root = tmp_path / "managed-task"
    raw = task_root / "raw"
    raw.mkdir(parents=True)
    old_source = raw / "old.jpg"
    old_source.write_bytes(b"old raw candidate")
    fetched_at = datetime.now(timezone.utc)
    old = ImageCandidate(
        news_id="news-1",
        image_url="https://cdn.example/old.jpg",
        source_url="https://official.example/game",
        source_type=SourceType.OFFICIAL_SITE,
        image_type=ImageType.GAME_CG,
        fetched_at=fetched_at,
        width=800,
        height=600,
        mime_type="image/jpeg",
        downloadable=True,
        local_path=str(old_source),
        selected=True,
    )
    (raw / "image_index.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "issue_id": "251",
                "news_items": [
                    {
                        "news_id": "news-1",
                        "sequence": 1,
                        "section": "新作",
                        "title": "Game",
                        "candidates": [old.id],
                    }
                ],
                "candidates": [old.model_dump(mode="json")],
            }
        ),
        encoding="utf-8",
    )
    (raw / "video_index.json").write_text(
        json.dumps({"schema_version": 1, "issue_id": "251", "videos": []}),
        encoding="utf-8",
    )
    raw_before = {
        path.relative_to(raw).as_posix(): path.read_bytes()
        for path in raw.rglob("*")
        if path.is_file()
    }
    store = TaskStore(tmp_path / "app")
    record = store.create_task("251", input_path, task_root=task_root)
    retry_urls: list[str] = []

    class RetryAdapter:
        def collect(self, news, source, context):
            retry_urls.append(source.url)
            return CollectionResult(
                candidates=[
                    ImageCandidate(
                        news_id=news.id,
                        image_url="https://cdn.example/retry.jpg",
                        source_url=source.url,
                        source_type=SourceType.OFFICIAL_SITE,
                        fetched_at=context.now,
                        downloadable=True,
                    )
                ]
            )

    def runner_factory():
        return PipelineRunner(
            adapter_factory=lambda *_args: RetryAdapter(),
            image_transport=_image_response,
        )

    controller = _controller(
        runner_factory=runner_factory,
        app_data=tmp_path / "unused-app",
        task_store=store,
    )
    qtbot.addWidget(controller.review_page)

    try:
        assert controller.load_review(record) is not None
        assert controller.retry_news(
            "news-1", "https://official.example/retry"
        )
        qtbot.waitUntil(lambda: not controller.is_supplement_running, timeout=10000)
        session = controller.review_page.session
        assert session is not None
        assert [str(item.id) for item in session.images] == [
            str(old.id),
            "a6b8b2f82f9a1e2a",
        ] or len(session.images) == 2
        assert retry_urls == ["https://official.example/retry"]
        assert {
            path.relative_to(raw).as_posix(): path.read_bytes()
            for path in raw.rglob("*")
            if path.is_file()
        } == raw_before
        attempts = list((task_root / "retries" / "news-1").iterdir())
        assert attempts and (attempts[0] / "image_index.json").is_file()
    finally:
        controller.close()
        store.close()


def _write_251_fixture(root: Path) -> Path:
    root.mkdir(parents=True)
    news = [
        ("news-x", 1, "新作", "x1"),
        ("news-h", 2, "汉化", "h1"),
        ("news-z", 3, "周边", "z1"),
    ]
    candidates = []
    news_items = []
    for news_id, sequence, section, prefix in news:
        source = root / f"{prefix}-source.jpg"
        # Imported legacy files must still be valid, decodable originals.
        Image.new("RGB", (800, 600), "red").save(source, format="JPEG")
        candidate = ImageCandidate(
            news_id=news_id,
            image_url=f"https://cdn.example/{prefix}.jpg",
            source_url="https://official.example/game",
            source_type=SourceType.OFFICIAL_SITE,
            image_type=ImageType.GAME_CG,
            fetched_at=datetime.now(timezone.utc),
            width=800,
            height=600,
            mime_type="image/jpeg",
            downloadable=True,
            local_path=str(source),
            selected=True,
        )
        candidates.append(candidate.model_dump(mode="json"))
        news_items.append(
            {
                "news_id": news_id,
                "sequence": sequence,
                "section": section,
                "title": f"{prefix} title",
                "status": "selected",
                "candidates": [candidate.id],
            }
        )
    (root / "image_index.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "issue_id": "251",
                "news_items": news_items,
                "candidates": candidates,
            }
        ),
        encoding="utf-8",
    )
    (root / "video_index.json").write_text(
        json.dumps({"schema_version": 1, "issue_id": "251", "news_items": [], "videos": []}),
        encoding="utf-8",
    )
    return root


def test_import_251_style_fixture_is_read_only_and_exports_xhz_names(qtbot, tmp_path: Path):
    fixture = _write_251_fixture(tmp_path / "legacy-251")
    before = {
        path.relative_to(fixture).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in fixture.rglob("*")
        if path.is_file()
    }
    store = TaskStore(tmp_path / "app")
    controller = _controller(
        runner_factory=lambda: PipelineRunner(),
        app_data=tmp_path / "unused-app",
        task_store=store,
    )
    qtbot.addWidget(controller.review_page)

    try:
        record = controller.import_output(fixture)
        session = controller.review_page.session
        assert session is not None
        manifest = session.export_final()
        assert set(manifest["files"]) == {
            "images/x1/x1.01.png",
            "images/h1/h1.01.png",
            "images/z1/z1.01.png",
        }
        assert record.imported is True
        after = {
            path.relative_to(fixture).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in fixture.rglob("*")
            if path.is_file()
        }
        assert after == before
        assert session.task_root.parent == fixture.parent
        with Image.open(session.task_root / "final" / "images" / "x1" / "x1.01.png") as image:
            assert image.format == "PNG" and image.size == (800, 600)
    finally:
        controller.close()
        store.close()


def test_duplicate_videos_and_failures_are_merged_in_desktop_run(qtbot, tmp_path: Path):
    input_path = tmp_path / "issue.docx"
    input_path.write_bytes(b"duplicate fixture")

    class Parser:
        def parse(self, path, issue_id):
            return IssueDraft(
                issue_id=issue_id,
                input_path=str(path),
                entries=[NewsDraft(sequence=1, section="新作", title="Game", body="")],
            )

    class Analyzer:
        def analyze(self, draft):
            return Issue(
                issue_id=draft.issue_id,
                input_path=draft.input_path,
                news_items=[
                    NewsItem(
                        issue_id=draft.issue_id,
                        sequence=1,
                        section="新作",
                        title="Game",
                        body="",
                    )
                ],
            )

    class Resolver:
        def resolve(self, news):
            return [
                SourceRef(
                    url="https://video.example/watch",
                    domain="video.example",
                    source_type=SourceType.VIDEO,
                ),
                SourceRef(
                    url="https://video.example/watch",
                    domain="video.example",
                    source_type=SourceType.VIDEO,
                ),
            ]

    class Adapter:
        def collect(self, news, source, context):
            return CollectionResult(
                video_candidates=[
                    VideoCandidate(
                        news_id=news.id,
                        source_url=source.url,
                        video_url="https://cdn.example/same.mp4",
                        title="Trailer",
                    )
                ],
                failures=[
                    FailureRecord(
                        stage=FailureStage.COLLECT,
                        news_id=news.id,
                        code="duplicate_source",
                        message="same source",
                        source_url=source.url,
                    )
                ],
            )

    def runner_factory():
        return PipelineRunner(
            parser=Parser(),
            analyzer=Analyzer(),
            resolver=Resolver(),
            adapter_factory=lambda *_args: Adapter(),
        )

    controller = _controller(runner_factory=runner_factory, app_data=tmp_path / "app")
    qtbot.addWidget(controller.progress_page)
    try:
        assert controller.start_task(
            input_path=input_path,
            issue_id="duplicate",
            output_dir=tmp_path / "runs",
            offline=True,
        )
        qtbot.waitUntil(lambda: not controller.is_running, timeout=5000)
        result = controller.last_result
        assert result.status == "completed"
        assert len(result.videos) == 1
        duplicate_failures = [
            failure
            for failure in result.failures
            if failure.code == "duplicate_source"
        ]
        assert len(duplicate_failures) == 1
        assert duplicate_failures[0].occurrences == 2
        skipped_failures = [
            failure
            for failure in result.failures
            if failure.code == "offline_video_skip"
        ]
        assert len(skipped_failures) == 1
        assert skipped_failures[0].occurrences == 2
    finally:
        controller.close()
        controller.task_store.close()


def test_desktop_settings_keep_credentials_out_of_app_data(qtbot, tmp_path: Path):
    socialdata = "socialdata-secret-task5"
    backend = InMemoryCredentialBackend()
    controller = DesktopController(
        app_data=tmp_path / "app",
        credential_store=CredentialStore(backend),
    )
    qtbot.addWidget(controller.settings_page)
    try:
        controller.settings_page.socialdata_edit.setText(socialdata)
        controller.settings_page.save()
        persisted = [
            path.read_bytes()
            for path in (tmp_path / "app").rglob("*")
            if path.is_file()
        ]
        assert persisted
        assert all(socialdata.encode() not in payload for payload in persisted)
        assert socialdata not in repr(controller.credential_store)
    finally:
        controller.close()
        controller.task_store.close()
