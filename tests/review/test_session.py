from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from galgame_news.domain import ImageCandidate, ImageType, SourceType
from galgame_news.review.session import ReviewDecision, ReviewSession


def _jpeg_bytes(color=(80, 120, 160)):
    stream = BytesIO()
    Image.new("RGB", (40, 30), color).save(stream, format="JPEG")
    return stream.getvalue()


def test_new_evidence_and_media_state_survive_defensive_loading(tmp_path):
    data = _candidate(tmp_path,"evidence")
    evidence = {"page_url":data["source_url"],"container":"section#gallery","item_id":"1","role":"background_art","text":"背景CG","method":"dom","relationship":"srcset","variant_of":None}
    data.update(evidence=[evidence, {"page_url":None}], download_status="downloaded", downloaded_url=data["image_url"], media_source_url=data["image_url"], expected_width=1280, expected_height=720)
    candidate, _ = ReviewSession._coerce_image(data)
    assert candidate is not None
    assert len(candidate.evidence) == 1
    restored = candidate.evidence[0].model_dump()
    assert {key: restored[key] for key in evidence} == evidence
    assert candidate.evidence[0].source_chain == []
    assert candidate.evidence[0].product_title == ""
    assert candidate.download_status == "downloaded"
    assert candidate.media_source_url == data["image_url"]


def _candidate(tmp_path, name, *, selected=False, image_type=ImageType.GAME_CG, relevance=0.5, review=False):
    source = tmp_path / f"{name}.jpg"
    source.write_bytes(_jpeg_bytes())
    payload = {
        "id": name,
        "news_id": "n1",
        "image_url": f"https://cdn.example/{name}.jpg",
        "source_url": "https://official.example/game",
        "source_type": SourceType.OFFICIAL_SITE.value,
        "image_type": image_type.value,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "width": 1280,
        "height": 720,
        "mime_type": "image/jpeg",
        "downloadable": True,
        "local_path": str(source),
        "selected": selected,
        "score": {
            "relevance": relevance,
            "freshness": 0.5,
            "source_trust": 0.8,
            "quality": 0.9,
            "total": relevance * 100,
        },
        "review_reasons": ["uncertain_match"] if review else [],
    }
    return payload


