from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

from galgame_news.desktop.new_task_page import NewTaskPage


def test_socialdata_is_opt_in_per_task_and_included_in_start_request(tmp_path):
    app = QApplication.instance() or QApplication([])
    page = NewTaskPage(default_output=tmp_path / "output")
    page.set_input_path(tmp_path / "261.docx")
    assert page.section_checkboxes["z"].text() == "周边"
    payloads = []
    page.start_requested.connect(payloads.append)

    assert page.socialdata_checkbox.isChecked() is False
    page.socialdata_checkbox.setChecked(True)
    page.request_start()

    assert payloads[0]["use_socialdata_x"] is True
    assert payloads[0]["selected_sections"] == ["x", "h", "z"]
    page.close()
    app.processEvents()


@pytest.mark.parametrize(
    ("selected", "expected"),
    [
        (["x"], ["x"]),
        (["h"], ["h"]),
        (["z"], ["z"]),
        (["x", "h"], ["x", "h"]),
        (["x", "z"], ["x", "z"]),
        (["h", "z"], ["h", "z"]),
        (["x", "h", "z"], ["x", "h", "z"]),
    ],
)
def test_section_checkboxes_submit_selected_sections(qtbot, tmp_path, selected, expected):
    page = NewTaskPage(default_output=tmp_path / "output")
    qtbot.addWidget(page)
    page.set_input_path(tmp_path / "261.docx")
    payloads = []
    page.start_requested.connect(payloads.append)

    for key, checkbox in page.section_checkboxes.items():
        checkbox.setChecked(key in selected)
    page.request_start()

    assert payloads[0]["selected_sections"] == expected


def test_empty_section_selection_disables_start_and_does_not_emit(qtbot, tmp_path):
    page = NewTaskPage(default_output=tmp_path / "output")
    qtbot.addWidget(page)
    payloads = []
    page.start_requested.connect(payloads.append)

    for checkbox in page.section_checkboxes.values():
        checkbox.setChecked(False)

    assert page.start_button.isEnabled() is False
    assert page.section_hint.isHidden() is False
    assert page.section_hint.text() == "请至少选择一个抓取栏目"
    page.request_start()
    assert payloads == []
    assert page.drop_hint.text() == "拖入一份周报，开始整理本期配图"

    page.section_checkboxes["h"].setChecked(True)
    assert page.start_button.isEnabled() is True
    assert page.section_hint.isHidden() is True


def test_section_selection_and_busy_state_control_start_button(qtbot, tmp_path):
    page = NewTaskPage(default_output=tmp_path / "output")
    qtbot.addWidget(page)

    page.set_busy(True)
    assert page.start_button.isEnabled() is False
    page.section_checkboxes["x"].setChecked(False)
    page.section_checkboxes["h"].setChecked(False)
    page.section_checkboxes["z"].setChecked(False)
    assert page.start_button.isEnabled() is False
    page.section_checkboxes["z"].setChecked(True)
    assert page.start_button.isEnabled() is False

    page.set_busy(False)
    assert page.start_button.isEnabled() is True
    page.set_busy(True)
    page.set_busy(False)
    assert page.start_button.isEnabled() is True

