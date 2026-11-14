"""GUI tests, run offscreen. Skipped when neither PySide6 nor PyQt6 is installed."""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if os.environ.get("ITB_REQUIRE_QT"):  # CI: a broken Qt install must fail, not skip
    import itbuilder.qt as qt
else:
    qt = pytest.importorskip("itbuilder.qt", exc_type=ImportError)

from itbuilder import __version__  # noqa: E402
from itbuilder import gui  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return qt.QApplication.instance() or qt.QApplication([])


def _wait(app, pred, timeout=5.0):
    import time

    end = time.monotonic() + timeout
    while not pred() and time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)
    return pred()


def test_task_delivers_results_on_the_gui_thread(app):
    got = {}
    t = gui.Task(lambda log, cancelled: (log("hi"), 42)[1], lambda r: got.setdefault("done", r),
                 lambda m: got.setdefault("failed", m), lambda s: got.setdefault("log", s))
    qt.QThreadPool.globalInstance().start(t)
    assert _wait(app, lambda: "done" in got)
    assert got == {"log": "hi", "done": 42}


def test_task_reports_unexpected_errors(app):
    got = {}

    def boom(log, cancelled):
        raise KeyError("x")

    t = gui.Task(boom, lambda r: got.setdefault("done", r), lambda m: got.setdefault("failed", m))
    qt.QThreadPool.globalInstance().start(t)
    assert _wait(app, lambda: "failed" in got)
    assert "Unexpected error" in got["failed"] and "KeyError" in got["failed"]


@pytest.fixture
def window(app, tmp_path, monkeypatch):
    monkeypatch.setenv("INFINITIME_BUILDER_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(gui.MainWindow, "load_firmware", lambda self, refresh=False: None)  # no network
    warnings = []
    monkeypatch.setattr(gui.QMessageBox, "warning", lambda *a, **k: warnings.append(a[2]))
    w = gui.MainWindow()
    w.warnings = warnings
    yield w
    w.close()
    w.deleteLater()


def test_main_window_opens(window, app):
    window.show()
    app.processEvents()
    assert window.windowTitle() == "InfiniTime Builder"
    assert not window.warnings


def test_unreadable_settings_are_reported(app, tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / "settings.json").write_text("{broken")
    monkeypatch.setenv("INFINITIME_BUILDER_HOME", str(home))
    monkeypatch.setattr(gui.MainWindow, "load_firmware", lambda self, refresh=False: None)
    warnings = []
    monkeypatch.setattr(gui.QMessageBox, "warning", lambda *a, **k: warnings.append(a[2]))
    w = gui.MainWindow()
    assert _wait(app, lambda: warnings)
    assert "could not be read" in warnings[0]
    w.close()


def test_about_shows_version(window, monkeypatch):
    shown = []
    monkeypatch.setattr(gui.QMessageBox, "about", lambda parent, title, text: shown.append(text))
    window.show_about()
    assert __version__ in shown[0]


def test_settings_save_failure_is_shown_not_raised(window, monkeypatch):
    def fail(s):
        raise OSError("disk full")

    monkeypatch.setattr(gui, "save_settings", fail)
    window.persist()
    assert "disk full" in window.status.text()