def _write_output(tmp_path, candidates, *, review_required=None):
    output = tmp_path / "output"
    output.mkdir()
    (output / "image_index.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "issue_id": "1",
                "news_items": [{"news_id": "n1", "sequence": 1, "section": "新作", "title": "Game", "status": "selected", "candidates": [item.get("id") for item in candidates]}],
                "candidates": candidates,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (output / "review_required.json").write_text(json.dumps(review_required or []), encoding="utf-8")
    (output / "video_index.json").write_text(json.dumps({"schema_version": 1, "issue_id": "1", "news_items": [], "videos": []}), encoding="utf-8")
    return output


def test_session_initializes_decisions_and_keeps_semantic_images_reviewable(tmp_path):
    accepted = _candidate(tmp_path, "accepted", selected=True, relevance=0.2)
    low = _candidate(tmp_path, "low", relevance=0.4, review=True)
    high = _candidate(tmp_path, "high", relevance=0.9, review=True)
    logo = _candidate(tmp_path, "logo", image_type=ImageType.LOGO, relevance=1.0, review=True)
    output = _write_output(tmp_path, [accepted, low, high, logo], review_required=[low, high, logo])

    session = ReviewSession.from_output(output, state_path=tmp_path / "review_state.json")

    assert session.decision("accepted") is ReviewDecision.ACCEPTED
    assert session.decision("high") is ReviewDecision.PENDING
    assert session.decision("logo") is ReviewDecision.PENDING
    assert [item.id for item in session.pending_images("n1")] == ["logo", "high", "low"]


def test_review_state_is_atomic_and_survives_reopen(tmp_path):
    candidate = _candidate(tmp_path, "pending", review=True)
    output = _write_output(tmp_path, [candidate], review_required=[candidate])
    state = tmp_path / "state" / "review_state.json"
    session = ReviewSession.from_output(output, state_path=state)

    session.set_decision("pending", ReviewDecision.ACCEPTED)
    assert state.is_file()
    assert not list(state.parent.glob("*.tmp"))
    reopened = ReviewSession.from_output(output, state_path=state)
    assert reopened.decision("pending") is ReviewDecision.ACCEPTED


def test_unadopted_supplement_is_hidden_unexported_and_adoption_persists(tmp_path):
    candidate = _candidate(tmp_path, "supplement", review=True)
    candidate["signals"] = {"supplement_module": True, "supplement_adopted": False,
                            "supplement_source": "official", "supplement_relation": "title match"}
    output = _write_output(tmp_path, [candidate], review_required=[candidate])
    state = tmp_path / "review_state.json"
    task_root = tmp_path / "task"
    session = ReviewSession.from_output(output, task_root=task_root, state_path=state)
    assert session.supplement_candidates("n1")
    assert session.pending_images() == []
    session.export_final(include_pending=True)
    assert not list((task_root / "final" / "images").rglob("*.png"))

    session.adopt_supplement_candidates(["supplement"])
    assert [str(item.id) for item in session.pending_images()] == ["supplement"]
    session.set_supplement_classification("supplement", ImageType.BACKGROUND_ART)
    manifest = session.export_final(include_pending=True)
    provenance = manifest["supplement_assets"][0]
    assert provenance["candidate_id"] == "supplement"
    assert provenance["supplement_relation"] == "title match"
    assert provenance["source_url"] == candidate["source_url"]
    assert provenance["supplement_relation"] == "title match"
    assert provenance["review_classification"] == "background_art"
    reopened = ReviewSession.from_output(output, task_root=task_root, state_path=state)
    assert reopened.supplement_candidates() == []
    assert [str(item.id) for item in reopened.pending_images()] == ["supplement"]
    assert reopened._image_by_id["supplement"].image_type is ImageType.BACKGROUND_ART


def test_json_supplement_directory_can_be_merged_for_review(tmp_path):
    base_candidate = _candidate(tmp_path, "auto-image", review=True)
    output = _write_output(tmp_path, [])
    task_root = tmp_path / "task"
    supplement_dir = task_root / "supplement" / "n1"
    image_dir = supplement_dir / "images"
    image_dir.mkdir(parents=True)
    image_path = image_dir / "auto-image.jpg"
    image_path.write_bytes(_jpeg_bytes())
    base_candidate.update(local_path="images/auto-image.jpg", selected=False,
                          signals={"supplement_module": True, "supplement_adopted": False})
    (supplement_dir / "image_index.json").write_text(json.dumps({
        "kind": "supplement", "status": "completed", "candidates": [base_candidate], "failures": [],
    }), encoding="utf-8")
    session = ReviewSession.from_output(output, task_root=task_root,
                                        state_path=task_root / "review_state.json")

    session.merge_supplement_directory(supplement_dir, "n1", persist=False)

    assert [str(candidate.id) for candidate in session.supplement_candidates()] == ["auto-image"]
    assert session.pending_images() == []


def test_export_final_uses_x_prefix_and_does_not_modify_raw(tmp_path):
    candidate = _candidate(tmp_path, "accepted", selected=True)
    output = _write_output(tmp_path, [candidate])
    raw_before = json.loads((output / "image_index.json").read_text(encoding="utf-8"))
    task_root = tmp_path / "task"
    session = ReviewSession.from_output(output, task_root=task_root)

    manifest = session.export_final()

    target = task_root / "final" / "images" / "x1" / "x1.01.png"
    with Image.open(target) as image:
        assert image.format == "PNG"
    assert manifest["files"] == ["images/x1/x1.01.png"]
    assert json.loads((output / "image_index.json").read_text(encoding="utf-8")) == raw_before


def test_export_final_reuses_category_aliases_and_manifest_appearance_order(tmp_path):
    output = tmp_path / "output"
    output.mkdir()
    items = [
        {"news_id": "h-first", "sequence": 8, "section": "漢化情報", "title": "H first", "status": "selected", "candidates": ["h-first-image"]},
        {"news_id": "x-first", "sequence": 2, "section": "新作", "title": "X first", "status": "selected", "candidates": ["x-first-image"]},
        {"news_id": "h-second", "sequence": 3, "section": "漢化", "title": "H second", "status": "selected", "candidates": ["h-second-image"]},
        {"news_id": "z-first", "sequence": 1, "section": "周邊", "title": "Z first", "status": "selected", "candidates": ["z-first-image"]},
        {"news_id": "z-second", "sequence": 4, "section": "未知栏目", "title": "Z second", "status": "selected", "candidates": ["z-second-image"]},
    ]
    candidates = []
    for item in items:
        name = f"{item['news_id']}-image"
        source = output / f"{name}.jpg"
        source.write_bytes(_jpeg_bytes())
        payload = _candidate(tmp_path, name, selected=True)
        payload["news_id"] = item["news_id"]
        payload["local_path"] = str(source)
        candidates.append(payload)
    (output / "image_index.json").write_text(
        json.dumps({"schema_version": 1, "issue_id": "1", "news_items": items, "candidates": candidates}, ensure_ascii=False),
        encoding="utf-8",
    )
    (output / "review_required.json").write_text("[]", encoding="utf-8")
    (output / "video_index.json").write_text(
        json.dumps({"schema_version": 1, "issue_id": "1", "news_items": [], "videos": []}),
        encoding="utf-8",
    )

    task_root = tmp_path / "task"
    session = ReviewSession.from_output(output, task_root=task_root)
    manifest = session.export_final()

    assert set(manifest["files"]) == {
        "images/h1/h1.01.png",
        "images/x1/x1.01.png",
        "images/h2/h2.01.png",
        "images/z1/z1.01.png",
        "images/z2/z2.01.png",
    }


def test_duplicate_candidates_are_kept_once(tmp_path):
    item = _candidate(tmp_path, "same", selected=True)
    output = _write_output(tmp_path, [item, item])
    session = ReviewSession.from_output(output, state_path=tmp_path / "state.json")
    assert len(session.images) == 1


def test_video_decisions_export_sanitized_titles_and_avoid_collisions(tmp_path):
    output = tmp_path / "output"
    output.mkdir()
    first = output / "first.mp4"
    second = output / "second.mp4"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    payload = {
        "schema_version": 1,
        "issue_id": "1",
        "news_items": [{"news_id": "n1", "sequence": 1, "section": "新作", "title": "Game", "videos": ["v1", "v2"]}],
        "videos": [
            {
                "id": "v1",
                "news_id": "n1",
                "source_url": "https://example.test/news",
                "video_url": "https://cdn.example/1.mp4",
                "title": 'bad:/title*?"<>|',
                "local_path": str(first),
                "downloadable": True,
                "status": "downloaded",
            },
            {
                "id": "v2",
                "news_id": "n1",
                "source_url": "https://example.test/news",
                "video_url": "https://cdn.example/2.mp4",
                "title": 'bad:/title*?"<>|',
                "local_path": str(second),
                "downloadable": True,
                "status": "downloaded",
            },
        ],
    }
    (output / "image_index.json").write_text(json.dumps({"schema_version": 1, "issue_id": "1", "news_items": [], "candidates": []}), encoding="utf-8")
    video_index = output / "video_index.json"
    video_index.write_text(json.dumps(payload), encoding="utf-8")
    before = video_index.read_bytes()

    task_root = tmp_path / "task"
    session = ReviewSession.from_output(output, task_root=task_root)
    assert session.decision("v1") is ReviewDecision.ACCEPTED
    assert session.decision("v2") is ReviewDecision.ACCEPTED

    manifest = session.export_final()

    files = sorted((task_root / "final" / "images" / "x1").iterdir())
    assert {path.name for path in files} == {"badtitle.mp4", "badtitle (2).mp4"}
    assert {path.read_bytes() for path in files} == {b"first", b"second"}
    assert set(manifest["files"]) == {"images/x1/badtitle (2).mp4", "images/x1/badtitle.mp4"}
    assert video_index.read_bytes() == before


def test_old_json_is_loaded_defensively_and_deduplicated_by_stable_id(tmp_path):
    output = tmp_path / "legacy"
    output.mkdir()
    source = output / "legacy.jpg"
    source.write_bytes(b"legacy")
    old = {
        "images": [
            {
                "id": "legacy-id",
                "news_id": "n1",
                "image_url": "https://cdn.example/legacy.jpg",
                "source_url": "https://example.test/news",
                "image_type": "game_cg",
                "downloadable": True,
                "local_path": str(source),
                "selected": False,
                "score": {"relevance": 0.8, "total": 80},
            },
            {
                "id": "legacy-id",
                "news_id": "n1",
                "image_url": "https://cdn.example/legacy.jpg",
                "source_url": "https://example.test/news",
                "image_type": "game_cg",
                "downloadable": True,
                "local_path": str(source),
                "selected": False,
            },
            {"id": "broken"},
        ]
    }
    (output / "image_index.json").write_text(json.dumps(old), encoding="utf-8")
    (output / "video_index.json").write_text("not-json", encoding="utf-8")
    # Critical corrupt indexes are no longer silently treated as empty.
    with pytest.raises(ValueError):
        ReviewSession.from_output(output, state_path=tmp_path / "state.json")
    assert (output / "video_index.json").read_text(encoding="utf-8") == "not-json"
    (output / "video_index.json").write_text('{"videos": []}', encoding="utf-8")
    session = ReviewSession.from_output(output, state_path=tmp_path / "state.json")

    assert [item.id for item in session.images] == ["legacy-id"]
    assert session.videos == []
    assert session.decision("legacy-id") is ReviewDecision.PENDING


def test_legacy_same_url_across_news_gets_independent_review_identity(tmp_path):
    first = _candidate(tmp_path, "first", review=True)
    second = _candidate(tmp_path, "second", review=True)
    for item in (first, second):
        item["id"] = "legacy-url-only-id"
        item["image_url"] = "https://cdn.example/shared.jpg"
    second["news_id"] = "n2"
    output = _write_output(tmp_path, [first, second], review_required=[first, second])
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({"decisions": {"legacy-url-only-id": "accepted"}}), encoding="utf-8")

    session = ReviewSession.from_output(output, state_path=state_path)

    assert len(session.images) == 2
    first_id, second_id = [str(value.id) for value in session.images]
    assert first_id != second_id
    # The old URL-only decision was ambiguous, so it is not applied to either item.
    assert session.decision(first_id) is ReviewDecision.PENDING
    assert session.decision(second_id) is ReviewDecision.PENDING
    session.set_decision(first_id, ReviewDecision.ACCEPTED)
    assert session.decision(second_id) is ReviewDecision.PENDING


