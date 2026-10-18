from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

from galgame_news.domain import (
    ImageNeed,
    Issue,
    IssueDraft,
    NewsDraft,
    NewsItem,
    SourceRef,
    SourceType,
)
from galgame_news.pipeline import CancellationToken, PipelineRunner, TaskRequest
from galgame_news.review import ReviewSession
from galgame_news.review.session import ReviewDecision
from galgame_news.tasks.repository import TaskRepository


class _Parser:
    def parse(self, path, issue_id):
        return IssueDraft(
            issue_id=issue_id,
            input_path=str(path),
            entries=[
                NewsDraft(
                    sequence=1, section="新作", title="Fixture title", body="offline"
                )
            ],
        )


class _Analyzer:
    def analyze(self, draft):
        return Issue(
            issue_id=draft.issue_id,
            input_path=draft.input_path,
            news_items=[
                NewsItem(
                    issue_id=draft.issue_id,
                    sequence=1,
                    section="新作",
                    title="Fixture title",
                    body="offline",
                    image_need=ImageNeed.EXPLICIT_NEW_IMAGE,
                )
            ],
        )


class _Resolver:
    def __init__(self):
        self.calls = 0

    def resolve(self, news):
        self.calls += 1
        return [
            SourceRef(
                url="https://fixture.invalid/image.png",
                domain="fixture.invalid",
                source_type=SourceType.DIRECT_IMAGE,
            )
        ]


def _transport(url, **_kwargs):
    stream = BytesIO()
    Image.new("RGB", (800, 600), (120, 40, 200)).save(stream, format="PNG")
    return type(
        "Response",
        (),
        {
            "content": stream.getvalue(),
            "headers": {"content-type": "image/png"},
            "status_code": 200,
            "url": url,
        },
    )()


def test_default_sqlite_pipeline_resume_review_batch_move_backup_and_integrity(
    tmp_path,
):
    source = tmp_path / "fixture.docx"
    source.write_bytes(b"local fixture input")
    output_root = tmp_path / "output"
    resolver = _Resolver()
    runner = PipelineRunner(
        parser=_Parser(),
        analyzer=_Analyzer(),
        resolver=resolver,
        image_transport=_transport,
    )
    request = TaskRequest(
        input_path=source,
        issue_id="71",
        output_dir=output_root,
        offline=True,
        no_videos=True,
    )

    first = runner.run(request, cancellation_token=CancellationToken())
    assert first.status == "completed"
    task_root = Path(first.task_dir)
    assert TaskRepository.is_authoritative(task_root)
    assert not (task_root / "checkpoint.json").exists()
    assert (
        not list((task_root / "media_state").glob("*.json"))
        if (task_root / "media_state").exists()
        else True
    )
    assert (
        first.pipeline_result.candidates
    ), "fixture run should yield one image candidate"
    final_before = {
        p.relative_to(task_root / "final"): sha256(p.read_bytes()).hexdigest()
        for p in (task_root / "final").rglob("*")
        if p.is_file()
    }
    assert final_before
    call_count = resolver.calls

    resumed = runner.run(
        TaskRequest(
            input_path=source,
            issue_id="71",
            output_dir=output_root,
            offline=True,
            no_videos=True,
            resume=True,
            task_dir=task_root,
        ),
        cancellation_token=CancellationToken(),
    )
    assert resumed.status == "completed"
    assert resolver.calls == call_count
    assert {
        p.relative_to(task_root / "final"): sha256(p.read_bytes()).hexdigest()
        for p in (task_root / "final").rglob("*")
        if p.is_file()
    } == final_before

    session = ReviewSession.from_output(task_root / "raw", task_root=task_root)
    media_id = session.images[0].id
    session.set_decisions([media_id], ReviewDecision.ACCEPTED, persist=True)
    reopened = ReviewSession.from_output(task_root / "raw", task_root=task_root)
    assert reopened.decision(media_id) is ReviewDecision.ACCEPTED
    assert media_id in reopened._manual_decisions

    repo = TaskRepository(task_root, create=False)
    candidate = reopened.images[0].model_dump(mode="json")
    news_id = str(candidate["news_id"])
    repo.save_batch(
        news_id,
        "supplement-1",
        f"retries/{news_id}/supplement-1",
        "supplement",
        "completed",
        [candidate],
        [],
        [],
    )
    assert repo.read_batch(news_id, "supplement-1")["images"][0]["id"] == media_id
    assert not (
        task_root / "retries" / news_id / "supplement-1" / "image_index.json"
    ).exists()
    assert repo.load_review_state()["decisions"][media_id] == "accepted"

    # Shared assets live outside retry-attempt folders, so an index-only DB
    # batch remains readable without materializing an attempt directory.
    repo.save_batch(
        news_id,
        "db-only-no-dir",
        f"retries/{news_id}/db-only-no-dir",
        "retry",
        "completed",
        [candidate],
        [],
        [],
    )
    assert not (task_root / "retries" / news_id / "db-only-no-dir").exists()
    db_only_delta = reopened.prepare_retry_delta(news_id, "db-only-no-dir")
    assert [item.candidate.id for item in db_only_delta.images] == [media_id]

    moved = tmp_path / "moved-task"
    import shutil

    shutil.copytree(task_root, moved)
    moved_repo = TaskRepository(moved, create=False)
    assert moved_repo.integrity_check()["ok"]
    backup = tmp_path / "backup"
    TaskRepository(task_root, create=False).backup(backup)
    assert TaskRepository(backup, create=False).integrity_check()["ok"]


