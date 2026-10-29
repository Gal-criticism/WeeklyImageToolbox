"""Reconcile task media files with review candidates and archive indexes.

This module deliberately assigns a file to a news item only when the directory
contains that item's exact stable news id. Unmatched files remain inventory
evidence and are never given guessed provenance.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from .inventory import ScanRoot, load_manifest, save_manifest, scan_inventory, _is_link_or_junction


def task_media_roots(root: Path | str) -> list[ScanRoot]:
    """Return the task's known media roots with their review/export roles."""
    root = Path(root).resolve()
    roots = []
    for relative, role in (("assets/originals", "originals"),
                           ("assets/review", "review"),
                           ("raw/images", "review"),
                           ("final/images", "final")):
        path = root / relative
        if path.is_dir() and not _is_link_or_junction(path):
            roots.append(ScanRoot(path, role))
    retries = root / "retries"
    if retries.is_dir() and not _is_link_or_junction(retries):
        for news in retries.iterdir():
            if not news.is_dir() or _is_link_or_junction(news):
                continue
            for attempt in news.iterdir():
                if not attempt.is_dir() or _is_link_or_junction(attempt):
                    continue
                for name, role in (("images", "originals"), ("videos", "originals"),
                                   ("review", "review"), ("review_assets", "review")):
                    path = attempt / name
                    if path.is_dir() and not _is_link_or_junction(path):
                        roots.append(ScanRoot(path, role))
    return roots


def scan_task_media(root: Path | str, *, cancelled=None, timeout=None, persist_baseline: bool = True):
    """Hash decoded files from known task folders and diff against saved baseline."""
    root = Path(root).resolve()
    manifest_path = root / "media_inventory.json"
    previous = None
    if manifest_path.is_file():
        try:
            previous = load_manifest(manifest_path)
        except (OSError, ValueError, TypeError):
            # A damaged baseline cannot prove removals or moves.
            previous = None
    video_probe = None
    try:
        from .video.downloader import VideoDownloader
        downloader = VideoDownloader()
        probe = downloader.media_probe
        probe_available = downloader._probe_executable() is not None
        if probe is not None and probe_available:
            def video_probe(path, remaining):
                if remaining is not None and remaining < 15:
                    return None
                try:
                    metadata = probe(path)
                    return bool(metadata and metadata.get("width", 0) > 0 and metadata.get("height", 0) > 0)
                except RuntimeError as exc:
                    if "ffprobe unavailable" in str(exc).casefold():
                        return None
                    return False
                except Exception:
                    return False
    except Exception:
        # The inventory remains useful without ffprobe; videos stay unvalidated.
        video_probe = None
    roots = task_media_roots(root)
    result = scan_inventory(roots, previous=previous,
                            cancel=cancelled, timeout=timeout, ffprobe=video_probe)
    if not roots and (previous is not None or not root.is_dir()):
        result.complete = False
        result.errors.append("no configured media roots are available")
    checked_at = datetime.now(timezone.utc).isoformat()
    usable_unique = len({record.sha256 for record in result.manifest.records if record.usable is True})
    status = {"last_checked_utc": checked_at, "complete": result.complete,
              "cancelled": result.cancelled, "errors": result.errors,
              "files": result.stats.files, "physical_unique": result.stats.physical_unique,
              "usable_files": result.stats.usable_files, "usable_unique": usable_unique,
              "bytes": result.stats.bytes,
              "by_role": result.stats.by_role, "usable_by_role": result.stats.usable_by_role,
              "unvalidated_videos": sum(r.kind == "video" and r.usable is None for r in result.manifest.records)}
    from .delivery.helpers import atomic_json_write
    try:
        atomic_json_write(root / "media_inventory_status.json", status, ensure_parent=True)
    except OSError as exc:
        result.errors.append(f"status write failed: {exc}")
        result.complete = False
    if result.complete and persist_baseline and roots:
        save_manifest(manifest_path, result.manifest, complete=True)
    return result


