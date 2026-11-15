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


def test_task_reports_make_progress(app):
    got = {"log": [], "pct": []}

    def work(log, cancelled):
        log("[ 10%] Building C object a.o")
        log("[ 10%] Building C object b.o")  # same percentage: reported once
        log(gui.PROGRESS_ONLY + "[ 55%] Building C object c.o")  # progress only, not logged
        return 1

    t = gui.Task(work, lambda r: got.setdefault("done", r), lambda m: got.setdefault("failed", m),
                 got["log"].append, got["pct"].append)
    qt.QThreadPool.globalInstance().start(t)
    assert _wait(app, lambda: "done" in got)
    assert got["pct"] == [10, 55]
    assert got["log"] == ["[ 10%] Building C object a.o", "[ 10%] Building C object b.o"]


def test_tasks_do_not_queue_behind_each_other_on_a_single_core(window, app):
    """A long background task (the size measurement) must not hold up the next one (a build)."""
    import threading

    assert window.pool is not qt.QThreadPool.globalInstance()
    assert window.pool.maxThreadCount() >= 4  # independent of the number of CPU cores
    release = threading.Event()
    blocker = gui.Task(lambda log, c: release.wait(10), lambda r: None, lambda m: None)
    window.pool.start(blocker)
    got = {}
    try:
        window.run(lambda log, c: 7, lambda r: got.setdefault("done", r))
        assert _wait(app, lambda: "done" in got), "the second task waited for the first"
    finally:
        release.set()
    assert got["done"] == 7


def test_flash_meter_becomes_the_progress_bar(window, app):
    import threading

    window.size_state = "failed"  # no estimate: the meter shows a fixed message
    window.update_sizes()
    flash_text = window.meter_text.text()
    release = threading.Event()

    def work(log, cancelled):
        log(gui.STATUS + "Compiling firmware...")
        log("[ 37%] Building CXX object x.o")
        release.wait(10)

    window.run(work, lambda r: None, title="Building firmware")
    try:
        assert _wait(app, lambda: window.meter.progress_pct == 37)
        assert window.meter.progress_active
        assert window.meter_text.text() == "Building firmware"
        assert "37%" in window.progress_text.text()
        assert window.status.text() == "Compiling firmware..."
        window.on_changed()  # e.g. toggling an app must not replace the progress display
        assert window.meter_text.text() == "Building firmware"
    finally:
        release.set()
    assert _wait(app, lambda: not window.meter.progress_active)
    assert window.meter_text.text() == flash_text and window.progress_text.text() == ""
    assert "[ 37%] Building CXX object x.o" in window.log.toPlainText()


def test_progress_animates_until_a_percentage_is_known(window, app):
    import threading

    release = threading.Event()
    window.run(lambda log, c: release.wait(10), lambda r: None)
    try:
        assert window.meter.progress_active and window.meter.progress_pct is None
        assert window.meter.anim.isActive()
    finally:
        release.set()
    assert _wait(app, lambda: not window.busy)
    assert not window.meter.anim.isActive()


def test_build_pauses_the_size_measurement_and_resumes_it(window, app, monkeypatch):
    calls = []
    measuring = gui.Task(lambda log, c: None, lambda r: None, lambda m: None)
    window.calib_task = measuring
    window.calib_op = gui.Progress("Measuring")
    window.engine = object()
    monkeypatch.setattr(window.project, "build_request", lambda: ("request", [], []))

    def fake_build(req, engine, work, out, log, cancelled, output):
        calls.append(("build", window.calib_task, window.size_state))
        raise gui.bld.BuildError("Build cancelled")

    monkeypatch.setattr(gui.bld, "build", fake_build)
    monkeypatch.setattr(window, "start_calibration", lambda force=False: calls.append(("measure", force)))
    window.start_build()
    assert measuring.cancelled
    assert _wait(app, lambda: len(calls) == 2)
    assert calls == [("build", None, "paused"), ("measure", True)]
    assert not window.building


def test_measurement_requested_during_a_build_waits_for_it(window):
    window.project.catalog = object()
    window.engine = object()
    window.building = True
    window.start_calibration(force=True)
    assert window.calib_task is None and window.calib_resume and window.size_state == "paused"


def test_log_lines_are_added_in_batches(window, app):
    for i in range(500):
        window.append_log(f"line {i}")
    assert window.log.toPlainText() == ""  # not yet: one redraw for the lot
    assert _wait(app, lambda: window.log.blockCount() >= 500)
    assert window.log.toPlainText().splitlines()[-1] == "line 499"
