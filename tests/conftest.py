"""Desktop fixtures close background work before pytest-qt deletes widgets."""
import sqlite3

import pytest


@pytest.fixture(autouse=True)
def drain_desktop_work(monkeypatch, request):
    if "desktop" not in request.node.path.parts and request.node.path.name != "test_desktop_toolbox.py":
        yield
        return
    qtbot = request.getfixturevalue("qtbot")
    from galgame_news.desktop.controller import DesktopController
    from galgame_news.desktop.review_page import ReviewPage

    controllers, pages = [], []
    controller_init, page_init = DesktopController.__init__, ReviewPage.__init__

    def create_controller(self, *args, **kwargs):
        controller_init(self, *args, **kwargs)
        controllers.append(self)

    def create_page(self, *args, **kwargs):
        page_init(self, *args, **kwargs)
        pages.append(self)

    monkeypatch.setattr(DesktopController, "__init__", create_controller)
    monkeypatch.setattr(ReviewPage, "__init__", create_page)
    request.node._desktop_work = (qtbot, controllers, pages)
    yield


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_teardown(item, nextitem):
    work = getattr(item, "_desktop_work", None)
    if work is not None:
        _drain(*work)
    yield


def _drain(qtbot, controllers, pages):
    for controller in controllers:
        try:
            controller.close(5000)
        except sqlite3.ProgrammingError as error:
            # Some old tests close the catalog explicitly before teardown.
            if "closed" not in str(error):
                raise
        assert not controller.has_pending_work
    for page in pages:
        page.begin_close()
        qtbot.waitUntil(lambda: not page.has_pending_work, timeout=5000)
        qtbot.waitUntil(lambda: not page.query._retired and not page.state_writer._retired
                       and not page.images._retired and not page.exporter._retired, timeout=5000)
        page.finish_close()
