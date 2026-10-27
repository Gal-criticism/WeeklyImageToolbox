from pathlib import Path

import pytest

from galgame_news.config import load_config
from galgame_news.pipeline.checkpoint import config_hash
from galgame_news.pipeline.contracts import TaskRequest


def test_old_positional_request_config_argument_keeps_its_position():
    request = TaskRequest("231.docx", "231", "output", False, True, False, "config.yml")
    assert request.config_path == Path("config.yml")
    assert request.use_archive_materials is False
    assert request.archive_range == "year"


def test_archive_range_changes_enabled_checkpoint_identity_only():
    config = load_config()
    common = {"input_path": "231.docx", "issue_id": "231", "output_dir": "output"}
    enabled_year = TaskRequest(**common, use_archive_materials=True)
    enabled_all = TaskRequest(**common, use_archive_materials=True, archive_range="all")
    assert config_hash(config, enabled_year) != config_hash(config, enabled_all)
    assert config_hash(config, TaskRequest(**common)) == config_hash(
        config, TaskRequest(**common, archive_range="all")
    )


def test_unknown_archive_range_is_rejected():
    with pytest.raises(ValueError, match="往期周报范围"):
        TaskRequest("231.docx", "231", "output", archive_range="unknown")
