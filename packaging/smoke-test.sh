#!/bin/sh
# Smoke test for an installed package (used by CI): the commands start and the main window opens.
set -e
infinitime-builder-cli --help > /dev/null
test -f /usr/share/applications/io.github.ScanuNicco.InfiniTimeBuilder.desktop
test -f /usr/share/icons/hicolor/scalable/apps/io.github.ScanuNicco.InfiniTimeBuilder.svg
QT_QPA_PLATFORM=offscreen INFINITIME_BUILDER_HOME=/tmp/itb-smoke python3 - <<'PY'
import os, sys
sys.path.insert(0, "/usr/share/infinitime-builder")
from itbuilder.qt import QApplication, BINDING
import itbuilder.gui as g
app = QApplication(sys.argv)
w = g.MainWindow()
w.show()
app.processEvents()
print(f"OK: main window opened with {BINDING}")
os._exit(0)  # don't wait for the background download the window starts
PY
