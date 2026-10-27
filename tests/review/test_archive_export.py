import json
from PIL import Image

from galgame_news.archive.materialize import ArchiveMaterializer
from galgame_news.archive.models import ArchiveAsset
from galgame_news.archive.providers import LocalArchiveProvider
from galgame_news.review import ReviewSession


def test_only_adopted_archive_asset_is_exported_with_provenance(tmp_path):
    raw = tmp_path / "task" / "raw"
    raw.mkdir(parents=True)
    payload = {"issue_id": "231", "news_items": [{"news_id": "n1", "title": "Game", "section": "新作", "sequence": 1}], "candidates": []}
    (raw / "image_index.json").write_text(json.dumps(payload), encoding="utf-8")
    (raw / "video_index.json").write_text('{"videos":[]}', encoding="utf-8")
    source = tmp_path / "history" / "a.png"
    source.parent.mkdir()
    Image.new("RGB", (1280, 720), "blue").save(source)
    asset = ArchiveAsset("a", "local", "image", "230", str(source), source.name, game_names=("Game",), local_path=str(source))
    session = ReviewSession.from_output(raw, task_root=raw.parent)
    # An archive query snapshot is not an ordinary pending candidate.
    (raw.parent / "archive").mkdir()
    (raw.parent / "archive" / "hits.json").write_text(json.dumps(asset.to_dict()), encoding="utf-8")
    assert session.export_final(include_pending=True)["image_count"] == 0
    materializer = ArchiveMaterializer(tmp_path / "cache", lambda _: LocalArchiveProvider([source.parent]))
    candidate = materializer.prepare(asset, news_id="n1", task_root=raw.parent)
    attempt_id = candidate.archive_provenance["attempt_id"]
    delta = session.prepare_retry_delta("n1", attempt_id)
    session.apply_retry_delta(delta)
    manifest = session.export_final(include_pending=True)
    assert manifest["image_count"] == 1
    assert manifest["archive_assets"][0]["archive_provenance"]["issue_number"] == "230"
    assert manifest["archive_assets"][0]["file"] in manifest["files"]