def test_sqlite_supplement_batch_and_strict_retry_delta_are_offline(tmp_path):
    """Supplement collection writes the real news ID to SQLite and is mergeable."""
    from galgame_news.domain import (
        CollectionResult,
        ImageCandidate,
        NewsItem,
        SourceType,
    )

    source = tmp_path / "fixture.docx"
    source.write_bytes(b"local fixture input")
    runner = PipelineRunner(
        parser=_Parser(),
        analyzer=_Analyzer(),
        resolver=_Resolver(),
        image_transport=_transport,
    )

    class Adapter:
        def collect(self, item, source_ref, _context):
            return CollectionResult(
                candidates=[
                    ImageCandidate(
                        news_id=item.id,
                        image_url="https://93.184.216.34/supplement.jpg",
                        source_url=source_ref.url,
                        source_type=SourceType.OFFICIAL_SITE,
                        nearby_text=item.title,
                        fetched_at=datetime.now(timezone.utc),
                    )
                ]
            )

    first = runner.run(
        TaskRequest(
            input_path=source,
            issue_id="72",
            output_dir=tmp_path / "output",
            no_videos=True,
        ),
        cancellation_token=CancellationToken(),
    )
    assert first.status == "completed"
    task_root = Path(first.task_dir)
    news = first.pipeline_result.issue.news_items[0]
    runner.adapter_factory = lambda *_args: Adapter()
    request = TaskRequest(
        input_path=source, issue_id="72", output_dir=task_root, no_videos=True
    )
    review = ReviewSession.from_output(task_root / "raw", task_root=task_root)

    result = runner.supplement_news(
        task_root=task_root,
        issue_id="72",
        news_id=news.id,
        source_url="https://93.184.216.34/gallery",
        news_item=news,
        request=request,
    )
    assert result.status in {"completed", "partial"}
    assert len(result.candidates) == 1
    repo = TaskRepository(task_root, create=False)
    batch = repo.read_batch(news.id, result.attempt_id)
    assert batch["kind"] == "supplement"
    assert batch["images"][0]["news_id"] == news.id
    assert batch["images"][0]["selected"] is False
    assert not (result.path / "image_index.json").exists()

    delta = review.prepare_retry_delta(news.id, result.attempt_id)
    merged = review.apply_retry_delta(delta, force_pending=True)
    assert merged.added_image_ids == [str(result.candidates[0].id)]
    assert review.decision(str(result.candidates[0].id)) is ReviewDecision.PENDING

    # Imported authoritative folders may have a batch index without a checkpoint.
    # Strict retry can use a caller-supplied real NewsItem, but never invented metadata.
    from galgame_news.pipeline.supplement import _news

    index_only = tmp_path / "index-only"
    TaskRepository(index_only)
    resolved_news, checkpoint = _news(
        index_only, "72", news.id, news, strict_checkpoint=True
    )
    assert resolved_news.id == news.id and resolved_news.title == news.title
    assert checkpoint == {}