def _news_assignment_map(root: Path) -> dict[str, set[str]]:
    """Map stable IDs and unambiguous canonical/game folder names to news IDs."""
    from .tasks.repository import TaskRepository
    assignments: dict[str, set[str]] = {}
    payloads = []
    if TaskRepository.is_authoritative(root):
        repo = TaskRepository(root, create=False)
        try:
            payloads.extend(repo.export_review_payloads()[:2])
        except (AttributeError, TypeError, ValueError):
            pass
    for path in (root / "raw" / "image_index.json", root / "image_index.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                payloads.append(payload)
        except (OSError, ValueError, UnicodeError):
            continue
    from .delivery.helpers import item_names
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        news = [n for n in payload.get("news_items", []) if isinstance(n, dict)
                and (n.get("news_id") or n.get("id"))]
        aliases = item_names(news)
        for item in news:
            ident = str(item.get("news_id") or item.get("id"))
            keys = {ident, aliases.get(ident, ""), str(item.get("title") or "").strip()}
            games = item.get("game_names") or item.get("games") or item.get("game") or ()
            if isinstance(games, str):
                games = (games,)
            keys.update(str(g).strip() for g in games if str(g).strip())
            for key in keys - {""}:
                assignments.setdefault(key.casefold(), set()).add(ident)
    return assignments


def _legacy_media_payloads(root: Path) -> list[dict]:
    payloads = []
    for path in (root / "raw" / "image_index.json", root / "image_index.json"):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                payloads.append(value)
        except (OSError, ValueError, UnicodeError):
            continue
    return payloads


def reconcile_task(root: Path | str, phase: str = "review", cancelled=None, *, timeout=None):
    """Scan task media and import safely assigned manual images as pending.

    Returns a compact report with scan evidence. Partial scans do not update the
    saved baseline and do not reconcile removals.
    """
    from .domain import ImageCandidate, SourceType
    from .tasks.repository import TaskRepository

    root = Path(root).resolve()
    result = scan_task_media(root, cancelled=cancelled, timeout=timeout, persist_baseline=False)
    report = {"phase": phase, "complete": result.complete, "cancelled": result.cancelled,
              "last_checked_utc": datetime.now(timezone.utc).isoformat(),
              "files": result.stats.files, "physical_unique": result.stats.physical_unique,
              "usable_files": result.stats.usable_files,
              "usable_unique": len({r.sha256 for r in result.manifest.records if r.usable is True}),
              "bytes": result.stats.bytes,
              "by_role": result.stats.by_role, "usable_by_role": result.stats.usable_by_role,
              "unvalidated_videos": sum(r.kind == "video" and r.usable is None for r in result.manifest.records),
              "errors": result.errors,
              "changes": {"added": result.changes.added, "removed": result.changes.removed,
                          "moved": result.changes.moved, "modified": result.changes.modified,
                          "ambiguous": result.changes.ambiguous},
              "imported": 0, "unassigned": 0}
    if not result.complete or not task_media_roots(root):
        from .delivery.helpers import atomic_json_write
        try:
            atomic_json_write(root / "media_sync_report.json", report, ensure_parent=True)
        except OSError as exc:
            report["errors"].append(f"report write failed: {exc}")
        return report
    assignments = _news_assignment_map(root)
    if not TaskRepository.is_authoritative(root):
        payloads = _legacy_media_payloads(root)
        news_by_id = {}
        known_paths, known_hashes = set(), set()
        for payload in payloads:
            for news in payload.get("news_items", []):
                if isinstance(news, dict) and (news.get("news_id") or news.get("id")):
                    ident = str(news.get("news_id") or news.get("id"))
                    news_by_id[ident] = {**news, "news_id": ident}
            for field in ("candidates", "images", "image_candidates"):
                for item in payload.get(field, []) if isinstance(payload.get(field), list) else []:
                    if not isinstance(item, dict):
                        continue
                    value = item.get("local_path") or item.get("path") or item.get("file")
                    if value:
                        try:
                            known_paths.add(str((root / value).resolve() if not Path(value).is_absolute() else Path(value).resolve()).casefold())
                        except OSError:
                            pass
                    known_hashes.update(str(item[key]).casefold() for key in ("sha256", "output_sha256") if item.get(key))
        overlay_path = root / "media_sync_overlay.json"
        try:
            overlay = json.loads(overlay_path.read_text(encoding="utf-8"))
            overlay_rows = list(overlay.get("candidates", []))
        except (OSError, ValueError, UnicodeError, TypeError):
            overlay_rows = []
        existing = {(str(row.get("news_id")), str(row.get("signals", {}).get("media_sync_sha256"))): row
                    for row in overlay_rows if isinstance(row, dict)}
        present = set()
        for record in result.manifest.records:
            if record.kind != "image" or record.usable is not True:
                continue
            path = Path(record.path)
            path_key = str(path.resolve()).casefold()
            if path_key in known_paths or record.sha256.casefold() in known_hashes:
                continue
            matches = set()
            for part in path.parts:
                matches.update(assignments.get(part.casefold(), ()))
            if len(matches) != 1:
                report["unassigned"] += 1
                continue
            assigned = next(iter(matches))
            digest = record.sha256.casefold()
            present.add((assigned, digest))
            prior = existing.get((assigned, digest))
            if prior:
                prior.update(local_path=str(path), original_path=str(path), sha256=record.sha256,
                             download_status="downloaded")
                prior.setdefault("signals", {})["media_sync_available"] = True
                continue
            from .domain import ImageCandidate, SourceType
            overlay_rows.append(ImageCandidate(
                news_id=assigned, image_url="local-media://sha256/" + record.sha256,
                source_url="local-media://sha256/" + record.sha256, source_type=SourceType.UNKNOWN,
                fetched_at=datetime.now(timezone.utc), local_path=str(path), original_path=str(path),
                download_status="downloaded", sha256=record.sha256, byte_size=record.size,
                downloadable=False, selected=False,
                signals={"media_sync": True, "media_sync_sha256": record.sha256,
                         "media_sync_available": True, "provenance": "manual_unverified"},
            ).model_dump(mode="json"))
            report["imported"] += 1
        for row in overlay_rows:
            signals = row.get("signals", {}) if isinstance(row, dict) else {}
            digest = str(signals.get("media_sync_sha256", "")).casefold()
            if digest and (str(row.get("news_id")), digest) not in present:
                signals["media_sync_available"] = False
        overlay_payload = {"schema_version": 1,
            "issue_id": next((p.get("issue_id") or p.get("issue_number") for p in payloads if p.get("issue_id") or p.get("issue_number")), ""),
            "news_items": list(news_by_id.values()), "candidates": overlay_rows}
        from .delivery.helpers import atomic_json_write
        atomic_json_write(overlay_path, overlay_payload, ensure_parent=True)
        save_manifest(root / "media_inventory.json", result.manifest, complete=True)
        return report
    repo = TaskRepository(root, create=False)
    candidates = list(repo.iter_candidates(kind="image"))
    manual_imported = 0
    path_index = {}
    digest_index = {}
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        for field in ("local_path", "original_path", "output_path"):
            value = candidate.get(field)
            if value:
                try:
                    path_index.setdefault(str(Path(value).resolve()).casefold(), []).append((candidate, field))
                except OSError:
                    continue
        for field in ("sha256", "original_sha256", "output_sha256"):
            digest = candidate.get(field)
            if digest:
                digest_index.setdefault(str(digest).casefold(), []).append((candidate, field))
    final_managed: dict[str, str] = {}
    try:
        final_payload = json.loads((root / "final" / "final_manifest.json").read_text(encoding="utf-8"))
        for relative in final_payload.get("files", []):
            digest = final_payload.get("file_sha256", {}).get(relative)
            target = (root / "final" / Path(*Path(relative).parts)).resolve()
            final_managed[str(target).casefold()] = str(digest or "").casefold()
    except (OSError, ValueError, UnicodeError, TypeError):
        pass
    records = [r for r in result.manifest.records if r.kind == "image" and r.usable is True]
    seen = set()
    rows = []
    changed_review_ids = set()
    updated_candidate_ids = set()
    updates = {}
    changed_original_ids = set()
    now = datetime.now(timezone.utc)
    available_hashes: dict[str, set[str]] = {}
    for record in records:
        path = Path(record.path)
        path_key = str(path.resolve()).casefold()
        owned = path_index.get(path_key, []) or digest_index.get(record.sha256.casefold(), [])
        if path_key in final_managed and final_managed[path_key] == record.sha256.casefold():
            continue
        matches = set()
        for part in path.parts:
            matches.update(assignments.get(part.casefold(), ()))
        matches.update(str(item.get("news_id")) for item, _field in owned if item.get("news_id"))
        if len(matches) != 1:
            report["unassigned"] += 1
            continue
        assigned = next(iter(matches))
        available_hashes.setdefault(assigned, set()).add(record.sha256)
        if owned:
            touched = set()
            for candidate, field in owned:
                candidate_id = str(candidate.get("id"))
                if candidate_id in changed_original_ids and field not in {"original_path", "original_sha256"}:
                    continue
                updated = updates.get(candidate_id, dict(candidate))
                touched.add(candidate_id)
                updated_candidate_ids.add(candidate_id)
                path_owned = bool(path_index.get(path_key))
                expected_field = {"local_path": "output_sha256", "original_path": "original_sha256",
                                  "output_path": "output_sha256"}.get(field)
                prior_digest = candidate.get(expected_field or field)
                if not prior_digest:
                    prior_digest = candidate.get("sha256")
                content_changed = path_owned and prior_digest is not None and str(prior_digest).casefold() != record.sha256.casefold()
                if content_changed:
                    changed_review_ids.add(candidate_id)
                if field in {"original_path", "original_sha256"}:
                    updated["original_path"] = str(path)
                    updated["original_sha256"] = record.sha256
                    updated["original_byte_size"] = record.size
                    if content_changed:
                        changed_original_ids.add(candidate_id)
                        # A converted review derivative no longer represents the
                        # replaced original and must not remain exportable.
                        updated["local_path"] = str(path)
                        updated["sha256"] = record.sha256
                        updated["output_sha256"] = None
                        updated["output_byte_size"] = None
                        updated["output_mime_type"] = None
                elif field in {"output_path", "output_sha256"}:
                    updated["local_path"] = str(path)
                    updated["output_sha256"] = record.sha256
                    updated["output_byte_size"] = record.size
                elif field == "local_path" and candidate.get("output_sha256"):
                    updated["local_path"] = str(path)
                    updated["output_sha256"] = record.sha256
                    updated["output_byte_size"] = record.size
                else:
                    updated["local_path"] = str(path)
                    updated["sha256"] = record.sha256
                    updated["byte_size"] = record.size
                updated_signals = dict(updated.get("signals") or {})
                updated_signals["media_sync_available"] = True
                if updated_signals.get("media_sync"):
                    updated_signals["media_sync_sha256"] = record.sha256
                updated["signals"] = updated_signals
                updates[candidate_id] = updated
            rows.extend(updates[key] for key in touched)
            continue
        dedupe_key = (assigned, record.sha256)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        file_url = "local-media://sha256/" + record.sha256
        prior = next((c for c in repo.iter_candidates(news_id=assigned)
                      if isinstance(c, dict) and c.get("signals", {}).get("media_sync_sha256") == record.sha256), None)
        if prior:
            prior["local_path"] = str(path)
            prior["downloaded_url"] = file_url
            prior["download_status"] = "downloaded"
            prior["sha256"] = record.sha256
            prior.setdefault("signals", {})["media_sync_available"] = True
            rows.append(prior)
            continue
        rows.append(ImageCandidate(
            news_id=assigned, image_url=file_url, source_url=file_url,
            source_type=SourceType.UNKNOWN, fetched_at=now, local_path=str(path),
            original_path=str(path), download_status="downloaded", sha256=record.sha256,
            byte_size=record.size, downloadable=False, selected=False,
            signals={"media_sync": True, "media_sync_sha256": record.sha256,
                     "media_sync_available": True, "provenance": "manual_unverified"},
        ).model_dump(mode="json"))
        manual_imported += 1
    # Mark removed/replaced manual files unavailable while retaining the
    # candidate row and its review decision as history.
    present_paths = {str(Path(r.path).resolve()).casefold() for r in records}
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        signals = candidate.get("signals", {})
        digest = signals.get("media_sync_sha256") if isinstance(signals, dict) else None
        if str(candidate.get("id")) in updated_candidate_ids:
            continue
        owned_paths = [candidate.get(name) for name in ("local_path", "original_path", "output_path") if candidate.get(name)]
        if not digest and not owned_paths:
            continue
        present = (digest in available_hashes.get(str(candidate.get("news_id")), set())) if digest else any(
            str(Path(value).resolve()).casefold() in present_paths for value in owned_paths)
        if bool(signals.get("media_sync_available", True)) != present:
            changed = dict(candidate)
            changed_signals = dict(signals)
            changed_signals["media_sync_available"] = present
            changed["signals"] = changed_signals
            rows.append(changed)
    if rows:
        merged_rows = {}
        for row in rows:
            key = str(row.get("id") or (row.get("news_id"), row.get("signals", {}).get("media_sync_sha256")))
            previous_row = merged_rows.get(key, {})
            merged = {**previous_row, **row}
            merged["signals"] = {**previous_row.get("signals", {}), **row.get("signals", {})}
            merged_rows[key] = merged
        repo.upsert_candidates(list(merged_rows.values()), kind="image")
    if changed_review_ids:
        review_state = repo.load_review_state() or {}
        decisions = dict(review_state.get("decisions", {}))
        manual = set(map(str, review_state.get("manual_decisions", [])))
        for candidate_id in changed_review_ids:
            if candidate_id:
                decisions[candidate_id] = "pending"
                manual.discard(candidate_id)
        review_state.update(revision=int(review_state.get("revision", 0)) + 1,
                            decisions=decisions, manual_decisions=sorted(manual))
        repo.save_review_state(review_state)
    report["review_reset"] = sorted(x for x in changed_review_ids if x)
    # Persist a separate report on every completed reconciliation for UI and
    # audit consumers; partial reports never replace the inventory baseline.
    from .delivery.helpers import atomic_json_write
    try:
        atomic_json_write(root / "media_sync_report.json", report, ensure_parent=True)
    except OSError as exc:
        report["errors"].append(f"report write failed: {exc}")
        report["complete"] = False
        return report
    # Persist the full-scan baseline only after the task repository accepted the
    # candidate refresh; failed syncs must remain diffable on the next pass.
    if task_media_roots(root):
        save_manifest(root / "media_inventory.json", result.manifest, complete=True)
    report["imported"] = manual_imported
    return report