def test_retry_merge_adds_new_candidates_without_overwriting_raw_or_duplicates(tmp_path):
    old = _candidate(tmp_path, "old", selected=True)
    output = _write_output(tmp_path, [old])
    retry = tmp_path / "task" / "retries" / "n1" / "attempt-1"
    retry.mkdir(parents=True)
    new = _candidate(tmp_path, "new", review=True)
    retry_source = retry / "new.jpg"
    retry_source.write_bytes(Path(new["local_path"]).read_bytes())
    new["local_path"] = str(retry_source)
    (retry / "image_index.json").write_text(
        json.dumps({"schema_version": 1, "news_items": [], "candidates": [old, new]}),
        encoding="utf-8",
    )
    (retry / "video_index.json").write_text(json.dumps({"videos": []}), encoding="utf-8")
    raw_before = (output / "image_index.json").read_bytes()

    session = ReviewSession.from_output(output, task_root=tmp_path / "task")
    merged = session.merge_retry_attempt("n1", "attempt-1")

    assert merged.added_image_ids == ["new"]
    assert [item.id for item in session.images] == ["old", "new"]
    assert session.decision("new") is ReviewDecision.PENDING
    assert (output / "image_index.json").read_bytes() == raw_before


def test_retry_merge_rejects_parent_absolute_and_symlink_escape(tmp_path):
    output = _write_output(tmp_path, [])
    task_root = tmp_path / "task"
    retries = task_root / "retries" / "n1"
    retries.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "image_index.json").write_text(json.dumps({"candidates": []}), encoding="utf-8")
    session = ReviewSession.from_output(output, task_root=task_root)

    with pytest.raises(ValueError, match="retry"):
        session.merge_retry_attempt("n1", "../outside")
    with pytest.raises(ValueError, match="retry"):
        session.merge_retry_attempt(str(outside))

    link = retries / "symlink"
    try:
        os.symlink(outside, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("directory symlinks are unavailable on this Windows host")
    with pytest.raises(ValueError, match="retry"):
        session.merge_retry_attempt("n1", "symlink")
