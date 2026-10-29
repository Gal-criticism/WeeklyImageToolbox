"""Verify archive identity against saved evidence and bounded live directories."""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import sys
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree as ET

from stability_acceptance import ACCEPTANCE, ROOT, write_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    options = parser.parse_args()
    sys.path.insert(0, str(ROOT / "src"))
    from galgame_news.archive import ArchiveAsset, ArchiveIndex, NextcloudArchiveProvider

    output = ACCEPTANCE / "archive"
    output.mkdir(parents=True, exist_ok=True)
    evidence = ROOT / "output" / "two-game-archive-check-20261027" / "records"
    saved = [ArchiveAsset.from_dict(row) for row in json.loads(
        (evidence / "live-assets.json").read_text(encoding="utf-8"))]
    index = ArchiveIndex(output / "sample-index.sqlite3")
    index.replace_source("nextcloud", saved)
    queries = ("AMBITIOUS MISSION", "Sugar Lies")
    results = {}
    for term in queries:
        assets = index.search([term], current_issue="264", days=None)
        results[term] = [asset.to_dict() for asset in assets]
    index.close()
    report = {
        "saved_evidence": str(evidence), "saved_assets": len(saved),
        "offline_matches": results, "live_requested": options.live,
        "boundary": "Saved metadata is not a current live result or a visual CG audit.",
    }
    if options.live:
        import httpx
        from galgame_news.settings.model import SettingsStore
        from galgame_news.settings.credentials import CredentialStore

        settings = SettingsStore().load().archive
        password = CredentialStore().get("archive_app_password")
        if not password:
            report["live_status"] = "credential_unavailable"
        else:
            provider = NextcloudArchiveProvider(
                settings.server_url, settings.username, password, settings.root_path,
                transport=httpx.Client(auth=(settings.username, password), timeout=15, follow_redirects=False),
                cache_dir=output / "webdav-cache",
            )
            try:
                connected = provider.test_connection()
                report["authenticated"] = connected
                if not connected:
                    report["live_status"] = "authentication_failed"
                else:
                    response = provider._request(
                        "PROPFIND", provider._url_for(provider._dav_root_path),
                        headers={"Depth": "1", "Content-Type": "application/xml"},
                        content=provider._PROPS,
                    )
                    response.raise_for_status()
                    xml = ET.fromstring(response.content)
                    periods = []
                    for item in xml.findall("{DAV:}response"):
                        prop = item.find("{DAV:}propstat/{DAV:}prop")
                        if prop is None or prop.find("{DAV:}resourcetype/{DAV:}collection") is None:
                            continue
                        path = unquote(urlsplit(item.findtext("{DAV:}href") or "").path)
                        periods.append(path.rstrip("/").rsplit("/", 1)[-1])
                    report["live_periods"] = periods
                    report["period_261_present"] = any(value in {"261", "\u7b2c261\u671f", "261\u671f"} for value in periods)
                    base = "/remote.php/dav/files/" + provider.canonical_user_id
                    directories = sorted({unquote(asset.path).rsplit("/", 1)[0] for asset in saved
                                          if asset.game_names == ("AMBITIOUS MISSION",)})
                    live_assets = []
                    directory_results = []
                    for directory in directories:
                        if not directory.startswith(base + "/"):
                            continue
                        child = NextcloudArchiveProvider(
                            settings.server_url, provider.canonical_user_id, password, directory[len(base):],
                            transport=provider.transport, cache_dir=output / "webdav-cache",
                        )
                        assets = child.scan()
                        live_assets.extend(assets)
                        directory_results.append({
                            "path": directory, "assets": len(assets),
                            "kinds": dict(Counter(asset.kind for asset in assets)),
                        })
                    live_index = ArchiveIndex(output / "live-index.sqlite3")
                    live_index.replace_source("nextcloud", live_assets)
                    report["live_matches"] = {
                        term: [asset.to_dict() for asset in live_index.search([term], "264", days=None)]
                        for term in queries
                    }
                    live_index.close()
                    report["live_directories"] = directory_results
                    report["live_status"] = "completed"
                    report["live_scope"] = "Root period listing and previously confirmed matching directories only."
            except Exception as exc:
                # Transport/auth exception representations may contain sensitive URLs.
                report["live_status"] = "failed"
                report["live_error_type"] = type(exc).__name__
            finally:
                provider.transport.close()
    write_json(output / "archive-acceptance.json", report)
    print(json.dumps({
        "offline_match_counts": {term: len(values) for term, values in results.items()},
        "live_status": report.get("live_status", "not_requested"),
        "live_match_counts": {term: len(values) for term, values in report.get("live_matches", {}).items()},
        "report": str(output / "archive-acceptance.json"),
    }, ensure_ascii=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