def test_cli_run_uses_sqlite_runner_with_output_root_not_legacy_application(
    monkeypatch, tmp_path
):
    from galgame_news import cli
    from galgame_news import pipeline

    seen = {}

    class FakeApp:
        def __init__(self, **kwargs):
            self.parser = self.analyzer = self.resolver = self.history = None
            self.config = None
            self.source_transport = self.image_transport = None
            self.video_downloader_factory = self.video_ffmpeg_detector = None

        def run(self, *args, **kwargs):
            raise AssertionError("CLI must not route through legacy Application.run")

    class FakeRunner:
        def __init__(self, **kwargs):
            seen["runner"] = kwargs

        def run(self, request, cancellation_token=None):
            seen["request"] = request
            return SimpleNamespace(
                status="completed", task_dir=tmp_path / "output" / "new-task"
            )

    monkeypatch.setattr(cli, "Application", FakeApp)
    monkeypatch.setattr(pipeline, "PipelineRunner", FakeRunner)
    monkeypatch.setattr(cli, "DEFAULT_OUTPUT_ROOT", tmp_path / "output")
    assert (
        cli.main(["run", str(tmp_path / "fixture.docx"), "--issue", "71", "--offline"])
        == 0
    )
    request = seen["request"]
    assert request.offline is True
    assert request.output_dir == tmp_path / "output"
    assert request.task_dir is None and request.resume is False


def test_scoped_repository_query_preserves_snapshot_decisions_and_hides_new_rows(
    tmp_path,
):
    from galgame_news.domain import ImageCandidate, SourceType

    repo = TaskRepository(tmp_path)
    repo.activate()
    news_a, news_b = "news-a", "news-b"
    candidate_a = ImageCandidate(
        news_id=news_a,
        image_url="https://fixture.invalid/a.png",
        source_url="https://fixture.invalid/a",
        source_type=SourceType.OFFICIAL_SITE,
        fetched_at=datetime.now(timezone.utc),
        downloadable=True,
    )
    candidate_b = ImageCandidate(
        news_id=news_b,
        image_url="https://fixture.invalid/b.png",
        source_url="https://fixture.invalid/b",
        source_type=SourceType.OFFICIAL_SITE,
        fetched_at=datetime.now(timezone.utc),
        downloadable=True,
    )
    repo.upsert_candidates(
        [candidate_a.model_dump(mode="json"), candidate_b.model_dump(mode="json")]
    )
    repo.save_review_state(
        {
            "schema_version": 1,
            "revision": 3,
            "decisions": {candidate_a.id: "accepted", candidate_b.id: "pending"},
            "manual_decisions": [],
        }
    )
    database_state_before = repo.load_review_state()
    snapshot = ReviewSession(
        tmp_path / "raw",
        task_root=tmp_path,
        state_path=repo.path,
        images=[candidate_a, candidate_b],
        videos=[],
        news_items=[],
        source_paths={},
        raw_types={},
        review_ids=set(),
        failed_ids=set(),
    )
    snapshot._initialize_decisions()

    # A background membership query must keep the open snapshot's unsaved choice.
    snapshot.set_decisions([candidate_a.id], ReviewDecision.REJECTED, persist=False)
    late_candidate = ImageCandidate(
        news_id=news_a,
        image_url="https://fixture.invalid/late.png",
        source_url="https://fixture.invalid/a",
        source_type=SourceType.OFFICIAL_SITE,
        fetched_at=datetime.now(timezone.utc),
        downloadable=True,
    )
    repo.upsert_candidates([late_candidate.model_dump(mode="json")])

    snapshot.scope_repository_query(news_a, {news_a, news_b})
    assert [item.id for item in snapshot.images] == [candidate_a.id]
    assert snapshot.decision(candidate_a.id) is ReviewDecision.REJECTED
    assert candidate_a.id in snapshot._manual_decisions
    assert repo.load_review_state() == database_state_before
    assert late_candidate.id not in {item.id for item in snapshot.images}

    snapshot_b = ReviewSession(
        tmp_path / "raw",
        task_root=tmp_path,
        state_path=repo.path,
        images=[candidate_a, candidate_b],
        videos=[],
        news_items=[],
        source_paths={},
        raw_types={},
        review_ids=set(),
        failed_ids=set(),
    )
    snapshot_b._initialize_decisions()
    snapshot_b.scope_repository_query(news_b, {news_a, news_b})
    assert [item.id for item in snapshot_b.images] == [candidate_b.id]
    empty_snapshot = ReviewSession(
        tmp_path / "raw",
        task_root=tmp_path,
        state_path=repo.path,
        images=[candidate_a, candidate_b],
        videos=[],
        news_items=[],
        source_paths={},
        raw_types={},
        review_ids=set(),
        failed_ids=set(),
    )
    empty_snapshot.scope_repository_query(None, set())
    assert empty_snapshot.images == []


def test_imported_sqlite_task_routes_options_and_retry_batches_to_source_and_lists_stub(tmp_path):
    import json
    from galgame_news.tasks.store import TaskStore

    source_root = tmp_path / "source-task"
    source_root.mkdir()
    (source_root / "raw").mkdir()
    source_repo = TaskRepository(source_root)
    source_repo.activate()
    source_repo.save_batch(
        "news-a", "db-attempt", "retries/news-a/db-attempt",
        "supplement", "completed", [], [], [],
    )
    source_repo.save_batch(
        "news-a", "shared-attempt", "retries/news-a/shared-attempt",
        "supplement", "completed", [], [], [],
    )

    store = TaskStore(tmp_path / "app")
    record = store.import_output(
        source_root / "raw", issue_id="88", task_root=tmp_path / "app" / "import-stub"
    )
    assert store.storage_root(record) == source_root.resolve()

    options = {"offline": True, "max_images": 9}
    store.save_request_options(record, options)
    assert store.load_request_options(record) == options
    assert source_repo.load_options() == options
    assert not (record.root_path / "request_options.json").exists()

    # Legacy retry manifests in the private import stub remain discoverable.
    for attempt_id in ("legacy-attempt", "shared-attempt"):
        attempt = record.root_path / "retries" / "news-a" / attempt_id
        attempt.mkdir(parents=True)
        (attempt / "image_index.json").write_text(json.dumps({
            "kind": "supplement", "news_items": [{"news_id": "news-a"}], "candidates": []
        }), encoding="utf-8")
    routed = store.register_retry_attempt(record, "news-a", "db-attempt")
    assert routed == source_root / "retries" / "news-a" / "db-attempt"

    attempts = store.list_retry_attempts(record)
    keys = [(attempt.news_id, attempt.attempt_id) for attempt in attempts]
    assert keys.count(("news-a", "shared-attempt")) == 1
    assert set(keys) == {
        ("news-a", "db-attempt"),
        ("news-a", "legacy-attempt"),
        ("news-a", "shared-attempt"),
    }
    shared = next(item for item in attempts if item.attempt_id == "shared-attempt")
    assert shared.path == source_root / "retries" / "news-a" / "shared-attempt"


def test_imported_legacy_retry_assets_survive_sqlite_scoped_query(tmp_path):
    import json
    from PIL import Image
    from galgame_news.domain import ImageCandidate, SourceType
    from galgame_news.tasks.store import TaskStore

    source = tmp_path / "source"
    repo = TaskRepository(source)
    repo.activate()
    base = ImageCandidate(news_id="news-a", image_url="https://fixture.invalid/base.png",
                          source_url="https://fixture.invalid/page", source_type=SourceType.OFFICIAL_SITE,
                          fetched_at=datetime.now(timezone.utc))
    repo.upsert_candidates([base.model_dump(mode="json")])
    store = TaskStore(tmp_path / "app")
    record = store.import_output(source, issue_id="1", task_root=tmp_path / "app" / "stub")
    batch = record.root_path / "retries" / "news-a" / "old-batch"
    batch.mkdir(parents=True)
    image = batch / "old.png"
    Image.new("RGB", (900, 600), "red").save(image)
    legacy = ImageCandidate(news_id="news-a", image_url="https://fixture.invalid/old.png",
                            source_url="https://fixture.invalid/page", source_type=SourceType.OFFICIAL_SITE,
                            fetched_at=datetime.now(timezone.utc), local_path=str(image), width=900, height=600)
    (batch / "image_index.json").write_text(json.dumps({"kind": "supplement",
        "news_items": [{"news_id": "news-a"}],
        "candidates": [legacy.model_dump(mode="json")]}), encoding="utf-8")
    session = ReviewSession.from_output(source)
    session.retry_root = source / "retries"
    session.legacy_retry_roots = (record.root_path / "retries",)
    attempt = store.list_retry_attempts(record)[0]
    session.merge_retry_attempt(attempt, force_pending=True, persist=False)
    assert session._source_paths[legacy.id] == image
    session.set_decisions([legacy.id], ReviewDecision.ACCEPTED, persist=False)
    snapshot = session.query_snapshot()
    snapshot.scope_repository_query("news-a", {"news-a"})
    assert legacy.id in {item.id for item in snapshot.images}
    assert snapshot.decision(legacy.id) is ReviewDecision.ACCEPTED
    assert session._confine_retry_asset(record.root_path / "unrelated.png") is None
    other = session.query_snapshot()
    other.scope_repository_query(None, {"news-b"})
    assert other.images == []
