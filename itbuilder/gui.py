"""InfiniTime Builder - graphical interface."""
from __future__ import annotations

import json
import shutil
import sys
import time
import uuid
import traceback
from dataclasses import dataclass, field
from pathlib import Path

from .qt import (  # noqa: F401  (PySide6 or PyQt6)
    QAbstractItemView, QAction, QApplication, QColor, QComboBox, QDesktopServices, QDialog, QDialogButtonBox, QFileDialog, QFont, QFontDatabase, QFormLayout, QFrame, QHBoxLayout, QIcon, QKeySequence, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMessageBox, QObject, QPainter, QPainterPath, QPalette, QPen, QPixmap, QPlainTextEdit, QPointF, QPushButton, QRectF, QRunnable, QScrollArea, QSize, QSizePolicy, QSplitter, QStackedWidget, QStyle, QStyleOptionViewItem, QStyledItemDelegate, QThreadPool, QTimer, QUrl, QVBoxLayout, QWidget, Qt, Signal, Slot,
)

from . import __version__
from . import builder as bld
from . import firmware as fw
from . import sizes
from .fsutil import atomic_write_text
from .manifest import ManifestError
from .project import Entry, Project, Settings, data_dir, load_settings, resolve_ref, save_settings
from .sources import FetchError, SourceSpec, SourceStore, discover

APP_ID = "io.github.ScanuNicco.InfiniTimeBuilder"
KEY_ROLE = Qt.ItemDataRole.UserRole.value + 1
SUB_ROLE = Qt.ItemDataRole.UserRole.value + 2
ON_ROLE = Qt.ItemDataRole.UserRole.value + 3
ICON_ROLE = Qt.ItemDataRole.UserRole.value + 4
ICON_FAMILY: str | None = None

ACCENT = QColor("#2f8f6f")
WARN = QColor("#d08a12")
BAD = QColor("#cf3f3f")
ROW_H = 58
# Estimates are slightly pessimistic (they keep shared code); allow building when this close.
SIZE_TOLERANCE = 0.015


def fmt_kb(n: int) -> str:
    return f"{n / 1024:.1f} KB" if n < 10 * 1024 else f"{n / 1024:.0f} KB"


def mix(a: QColor, b: QColor, t: float) -> QColor:
    return QColor(int(a.red() + (b.red() - a.red()) * t), int(a.green() + (b.green() - a.green()) * t),
                  int(a.blue() + (b.blue() - a.blue()) * t))


def is_dark(w: QWidget) -> bool:
    return w.palette().color(QPalette.ColorRole.Window).lightness() < 128


# --------------------------------------------------------------------------- background tasks


# Task log lines: "\x01..." is a progress message (also shown in the status line); "\x03..." is
# tool output used only for the progress bar (e.g. the size measurement's compiler output).
STATUS, PROGRESS_ONLY = "\x01", "\x03"


class _Signals(QObject):
    log = Signal(str)
    progress = Signal(int)
    done = Signal(object)
    failed = Signal(str)


class _Bridge(QObject):
    """Lives in the GUI thread so the callbacks always run there (queued connections)."""

    def __init__(self, on_log, on_done, on_failed, on_progress):
        super().__init__()
        self.cbs = (on_log, on_done, on_failed, on_progress)

    @Slot(str)
    def log(self, s):
        self.cbs[0](s)

    @Slot(object)
    def done(self, r):
        self.cbs[1](r)

    @Slot(str)
    def failed(self, m):
        self.cbs[2](m)

    @Slot(int)
    def progress(self, pct):
        self.cbs[3](pct)


class Task(QRunnable):
    """Run fn(log, cancelled) in the thread pool; callbacks are invoked in the GUI thread.

    Lines passed to log that are make progress lines ("[ 42%] ...") also go to on_progress(42);
    lines starting with PROGRESS_ONLY go only there.
    """

    def __init__(self, fn, on_done, on_failed, on_log=lambda s: None, on_progress=lambda pct: None):
        super().__init__()
        self.setAutoDelete(False)
        self.fn = fn
        self.signals = _Signals()
        self.bridge = _Bridge(on_log, on_done, on_failed, on_progress)
        self.signals.log.connect(self.bridge.log)
        self.signals.done.connect(self.bridge.done)
        self.signals.failed.connect(self.bridge.failed)
        self.signals.progress.connect(self.bridge.progress)
        self.cancelled = False
        self._pct = -1

    def _emit(self, signal_name: str, value) -> None:
        # The window may have been closed while this task was still running; then there is
        # nobody left to tell, so stop quietly (and cancel the rest of the work).
        try:
            getattr(self.signals, signal_name).emit(value)
        except RuntimeError:
            self.cancelled = True

    def _log(self, line: str) -> None:
        only_progress = line.startswith(PROGRESS_ONLY)
        if only_progress:
            line = line[1:]
        pct = bld.make_progress(line)
        if pct is not None and pct != self._pct:
            self._pct = pct
            self._emit("progress", pct)
        if not only_progress:
            self._emit("log", line)

    def run(self):
        try:
            res = self.fn(self._log, lambda: self.cancelled)
        except (bld.BuildError, FetchError, ManifestError, fw.FirmwareError, OSError) as e:
            self._emit("failed", str(e))
        except Exception:  # noqa: BLE001 - surface unexpected errors to the user instead of dying silently
            self._emit("failed", "Unexpected error:\n" + traceback.format_exc())
        else:
            self._emit("done", res)


# --------------------------------------------------------------------------- toggle rows


def icon_font(px: int) -> QFont:
    f = QFont(ICON_FAMILY) if ICON_FAMILY else QFont()
    f.setPixelSize(px)
    f.setBold(True)
    return f


def draw_icon(p: QPainter, rect: QRectF, icon: str | None, name: str, px: int):
    if icon and ord(icon[0]) >= 0xE000 and ICON_FAMILY:
        p.setFont(icon_font(px))
        p.drawText(rect, Qt.AlignmentFlag.AlignCenter, icon)
    else:
        f = QFont()
        f.setPixelSize(px)
        f.setBold(True)
        p.setFont(f)
        p.drawText(rect, Qt.AlignmentFlag.AlignCenter, icon or name[:1])


def draw_switch(p: QPainter, rect: QRectF, on: bool, enabled_color: QColor, off_color: QColor):
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(enabled_color if on else off_color)
    p.drawRoundedRect(rect, rect.height() / 2, rect.height() / 2)
    d = rect.height() - 6
    x = rect.right() - d - 3 if on else rect.left() + 3
    p.setBrush(QColor("#ffffff"))
    p.drawEllipse(QRectF(x, rect.top() + 3, d, d))


class RowDelegate(QStyledItemDelegate):
    """Paints a row: [grip] [icon] name / subtitle ......... [switch]. Clicks are handled by ToggleList."""

    def __init__(self, parent, draggable: bool):
        super().__init__(parent)
        self.draggable = draggable

    def sizeHint(self, option, index):
        return QSize(200, ROW_H)

    def _switch_rect(self, r: QRectF) -> QRectF:
        return QRectF(r.right() - 58, r.center().y() - 12, 44, 24)

    def paint(self, p: QPainter, opt: QStyleOptionViewItem, index):
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        # the list itself is transparent, so take colours from the application palette
        pal = QApplication.palette()
        r = QRectF(opt.rect).adjusted(2, 3, -2, -3)
        on = bool(index.data(ON_ROLE))
        hover = bool(opt.state & QStyle.StateFlag.State_MouseOver)
        selected = bool(opt.state & QStyle.StateFlag.State_Selected)
        base = pal.color(QPalette.ColorRole.Base)
        text = pal.color(QPalette.ColorRole.Text)
        bg = mix(base, text, 0.07 if (hover or selected) else 0.03)
        p.setPen(QPen(mix(base, ACCENT, 0.6), 1.5) if selected else Qt.PenStyle.NoPen)
        p.setBrush(bg)
        p.drawRoundedRect(r, 10, 10)

        x = r.left() + 10
        if self.draggable:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(mix(base, text, 0.35))
            for row in range(3):
                for col in range(2):
                    p.drawEllipse(QPointF(x + 3 + col * 6, r.center().y() - 6 + row * 6), 1.6, 1.6)
            x += 20
        ic = QRectF(x, r.center().y() - 18, 36, 36)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(ACCENT if on else mix(base, text, 0.18))
        p.drawRoundedRect(ic, 9, 9)
        p.setPen(QColor("#ffffff") if on else mix(base, text, 0.6))
        draw_icon(p, ic, index.data(ICON_ROLE), index.data(Qt.ItemDataRole.DisplayRole), 17)

        tx = ic.right() + 12
        sw = self._switch_rect(r)
        f = QFont(opt.font)
        f.setPointSizeF(f.pointSizeF() * 1.05)
        f.setBold(True)
        p.setFont(f)
        p.setPen(text if on else mix(base, text, 0.65))
        name_rect = QRectF(tx, r.top() + 8, sw.left() - tx - 10, r.height() / 2 - 6)
        p.drawText(name_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   p.fontMetrics().elidedText(index.data(Qt.ItemDataRole.DisplayRole), Qt.TextElideMode.ElideRight, int(name_rect.width())))
        f2 = QFont(opt.font)
        f2.setPointSizeF(f2.pointSizeF() * 0.88)
        p.setFont(f2)
        p.setPen(mix(base, text, 0.55))
        sub_rect = QRectF(tx, r.center().y() + 1, sw.left() - tx - 10, r.height() / 2 - 8)
        p.drawText(sub_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   p.fontMetrics().elidedText(index.data(SUB_ROLE) or "", Qt.TextElideMode.ElideRight, int(sub_rect.width())))
        draw_switch(p, sw, on, ACCENT, mix(base, text, 0.25))
        p.restore()


class ToggleList(QListWidget):
    """A list of toggle rows that sizes itself to its content (the page scrolls, not the list)."""

    toggled = Signal(str)

    def __init__(self, draggable: bool):
        super().__init__()
        self.delegate = RowDelegate(self, draggable)
        self.setItemDelegate(self.delegate)
        self.setMouseTracking(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setSpacing(0)
        self.setStyleSheet("QListWidget { background: transparent; } QListWidget::item { border: none; }")
        if draggable:
            self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
            self.setDefaultDropAction(Qt.DropAction.MoveAction)
            self.setDragEnabled(True)
        self.model().rowsInserted.connect(lambda *a: self.fit())
        self.model().rowsRemoved.connect(lambda *a: self.fit())

    def fit(self):
        visible = sum(1 for i in range(self.count()) if not self.isRowHidden(i))
        self.setFixedHeight(max(visible * ROW_H, 4) + 4)

    def mouseReleaseEvent(self, ev):
        item = self.itemAt(ev.position().toPoint())
        if item and ev.button() == Qt.MouseButton.LeftButton:
            rect = QRectF(self.visualItemRect(item)).adjusted(2, 3, -2, -3)
            sw = self.delegate._switch_rect(rect).adjusted(-8, -10, 8, 10)
            if sw.contains(ev.position()):
                self.toggled.emit(item.data(KEY_ROLE))
                return
        super().mouseReleaseEvent(ev)

    def mouseDoubleClickEvent(self, ev):
        item = self.itemAt(ev.position().toPoint())
        if item:
            self.toggled.emit(item.data(KEY_ROLE))

    def keyPressEvent(self, ev):
        if ev.key() in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter) and self.currentItem():
            self.toggled.emit(self.currentItem().data(KEY_ROLE))
            return
        super().keyPressEvent(ev)

    def keys(self) -> list[str]:
        return [self.item(i).data(KEY_ROLE) for i in range(self.count())]


# --------------------------------------------------------------------------- previews


class WatchPreview(QWidget):
    """A PineTime drawn to scale. Subclasses paint the 240x240 screen. Click to flip pages."""

    def __init__(self):
        super().__init__()
        self.entries: list[Entry] = []
        self.per_page = 6
        self.page = 0
        self.setMinimumSize(260, 300)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def set_entries(self, entries: list[Entry], per_page: int | None = None):
        self.entries = entries
        if per_page:
            self.per_page = per_page
        self.page = min(self.page, self.pages - 1)
        self.update()

    @property
    def pages(self) -> int:
        return max(1, (len(self.entries) + self.per_page - 1) // self.per_page)

    def mousePressEvent(self, ev):
        self.page = (self.page + (1 if ev.position().x() > self.width() / 2 else -1)) % self.pages
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        side = max(120, min(self.width() - 60, self.height() - 70, 340))
        x0, y0 = (self.width() - side) / 2, 24
        # case and strap
        strap = QColor("#2b2b2e")
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(strap)
        p.drawRoundedRect(QRectF(x0 + side * 0.18, 0, side * 0.64, self.height()), 14, 14)
        p.setBrush(QColor("#4a4a50"))
        p.drawRoundedRect(QRectF(x0 - 16, y0 - 16, side + 32, side + 32), 34, 34)
        p.setBrush(QColor("#5a5a62"))
        p.drawRoundedRect(QRectF(x0 + side + 14, y0 + side * 0.35, 6, side * 0.3), 3, 3)  # button
        p.setBrush(QColor("#000"))
        p.drawRoundedRect(QRectF(x0 - 4, y0 - 4, side + 8, side + 8), 22, 22)
        p.save()
        clip = QPainterPath()
        clip.addRect(QRectF(x0, y0, side, side))
        p.setClipPath(clip)
        p.translate(x0, y0)
        p.scale(side / 240, side / 240)
        self.paint_screen(p)
        p.restore()
        caption = self.caption()
        if caption:
            f = QFont()
            f.setPointSizeF(f.pointSizeF() * 0.9)
            p.setFont(f)
            w = p.fontMetrics().horizontalAdvance(caption) + 24
            r = QRectF((self.width() - w) / 2, y0 + side + 22, w, 22)
            p.setBrush(self.palette().color(QPalette.ColorRole.Window))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawRoundedRect(r, 11, 11)
            p.setPen(self.palette().color(QPalette.ColorRole.WindowText))
            p.drawText(r, Qt.AlignmentFlag.AlignCenter, caption)

    def caption(self) -> str | None:
        return f"page {self.page + 1} / {self.pages}  ·  click to flip" if self.pages > 1 else None

    def paint_screen(self, p: QPainter):
        raise NotImplementedError


class LauncherPreview(WatchPreview):
    def paint_screen(self, p: QPainter):
        items = self.entries[self.page * self.per_page:(self.page + 1) * self.per_page]
        p.setPen(QColor("#fff"))
        f = QFont("monospace")
        f.setPixelSize(17)
        f.setBold(True)
        p.setFont(f)
        p.drawText(QRectF(4, 0, 100, 22), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, "10:09")
        for i, e in enumerate(items):
            c, r = i % 3, i // 3
            rect = QRectF(8 + c * 76, 28 + r * 106, 70, 98)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor("#383838"))
            p.drawRoundedRect(rect, 12, 12)
            p.setPen(QColor("#fff"))
            draw_icon(p, rect, e.icon, e.name, 26)
        if not items:
            p.setPen(QColor("#888"))
            f = QFont()
            f.setPixelSize(16)
            p.setFont(f)
            p.drawText(QRectF(0, 0, 240, 240), Qt.AlignmentFlag.AlignCenter, "No apps")
        if self.pages > 1:
            h = 210 / self.pages
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor("#333"))
            p.drawRect(QRectF(235, 28, 3, 210))
            p.setBrush(QColor("#ddd"))
            p.drawRect(QRectF(235, 28 + h * self.page, 3, h))


class FacePreview(WatchPreview):
    """A screenshot of a watch face. Shows the face clicked in the list; clicking the watch steps
    through the faces that are on the watch."""

    def __init__(self):
        super().__init__()
        self.per_page = 1
        self.focus: Entry | None = None  # a face clicked in the lists, if any
        self._pixmaps: dict[str, QPixmap] = {}

    def show_entry(self, entry: Entry | None):
        self.focus = entry
        if entry in self.entries:
            self.page = self.entries.index(entry)
            self.focus = None
        self.update()

    def set_entries(self, entries: list[Entry], per_page: int | None = None):
        if self.focus is None and self.entries and 0 <= self.page < len(self.entries) and self.entries[self.page] in entries:
            self.page = entries.index(self.entries[self.page])  # keep showing the same face after a reorder
        super().set_entries(entries)

    def mousePressEvent(self, ev):
        self.focus = None
        super().mousePressEvent(ev)

    def current(self) -> Entry | None:
        if self.focus is not None:
            return self.focus
        return self.entries[self.page] if self.entries else None

    def caption(self) -> str | None:
        e = self.current()
        if e is None:
            return None
        if self.focus is not None:
            return f"{e.name}  ·  not on your watch"
        return f"{e.name}  ·  {self.page + 1} / {len(self.entries)}" + ("  ·  click for next" if len(self.entries) > 1 else "")

    def paint_screen(self, p: QPainter):
        e = self.current()
        path = str(e.screenshot) if e is not None and e.screenshot is not None else None
        if path is not None and path not in self._pixmaps:
            self._pixmaps[path] = QPixmap(path)
        pix = self._pixmaps.get(path) if path else None
        if pix is not None and not pix.isNull():
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            p.drawPixmap(QRectF(0, 0, 240, 240), pix, QRectF(pix.rect()))
            return
        p.setPen(QColor("#9a9a9a"))
        f = QFont()
        f.setPixelSize(17)
        p.setFont(f)
        text = "No watch faces" if e is None else f"{e.name}\n\nno preview available"
        # different flag types: PyQt6 only combines them as plain integers
        flags = Qt.AlignmentFlag.AlignCenter.value | Qt.TextFlag.TextWordWrap.value
        p.drawText(QRectF(10, 0, 220, 240), flags, text)


# --------------------------------------------------------------------------- flash meter


class FlashMeter(QWidget):
    """The flash usage bar. While an operation runs it becomes that operation's progress bar."""

    def __init__(self):
        super().__init__()
        self.used: int | None = None
        self.limit = sizes.DEFAULT_FLASH_LIMIT
        self.progress_active = False
        self.progress_pct: int | None = None  # None while active: no percentage known, so animate
        self.anim = QTimer(self, interval=40)
        self.anim.timeout.connect(self.update)
        self.setMinimumHeight(14)
        self.setMaximumHeight(14)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set(self, used: int | None, limit: int):
        self.used, self.limit = used, limit
        self.update()

    def set_progress(self, active: bool, pct: int | None = None):
        self.progress_active, self.progress_pct = active, pct if active else None
        animate = active and pct is None
        if animate and not self.anim.isActive():
            self.anim.start()
        elif not animate:
            self.anim.stop()
        self.update()

    def colour(self) -> QColor:
        if self.used is None:
            return QColor("#888")
        frac = self.used / self.limit
        return BAD if frac > 1 else WARN if frac > 0.92 else ACCENT

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        base = self.palette().color(QPalette.ColorRole.Window)
        text = self.palette().color(QPalette.ColorRole.WindowText)
        r = QRectF(self.rect()).adjusted(0, 2, 0, -2)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(mix(base, text, 0.12))
        p.drawRoundedRect(r, r.height() / 2, r.height() / 2)
        rad = r.height() / 2
        if self.progress_active:
            p.setBrush(ACCENT)
            if self.progress_pct is not None:
                w = r.width() * self.progress_pct / 100
                if w > 0:
                    p.drawRoundedRect(QRectF(r.left(), r.top(), max(w, r.height()), r.height()), rad, rad)
            else:  # a block sliding across, timed by the clock so a slow machine just skips frames
                chunk = r.width() * 0.25
                t = (time.monotonic() % 1.6) / 1.6
                x = r.left() - chunk + (r.width() + chunk) * t
                p.setClipRect(r)
                p.drawRoundedRect(QRectF(x, r.top(), chunk, r.height()), rad, rad)
            return
        if self.used is not None:
            scale = max(self.limit, self.used) * 1.0
            w = r.width() * self.used / scale
            p.setBrush(self.colour())
            p.drawRoundedRect(QRectF(r.left(), r.top(), max(w, r.height()), r.height()), r.height() / 2, r.height() / 2)
            if self.used > self.limit:  # limit marker
                lx = r.left() + r.width() * self.limit / scale
                p.setBrush(text)
                p.drawRect(QRectF(lx - 1, r.top() - 2, 2, r.height() + 4))


# --------------------------------------------------------------------------- picker page


class PickerPage(QWidget):
    """Apps or watch faces: an ordered 'on your watch' list and an 'available' list, with toggles."""

    changed = Signal()

    def __init__(self, kind: str):
        super().__init__()
        self.kind = kind
        self.entries: dict[str, Entry] = {}
        self.subtitles: dict[str, str] = {}
        noun = "apps" if kind == "app" else "watch faces"

        self.search = QLineEdit()
        self.search.setPlaceholderText(f"Search {noun}")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self.apply_filter)
        self.on_title = QLabel()
        self.off_title = QLabel()
        for t in (self.on_title, self.off_title):
            t.setObjectName("sectionTitle")
        self.on_hint = QLabel("Drag to set the order on the watch." if kind == "app" else
                              "The first face is used if the one you had is not included. Drag to reorder.")
        self.on_hint.setObjectName("hint")
        self.on_list = ToggleList(draggable=True)
        self.off_list = ToggleList(draggable=False)
        self.on_list.toggled.connect(self.toggle)
        self.off_list.toggled.connect(self.toggle)
        self.on_list.model().rowsMoved.connect(lambda *a: self._changed())
        self.empty_off = QLabel(f"All {noun} are on your watch.")
        self.empty_off.setObjectName("hint")

        col = QVBoxLayout()
        col.setContentsMargins(0, 0, 8, 0)
        col.addWidget(self.search)
        col.addSpacing(10)
        col.addWidget(self.on_title)
        col.addWidget(self.on_hint)
        col.addWidget(self.on_list)
        col.addSpacing(14)
        col.addWidget(self.off_title)
        col.addWidget(self.off_list)
        col.addWidget(self.empty_off)
        col.addStretch()
        inner = QWidget()
        inner.setLayout(col)
        scroll = QScrollArea()
        scroll.setWidget(inner)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self.preview = LauncherPreview() if kind == "app" else FacePreview()
        if kind == "watchface":  # show the face that was clicked
            for lw in (self.on_list, self.off_list):
                lw.currentItemChanged.connect(
                    lambda cur, _prev: cur is not None and self.preview.show_entry(self.entries.get(cur.data(KEY_ROLE))))
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(scroll, 5)
        lay.addWidget(self.preview, 4)

    def _item(self, e: Entry, on: bool) -> QListWidgetItem:
        it = QListWidgetItem(e.name)
        it.setData(KEY_ROLE, e.key)
        it.setData(ON_ROLE, on)
        it.setData(ICON_ROLE, e.icon or ("\uf017" if self.kind == "watchface" and ICON_FAMILY else None))
        it.setData(SUB_ROLE, self.subtitles.get(e.key, e.origin))
        it.setToolTip(f"{e.name} - {e.origin}" + (f"\n{e.description}" if e.description else "")
                      + f"\n{e.qualified}\n\nClick the switch (or double-click) to turn on or off.")
        flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        if on:
            flags |= Qt.ItemFlag.ItemIsDragEnabled
        it.setFlags(flags)
        return it

    def populate(self, entries: dict[str, Entry], selected: list[str]):
        self.entries = entries
        self.on_list.clear()
        self.off_list.clear()
        for k in selected:
            if k in entries:
                self.on_list.addItem(self._item(entries[k], True))
        for e in sorted((e for k, e in entries.items() if k not in selected), key=lambda e: e.name.lower()):
            self.off_list.addItem(self._item(e, False))
        self.refresh()

    def selected(self) -> list[str]:
        return self.on_list.keys()

    def toggle(self, key: str):
        for src, dst, on in ((self.on_list, self.off_list, False), (self.off_list, self.on_list, True)):
            for i in range(src.count()):
                if src.item(i).data(KEY_ROLE) == key:
                    src.takeItem(i)
                    item = self._item(self.entries[key], on)
                    if on:
                        dst.addItem(item)
                    else:  # keep "available" alphabetical
                        names = [dst.item(j).text().lower() for j in range(dst.count())]
                        pos = next((j for j, n in enumerate(names) if n > item.text().lower()), len(names))
                        dst.insertItem(pos, item)
                    self._changed()
                    return

    def set_subtitles(self, subtitles: dict[str, str]):
        self.subtitles = subtitles
        for lw in (self.on_list, self.off_list):
            for i in range(lw.count()):
                it = lw.item(i)
                it.setData(SUB_ROLE, subtitles.get(it.data(KEY_ROLE), ""))

    def apply_filter(self):
        q = self.search.text().strip().lower()
        for lw in (self.on_list, self.off_list):
            for i in range(lw.count()):
                e = self.entries.get(lw.item(i).data(KEY_ROLE))
                hay = f"{e.name} {e.origin} {e.description}".lower() if e else ""
                lw.setRowHidden(i, bool(q) and q not in hay)
            lw.fit()

    def _changed(self):
        self.refresh()
        self.changed.emit()

    def refresh(self):
        noun = "apps" if self.kind == "app" else "watch faces"
        self.on_title.setText(f"On your watch  ·  {self.on_list.count()}")
        self.off_title.setText(f"Available {noun}  ·  {self.off_list.count()}")
        self.off_title.setVisible(self.off_list.count() > 0)
        self.empty_off.setVisible(self.off_list.count() == 0)
        self.apply_filter()
        self.preview.set_entries([self.entries[k] for k in self.selected() if k in self.entries])


# --------------------------------------------------------------------------- dialogs


class DiscoverDialog(QDialog):
    def __init__(self, parent, pool):
        super().__init__(parent)
        self.setWindowTitle("Find apps on GitHub")
        self.resize(560, 420)
        self.pool = pool
        self.list = QListWidget()
        self.status = QLabel("Searching GitHub for repositories tagged 'infinitime-app'...")
        self.status.setWordWrap(True)
        self.query = QLineEdit()
        self.query.setPlaceholderText("Filter (optional)")
        self.query.returnPressed.connect(self.search)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.button(QDialogButtonBox.StandardButton.Ok).setText("Add selected")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        self.list.itemDoubleClicked.connect(lambda _: self.accept())
        lay = QVBoxLayout(self)
        lay.addWidget(self.query)
        lay.addWidget(self.status)
        lay.addWidget(self.list)
        lay.addWidget(bb)
        self.search()

    def search(self):
        q = self.query.text().strip()
        self.task = Task(lambda log, c: discover(q), self._show, self.status.setText)
        self.pool.start(self.task)

    def _show(self, repos):
        self.list.clear()
        self.status.setText(f"{len(repos)} repositories found." if repos else
                            "No repositories found. App authors: add the 'infinitime-app' topic to your repo.")
        for r in repos:
            it = QListWidgetItem(f"{r['full_name']}  ({r['stars']} stars)\n    {r['description']}")
            it.setData(KEY_ROLE, r["full_name"])
            self.list.addItem(it)

    def chosen(self) -> str | None:
        it = self.list.currentItem()
        return it.data(KEY_ROLE) if it else None


class FirmwareDialog(QDialog):
    def __init__(self, parent, settings: Settings):
        super().__init__(parent)
        self.setWindowTitle("Firmware source")
        self.url = QLineEdit(settings.firmware_url)
        self.ref = QComboBox()
        self.ref.setEditable(True)
        self.ref.addItems(["", "main"])
        self.ref.setCurrentText(settings.firmware_ref)
        self.ref.lineEdit().setPlaceholderText("Latest release")
        note = QLabel("Use the official repository, or your own fork. Version may be a tag or branch; "
                      "leave it empty to use the latest release.")
        note.setWordWrap(True)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        form = QFormLayout(self)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setContentsMargins(20, 18, 20, 16)
        form.setVerticalSpacing(12)
        form.addRow(note)
        form.addRow("Repository", self.url)
        form.addRow("Version", self.ref)
        form.addRow(bb)
        # A word-wrapped label makes Qt size the dialog to its narrowest wrap; give it room instead.
        self.setMinimumWidth(560)
        self.resize(620, self.sizeHint().height())


# --------------------------------------------------------------------------- main window


STYLE = """
QMainWindow, #page { background: palette(window); }
#title { font-size: 20px; font-weight: 700; }
#chip { padding: 3px 10px; border-radius: 11px; background: %(chip)s; }
#sectionTitle { font-size: 13px; font-weight: 700; padding: 2px 4px; }
#hint { color: %(muted)s; padding: 0 4px 4px 4px; }
#navButton { border: none; padding: 8px 16px; border-radius: 16px; font-weight: 600; color: %(muted)s; }
#navButton:checked { background: %(chip)s; color: palette(window-text); }
#navButton:hover:!checked { color: palette(window-text); }
#buildButton { background: %(accent)s; color: white; border: none; border-radius: 10px;
               padding: 10px 26px; font-size: 15px; font-weight: 700; min-height: 20px; }
#buildButton:hover { background: %(accent_hover)s; }
#buildButton:disabled { background: %(chip)s; color: %(muted)s; }
#bottomBar { background: %(bar)s; border-top: 1px solid %(line)s; }
#secondaryButton, #logButton, #stopButton { border-radius: 10px; padding: 10px 18px; font-size: 14px;
               font-weight: 600; min-height: 20px; }
#secondaryButton, #logButton { background: %(chip)s; border: 1px solid transparent; }
#secondaryButton:hover, #logButton:hover { border-color: %(line)s; background: %(line)s; }
#logButton:checked { background: %(accent_tint)s; color: %(accent_text)s; border-color: %(accent)s; }
#stopButton { background: transparent; color: %(bad)s; border: 1px solid %(bad)s; }
#stopButton:hover { background: %(bad_tint)s; }
#stopButton:pressed, #secondaryButton:pressed, #logButton:pressed { padding-top: 11px; padding-bottom: 9px; }
#meterText { font-weight: 600; }
#progressText { font-weight: 600; color: %(muted)s; }
#statusText { color: %(muted)s; }
QLineEdit { padding: 6px 10px; border-radius: 8px; border: 1px solid %(line)s; background: palette(base); }
#card { background: %(card)s; border-radius: 12px; }
"""


@dataclass
class Progress:
    """A running operation shown by the progress bar. pct None means 'no percentage known yet'."""
    label: str = ""
    pct: int | None = None
    start: float = field(default_factory=time.monotonic)


def fmt_elapsed(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


# Tasks mostly wait on containers and the network, so they get their own pool instead of Qt's
# global one, which allows one thread per CPU core: on a single-core computer a build clicked
# while app sizes were being measured would otherwise wait, greyed out, until that finished.
POOL_THREADS = 8


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("InfiniTime Builder")
        self.resize(1180, 780)
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(POOL_THREADS)
        self.settings = load_settings()
        self.project = Project(self.settings, SourceStore(data_dir() / "cache"))
        self.engine: bld.Engine | None = None
        self.engine_error = ""
        self.task: Task | None = None
        self.calib_task: Task | None = None
        self.size_profile: sizes.SizeProfile | None = None
        self.size_state = "none"  # none | measuring | paused | ready | failed
        self.building = False
        self.calib_resume = False  # measure again once the current build is over
        self.op: Progress | None = None  # the foreground operation (self.task)
        self.calib_op: Progress | None = None  # the size measurement (self.calib_task)
        self.progress_timer = QTimer(self, interval=1000)  # keeps the elapsed time ticking
        self.progress_timer.timeout.connect(self.refresh_progress)
        # Tool output arrives a line at a time; adding it to the log in batches keeps a fast build
        # from making the window redraw hundreds of times a second.
        self._log_buf: list[str] = []
        self.log_timer = QTimer(self, singleShot=True, interval=150)
        self.log_timer.timeout.connect(self.flush_log)
        self.last_result: bld.BuildResult | None = None
        self.estimate_over = False
        self.calib_timer = QTimer(self, singleShot=True, interval=800)
        self.calib_timer.timeout.connect(lambda: self.start_calibration(force=True))
        self._apply_style()

        # ---- header
        title = QLabel("InfiniTime Builder")
        title.setObjectName("title")
        self.fw_chip = QPushButton("Loading firmware…")
        self.fw_chip.setObjectName("chip")
        self.fw_chip.setFlat(True)
        self.fw_chip.setCursor(Qt.CursorShape.PointingHandCursor)
        self.fw_chip.setToolTip("Change InfiniTime version or use a fork")
        self.fw_chip.clicked.connect(self.change_firmware)
        self.engine_chip = QLabel("Checking Docker…")
        self.engine_chip.setObjectName("chip")
        nav = QHBoxLayout()
        nav.setSpacing(4)
        self.nav_buttons = []
        for i, label in enumerate(("Apps", "Watch faces", "Sources")):
            b = QPushButton(label)
            b.setObjectName("navButton")
            b.setCheckable(True)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(lambda _=False, i=i: self.show_page(i))
            nav.addWidget(b)
            self.nav_buttons.append(b)
        head = QHBoxLayout()
        head.addWidget(title)
        head.addSpacing(18)
        head.addLayout(nav)
        head.addStretch()
        head.addWidget(self.fw_chip)
        head.addWidget(self.engine_chip)

        # ---- pages
        self.apps = PickerPage("app")
        self.faces = PickerPage("watchface")
        self.apps.changed.connect(self.on_changed)
        self.faces.changed.connect(self.on_changed)
        self.pages = QStackedWidget()
        self.pages.addWidget(self.apps)
        self.pages.addWidget(self.faces)
        self.pages.addWidget(self._sources_page())

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(20000)
        self.log.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.log.hide()
        split = QSplitter(Qt.Orientation.Vertical)
        split.addWidget(self.pages)
        split.addWidget(self.log)
        split.setStretchFactor(0, 3)

        # ---- bottom bar
        self.meter = FlashMeter()
        self.meter_text = QLabel("Flash: measuring…")
        self.meter_text.setObjectName("meterText")
        self.status = QLabel("")
        self.status.setObjectName("statusText")
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        info = QVBoxLayout()
        info.setSpacing(4)
        top = QHBoxLayout()
        top.addWidget(self.meter_text)
        top.addStretch()
        self.progress_text = QLabel("")  # percentage and elapsed time while an operation runs
        self.progress_text.setObjectName("progressText")
        top.addWidget(self.progress_text)
        info.addLayout(top)
        info.addWidget(self.meter)
        self.res_text = QLabel("")
        self.res_text.setObjectName("statusText")
        self.res_text.setToolTip("Fonts and images for the watch's external storage. Send the "
                                 "infinitime-resources zip to the watch after the firmware.")
        info.addWidget(self.res_text)
        info.addWidget(self.status)
        self.build_btn = QPushButton("Build firmware")
        self.build_btn.setObjectName("buildButton")
        self.build_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.build_btn.clicked.connect(self.start_build)
        self.cancel_btn = QPushButton("Stop")
        self.cancel_btn.setObjectName("stopButton")
        self.cancel_btn.setToolTip("Stop the build")
        self.cancel_btn.clicked.connect(self.cancel)
        self.cancel_btn.hide()
        self.open_btn = QPushButton("Open folder")
        self.open_btn.setObjectName("secondaryButton")
        self.open_btn.clicked.connect(self.open_output)
        self.open_btn.hide()
        self.log_btn = QPushButton("Log")
        self.log_btn.setObjectName("logButton")
        self.log_btn.setCheckable(True)
        self.log_btn.setToolTip("Show the full build output")
        self.log_btn.toggled.connect(self.log.setVisible)
        for b in (self.cancel_btn, self.open_btn, self.log_btn):
            b.setCursor(Qt.CursorShape.PointingHandCursor)
        bar = QWidget()
        bar.setObjectName("bottomBar")
        bl = QHBoxLayout(bar)
        bl.setContentsMargins(18, 12, 18, 12)
        bl.addLayout(info, 1)
        bl.addSpacing(16)
        bl.setSpacing(8)
        bl.addWidget(self.log_btn)
        bl.addWidget(self.open_btn)
        bl.addWidget(self.cancel_btn)
        bl.addWidget(self.build_btn)

        page = QWidget()
        page.setObjectName("page")
        pl = QVBoxLayout(page)
        pl.setContentsMargins(18, 14, 18, 10)
        pl.addLayout(head)
        pl.addSpacing(10)
        pl.addWidget(split, 1)
        central = QWidget()
        cl = QVBoxLayout(central)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(0)
        cl.addWidget(page, 1)
        cl.addWidget(bar)
        self.setCentralWidget(central)
        self._menus()
        self.show_page(0)
        self.set_busy(True, "Getting ready…")
        self.load_firmware()
        if self.settings.load_warning:
            # After the window is shown, so the message has a parent on screen.
            QTimer.singleShot(0, lambda: QMessageBox.warning(self, "InfiniTime Builder", self.settings.load_warning))

    # ---- construction helpers
    def _apply_style(self):
        dark = is_dark(self)
        win = self.palette().color(QPalette.ColorRole.Window)
        txt = self.palette().color(QPalette.ColorRole.WindowText)
        self.setStyleSheet(STYLE % {
            "chip": mix(win, txt, 0.10).name(),
            "muted": mix(win, txt, 0.55).name(),
            "accent": ACCENT.name(),
            "accent_hover": ACCENT.lighter(115).name(),
            "accent_tint": mix(win, ACCENT, 0.16).name(),
            "accent_text": (ACCENT.lighter(150) if dark else ACCENT).name(),
            "bad": (BAD.lighter(130) if dark else BAD).name(),
            "bad_tint": mix(win, BAD, 0.12).name(),
            "bar": mix(win, txt, 0.04 if not dark else 0.06).name(),
            "line": mix(win, txt, 0.15).name(),
            "card": mix(win, txt, 0.04).name(),
        })

    def _menus(self):
        m = self.menuBar().addMenu("&File")
        for text, fn, key in (("Import profile…", self.import_profile, None),
                              ("Export profile…", self.export_profile, None),
                              (None, None, None),
                              ("Reset to the minimal selection", self.reset_minimal, None),
                              ("Use InfiniTime's default selection", self.reset_infinitime, None),
                              (None, None, None),
                              ("Change output folder…", self.change_output, None),
                              ("Re-measure app sizes", lambda: self.start_calibration(force=True), None),
                              (None, None, None),
                              ("Quit", self.close, QKeySequence.StandardKey.Quit)):
            if text is None:
                m.addSeparator()
                continue
            a = QAction(text, self)
            a.triggered.connect(fn)
            if key:
                a.setShortcut(key)
            m.addAction(a)
        h = self.menuBar().addMenu("&Help")
        a = QAction("How to install the firmware on the watch", self)
        a.triggered.connect(self.show_install_help)
        h.addAction(a)
        a = QAction("About the size estimate", self)
        a.triggered.connect(lambda: QMessageBox.information(self, "Size estimate", SIZE_HELP))
        h.addAction(a)
        a = QAction("Writing your own app", self)
        a.triggered.connect(lambda: QDesktopServices.openUrl(QUrl(
            "https://github.com/ScanuNicco/infinitime-builder/blob/main/docs/app-repositories.md")))
        h.addAction(a)
        h.addSeparator()
        a = QAction("About InfiniTime Builder", self)
        a.triggered.connect(self.show_about)
        h.addAction(a)

    def show_about(self):
        from .qt import BINDING

        QMessageBox.about(self, "About InfiniTime Builder", (
            f"<b>InfiniTime Builder {__version__}</b><br>"
            "Choose apps and watch faces for your PineTime and build InfiniTime firmware.<br><br>"
            f"Qt binding: {BINDING}<br>"
            "Licence: GPL-3.0-or-later<br>"
            "<a href='https://github.com/ScanuNicco/infinitime-builder'>github.com/ScanuNicco/infinitime-builder</a>"))

    def _sources_page(self) -> QWidget:
        w = QWidget()
        self.src_list = QListWidget()
        self.src_list.setAlternatingRowColors(False)
        self.src_list.setSpacing(4)
        self.src_list.itemDoubleClicked.connect(self.open_source_homepage)
        self.src_input = QLineEdit()
        self.src_input.setPlaceholderText("GitHub repository — owner/repo or https://github.com/owner/repo")
        self.src_input.returnPressed.connect(self.add_source)
        self.src_ref = QLineEdit()
        self.src_ref.setPlaceholderText("branch / tag (optional)")
        self.src_ref.setMaximumWidth(190)
        add = QPushButton("Add")
        add.setDefault(True)
        add.clicked.connect(self.add_source)
        local = QPushButton("Add local folder…")
        local.clicked.connect(self.add_local_source)
        find = QPushButton("Find apps on GitHub…")
        find.clicked.connect(self.discover)
        rem = QPushButton("Remove selected")
        rem.clicked.connect(self.remove_source)
        upd = QPushButton("Check for updates")
        upd.clicked.connect(lambda: self.reload_sources(refresh=True))
        row = QHBoxLayout()
        row.addWidget(self.src_input, 1)
        row.addWidget(self.src_ref)
        row.addWidget(add)
        row2 = QHBoxLayout()
        for b in (find, local, upd):
            row2.addWidget(b)
        row2.addStretch()
        row2.addWidget(rem)
        t = QLabel("Third-party apps")
        t.setObjectName("sectionTitle")
        intro = QLabel("Add apps and watch faces made by other people. A source is a GitHub repository with an "
                       "<code>infinitime-app.json</code> manifest; its apps then appear in the Apps and Watch faces "
                       "pages, switched off.<br><b>Third-party code runs on your watch — only add sources you trust."
                       "</b>")
        intro.setObjectName("hint")
        intro.setWordWrap(True)
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(t)
        lay.addWidget(intro)
        lay.addSpacing(6)
        lay.addLayout(row)
        lay.addLayout(row2)
        lay.addWidget(self.src_list, 1)
        return w

    def show_page(self, i: int):
        self.pages.setCurrentIndex(i)
        for j, b in enumerate(self.nav_buttons):
            b.setChecked(i == j)

    # ---- state
    def set_busy(self, busy: bool, msg: str = "", cancellable: bool = False):
        self.busy = busy
        self.cancel_btn.setVisible(busy and cancellable)
        self.pages.setEnabled(not (busy and cancellable))
        self.update_build_button()
        if msg:
            self.status.setText(msg)

    def update_build_button(self):
        ok = not self.busy and self.project.catalog is not None and not self.estimate_over
        self.build_btn.setEnabled(ok)
        self.build_btn.setText("Building…" if self.busy and self.cancel_btn.isVisible() else "Build firmware")

    def run(self, fn, done, failed=None, msg="", cancellable=False, title=""):
        def finish():
            self.op = None
            self.flush_log()
            self.set_busy(False)
            self.refresh_progress()

        self.task = Task(fn, lambda r: (finish(), done(r)), lambda m: (finish(), (failed or self.show_error)(m)),
                         self.append_log, self.set_op_progress)
        self.op = Progress(title or msg.rstrip("…."))
        self.set_busy(True, msg, cancellable)
        self.refresh_progress()
        self.pool.start(self.task)

    def set_op_progress(self, pct: int):
        if self.op:
            self.op.pct = pct
            self.refresh_progress()

    def set_calib_progress(self, pct: int):
        if self.calib_op:
            self.calib_op.pct = pct
            self.refresh_progress()

    def refresh_progress(self):
        """While an operation runs (the foreground one first, else the size measurement), the flash
        meter shows its progress; afterwards it goes back to showing flash usage."""
        op = self.op or self.calib_op
        if op is None:
            self.progress_timer.stop()
            self.progress_text.setText("")
            if self.meter.progress_active:
                self.meter.set_progress(False)
                self.update_sizes()
            return
        if not self.progress_timer.isActive():
            self.progress_timer.start()
        self.meter.set_progress(True, op.pct)
        self.meter_text.setText(op.label)
        parts = [f"{op.pct}%" if op.pct is not None else "", fmt_elapsed(time.monotonic() - op.start)]
        self.progress_text.setText("  ·  ".join(p for p in parts if p))

    def append_log(self, line: str):
        # progress messages are prefixed with STATUS so they also update the status line; they
        # start a new step, whose percentage (if any) is not known yet
        if line.startswith(STATUS):
            line = line[1:]
            self.status.setText(line)
            if self.op:
                self.op.pct = None
                self.refresh_progress()
        self._log_buf.append(line)
        if not self.log_timer.isActive():
            self.log_timer.start()

    def flush_log(self):
        if self._log_buf:
            text = "\n".join(self._log_buf)
            self._log_buf.clear()
            self.log.appendPlainText(text)

    def clear_log(self):
        self._log_buf.clear()
        self.log.clear()

    def show_error(self, msg: str):
        self.status.setText(f"<span style='color:{BAD.name()}'>" + msg.splitlines()[0] + "</span>")
        QMessageBox.warning(self, "InfiniTime Builder", msg)

    def save(self):
        self.project.set_selected("app", self.apps.selected())
        self.project.set_selected("watchface", self.faces.selected())
        self.persist()

    def persist(self):
        """Save the settings; a failure (e.g. disk full) is shown rather than raised."""
        try:
            save_settings(self.settings)
        except OSError as e:
            self.status.setText(f"<span style='color:{BAD.name()}'>Could not save settings: {e}</span>")

    def on_changed(self):
        self.save()
        self.open_btn.hide()
        self.update_sizes()

    # ---- loading
    def load_firmware(self, refresh=False):
        settings = self.settings
        self.stop_calibration()

        def work(log, cancelled):
            try:
                engine, err = bld.find_engine(settings.engine), ""
            except bld.BuildError as e:
                engine, err = None, str(e)
            ref = resolve_ref(settings)
            status = lambda m: log(STATUS + m)  # noqa: E731
            path = bld.ensure_firmware(data_dir() / "cache", settings.firmware_url, ref, engine, status, refresh)
            project = Project(settings, self.project.store)
            project.set_firmware(path, ref)
            project.load_all_sources(status)
            return engine, err, ref, project

        def done(res):
            self.engine, self.engine_error, ref, self.project = res
            global ICON_FAMILY
            if ICON_FAMILY is None:
                fid = QFontDatabase.addApplicationFont(str(self.project.firmware_dir / fw.FA_FONT))
                fams = QFontDatabase.applicationFontFamilies(fid) if fid >= 0 else []
                ICON_FAMILY = fams[0] if fams else ""
            cat = self.project.catalog
            self.apps.preview.per_page = cat.apps_per_page
            fork = "" if "InfiniTimeOrg/InfiniTime" in self.settings.firmware_url else "  ·  fork"
            self.fw_chip.setText(f"InfiniTime {cat.version}{fork}  ▾")
            self.fw_chip.setToolTip(f"{self.settings.firmware_url} @ {ref or 'default branch'}\nClick to change")
            self.show_engine()
            self.populate()
            self.set_busy(False, "Ready. Switch apps on or off, drag to reorder, then build."
                          if self.engine else "Docker is needed to build: " + self.engine_error)
            self.load_size_profile()

        def failed(msg):
            self.fw_chip.setText("Firmware: could not load  ▾")
            self.show_error("Could not get the InfiniTime source:\n" + msg)

        self.run(work, done, failed, "Downloading InfiniTime source (first run only)…", title="Loading InfiniTime")

    def show_engine(self):
        if self.engine:
            self.engine_chip.setText(f"● {self.engine.name}")
            self.engine_chip.setStyleSheet(f"color: {ACCENT.name()}")
            self.engine_chip.setToolTip("Ready to build")
        else:
            self.engine_chip.setText("● Docker not available")
            self.engine_chip.setStyleSheet(f"color: {BAD.name()}")
            self.engine_chip.setToolTip(self.engine_error)

    def populate(self):
        self.apps.populate(self.project.entries("app"), self.project.selected("app"))
        self.faces.populate(self.project.entries("watchface"), self.project.selected("watchface"))
        self.src_list.clear()
        if not self.settings.sources:
            it = QListWidgetItem("No sources yet. Add a GitHub repository above, or use 'Find apps on GitHub'.")
            it.setFlags(Qt.ItemFlag.NoItemFlags)
            self.src_list.addItem(it)
        for spec in self.settings.sources:
            pkg = self.project.packages.get(spec.id)
            label = spec.location + (f" @ {spec.ref}" if spec.ref else "")
            if pkg:
                kinds = ", ".join(f"{it.name} ({'watch face' if it.kind == 'watchface' else 'app'})" for it in pkg.items)
                text = f"{pkg.name}   —   {label}\n{kinds}"
            else:
                text = f"{label}\nerror: {self.project.package_errors.get(spec.id, 'not loaded')}"
            it = QListWidgetItem(text)
            it.setData(KEY_ROLE, spec.id)
            home = self.source_homepage(spec)
            if home:
                it.setToolTip(f"{home}\nDouble-click to open")
            if not pkg:
                it.setForeground(BAD)
            self.src_list.addItem(it)
        self.update_sizes()

    # ---- sizes
    def load_size_profile(self):
        if not self.project.catalog:
            return
        prof = sizes.SizeProfile.load(self.project.size_profile_path())
        if prof:
            self.size_profile, self.size_state = prof, "ready"
            self.update_sizes()
        else:
            # keep showing an older profile for the built-in items while re-measuring
            self.start_calibration()

    def schedule_calibration(self):
        """Re-measure shortly; several quick changes (e.g. adding sources) result in one measurement."""
        self.size_state = "measuring"
        self.update_sizes()
        self.calib_timer.start()

    def stop_calibration(self):
        if self.calib_task:
            self.calib_task.cancelled = True
            self.calib_task = None
        self.calib_op = None
        self.refresh_progress()

    def start_calibration(self, force=False):
        if not self.project.catalog:
            return
        if not self.engine:
            self.size_state = "failed"
            self.update_sizes()
            return
        if self.building:
            # One compile at a time: two at once can run a small computer out of memory.
            self.calib_resume = True
            self.size_state = "paused"
            self.update_sizes()
            return
        if self.calib_task and not force:
            return
        self.stop_calibration()
        project, engine = self.project, self.engine
        path = project.size_profile_path()
        catalog, firmware_dir, external = project.catalog, project.firmware_dir, project.external_items()
        url, ref = self.settings.firmware_url, project.firmware_ref

        def work(log, cancelled):
            # own folder per run: a cancelled run may still be cleaning up while the next one starts
            work_dir = data_dir() / "work" / f"calibrate-{uuid.uuid4().hex[:8]}"
            try:
                prof = bld.calibrate(firmware_dir, catalog, external, engine, work_dir, log, cancelled,
                                     output=lambda line: log(PROGRESS_ONLY + line), firmware_url=url,
                                     firmware_ref=ref)
            finally:
                shutil.rmtree(work_dir, ignore_errors=True)
            prof.fingerprint = path.stem
            prof.save(path)
            return prof

        def done(prof):
            if self.calib_task is not task:
                return
            self.stop_calibration()
            if prof.fingerprint == self.project.size_fingerprint():
                self.size_profile, self.size_state = prof, "ready"
            self.update_sizes()
            if prof.failed:
                self.status.setText("Some third-party apps could not be compiled for this firmware; "
                                    "their size is unknown and building with them will likely fail.")

        def failed(msg):
            if self.calib_task is not task:
                return
            self.stop_calibration()
            self.size_state = "failed"
            self.update_sizes()
            self.append_log("Size measurement failed:\n" + msg)

        task = Task(work, done, failed, self.append_log, self.set_calib_progress)
        self.calib_task = task
        self.calib_op = Progress("Measuring app sizes in the background — you can keep choosing apps")
        self.size_state = "measuring"
        self.update_sizes()
        self.refresh_progress()
        self.pool.start(task)

    def update_sizes(self):
        """Refresh the flash meter and each row's size."""
        enabled = set(self.apps.selected()) | set(self.faces.selected())
        prof = self.size_profile
        limit = prof.flash_limit if prof else sizes.DEFAULT_FLASH_LIMIT
        self.estimate_over = False
        if prof and self.size_state in ("ready", "measuring", "paused"):
            used, unknown = prof.estimate(enabled)
            self.meter.set(used, limit)
            free = limit - used
            unk = f"  ·  +{len(unknown)} not measured yet" if unknown else ""
            if free >= 0:
                self.meter_text.setText(f"Flash: about {fmt_kb(used)} of {fmt_kb(limit)}  ·  {fmt_kb(free)} free{unk}")
            elif -free <= limit * SIZE_TOLERANCE:
                self.meter_text.setText(f"Flash: about {fmt_kb(used)} of {fmt_kb(limit)}  ·  right at the limit — "
                                        f"the build will tell for sure{unk}")
            else:
                self.estimate_over = True
                self.meter_text.setText(f"<span style='color:{BAD.name()}'>Too big: about {fmt_kb(-free)} over "
                                        f"the {fmt_kb(limit)} limit — switch something off</span>")
            if self.size_state == "measuring":
                self.meter_text.setText(self.meter_text.text() + "  ·  re-measuring…")
            elif self.size_state == "paused":
                self.meter_text.setText(self.meter_text.text() + "  ·  re-measuring after the build")
            self.update_resource_text(prof, enabled)
            subs = {}
            for page in (self.apps, self.faces):
                for k, e in page.entries.items():
                    c = prof.cost(k, enabled)
                    size = f"~{fmt_kb(c)}" if c is not None else "size not measured yet"
                    res = prof.resource_cost(k)
                    pack = f"  ·  {fmt_kb(res)} in resource pack" if res else ("  ·  uses resource pack" if e.needs_resources else "")
                    subs[k] = f"{e.origin}  ·  {size}{pack}"
                page.set_subtitles(subs)
        else:
            self.meter.set(None, limit)
            self.res_text.setText("")
            self.meter_text.setText({
                "measuring": "Flash: measuring how much space each app needs (first time only; a few minutes on "
                             "slower computers). You can keep choosing apps meanwhile.",
                "paused": "Flash: size measurement paused while building — the build checks the size exactly",
                "failed": "Flash: size estimate unavailable — the build will still check that it fits",
                "none": "Flash: —",
            }[self.size_state])
            for page in (self.apps, self.faces):
                page.set_subtitles({k: e.origin + ("  ·  uses resource pack" if e.needs_resources else "")
                                    for k, e in page.entries.items()})
        for page in (self.apps, self.faces):
            page.on_list.viewport().update()
            page.off_list.viewport().update()
        self.update_build_button()
        if self.op or self.calib_op:
            self.refresh_progress()  # the meter is showing an operation's progress

    def update_resource_text(self, prof: sizes.SizeProfile, enabled: set[str]):
        if not prof.resource_sizes:
            self.res_text.setText("")
            return
        nbytes, blocks, capacity = prof.estimate_resources(enabled)
        unknown = [k for k in enabled if k.startswith("ext:") and not prof.known(k)]
        stored, room = blocks * prof.fs_block_size, capacity * prof.fs_block_size
        if blocks <= capacity:
            self.res_text.setText(f"Resource pack: about {fmt_kb(stored)} of the {fmt_kb(room)} the watch has room for"
                                  + (f"  ·  +{len(unknown)} not measured yet" if unknown else ""))
            self.res_text.setStyleSheet("")
        else:
            self.res_text.setText(f"Resource pack too big: about {fmt_kb(stored)}, but the watch only has room for "
                                  f"{fmt_kb(room)}. Switch off faces or apps with large fonts or images.")
            self.res_text.setStyleSheet(f"color: {BAD.name()}")

    # ---- sources
    def _add_spec(self, spec: SourceSpec):
        if any(s.id == spec.id for s in self.settings.sources):
            QMessageBox.information(self, "InfiniTime Builder", "That source is already added.")
            return

        def done(pkg):
            self.save()
            self.settings.sources.append(spec)
            self.persist()
            self.populate()
            names = ", ".join(i.name for i in pkg.items)
            self.status.setText(f"Added {pkg.name}: {names}. Switch them on in Apps / Watch faces.")
            self.src_input.clear()
            self.src_ref.clear()
            self.schedule_calibration()

        self.run(lambda log, c: self.project.load_source(spec), done,
                 lambda m: self.show_error(f"Could not add {spec.location}:\n{m}"), f"Fetching {spec.location}…")

    def source_homepage(self, spec: SourceSpec) -> str | None:
        """The manifest's homepage, else the GitHub page of a GitHub source."""
        pkg = self.project.packages.get(spec.id)
        if pkg and pkg.homepage:
            return pkg.homepage
        return f"https://github.com/{spec.location}" if spec.kind == "github" else None

    def open_source_homepage(self, item: QListWidgetItem):
        sid = item.data(KEY_ROLE)
        spec = next((s for s in self.settings.sources if s.id == sid), None)
        url = self.source_homepage(spec) if spec else None
        if url:
            QDesktopServices.openUrl(QUrl(url))

    def add_source(self):
        text = self.src_input.text().strip()
        if not text:
            return
        try:
            self._add_spec(SourceSpec.parse(text, self.src_ref.text()))
        except FetchError as e:
            self.show_error(str(e))

    def add_local_source(self):
        d = QFileDialog.getExistingDirectory(self, "Folder containing infinitime-app.json")
        if d:
            self._add_spec(SourceSpec("local", str(Path(d).resolve())))

    def discover(self):
        dlg = DiscoverDialog(self, self.pool)
        if dlg.exec() and dlg.chosen():
            self._add_spec(SourceSpec("github", dlg.chosen()))

    def remove_source(self):
        it = self.src_list.currentItem()
        sid = it.data(KEY_ROLE) if it else None
        if not sid:
            return
        self.save()
        spec = next(s for s in self.settings.sources if s.id == sid)
        self.settings.sources.remove(spec)
        self.project.packages.pop(sid, None)
        self.project.store.remove(spec)
        prefix = f"ext:{sid}#"
        for kind in ("app", "watchface"):
            self.project.set_selected(kind, [k for k in self.project.selected(kind) if not k.startswith(prefix)])
        self.persist()
        self.populate()
        self.load_size_profile()

    def reload_sources(self, refresh=False):
        self.save()

        def work(log, c):
            for spec in self.settings.sources:
                try:
                    self.project.load_source(spec, refresh=refresh)
                except (FetchError, ManifestError, OSError) as e:
                    log(f"{spec.id}: {e}")

        def done(_):
            self.populate()
            self.status.setText("Sources updated.")
            self.load_size_profile()

        self.run(work, done, msg="Updating sources…", title="Updating sources")

    # ---- firmware / profile
    def change_firmware(self):
        dlg = FirmwareDialog(self, self.settings)
        if dlg.exec():
            self.save()
            self.settings.firmware_url = dlg.url.text().strip() or bld.DEFAULT_FIRMWARE_URL
            self.settings.firmware_ref = dlg.ref.currentText().strip()
            self.persist()
            self.size_profile, self.size_state = None, "none"
            self.load_firmware(refresh=self.settings.firmware_ref in ("main", "develop"))

    def reset_minimal(self):
        self.settings.apps = None
        self.settings.watchfaces = None
        self.persist()
        self.populate()

    def reset_infinitime(self):
        self.project.use_infinitime_defaults()
        self.persist()
        self.populate()

    def export_profile(self):
        self.save()
        fn, _ = QFileDialog.getSaveFileName(self, "Export profile", "my-infinitime.json", "Profile (*.json)")
        if fn:
            try:
                atomic_write_text(Path(fn), json.dumps(self.settings.to_json(include_local=False), indent=2))
            except OSError as e:
                self.show_error(f"Could not save the profile: {e}")
                return
            self.status.setText(f"Profile saved to {fn}. Share it so others can build the same firmware.")

    def import_profile(self):
        fn, _ = QFileDialog.getOpenFileName(self, "Import profile", "", "Profile (*.json)")
        if not fn:
            return
        try:
            data = json.loads(Path(fn).read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("format") != "infinitime-builder-profile":
                raise ValueError("not an InfiniTime Builder profile")
            local = [s for s in self.settings.sources if s.kind == "local"]
            new = Settings.from_json(data, base=self.settings)
            new.sources += [s for s in local if s not in new.sources]
        except (OSError, ValueError, KeyError) as e:
            self.show_error(f"Could not import profile: {e}")
            return
        self.settings.__dict__.update(new.__dict__)
        self.persist()
        self.load_firmware()

    def change_output(self):
        d = QFileDialog.getExistingDirectory(self, "Where to save built firmware", self.settings.output_dir)
        if d:
            self.settings.output_dir = d
            self.persist()

    # ---- build
    def start_build(self):
        self.save()
        req, errors, warnings = self.project.build_request()
        if errors:
            self.show_error("\n".join(errors))
            return
        if warnings and QMessageBox.question(self, "InfiniTime Builder", "\n".join(warnings) + "\n\nBuild anyway?") \
                != QMessageBox.StandardButton.Yes:
            return
        if self.calib_task:
            # The build checks the size exactly; measuring at the same time would only make both
            # slower and could run a small computer out of memory. It resumes after the build.
            self.stop_calibration()
            self.calib_resume = True
            self.size_state = "paused"
            self.update_sizes()
        self.clear_log()
        self.open_btn.hide()
        engine, pref, out = self.engine, self.settings.engine, Path(self.settings.output_dir).expanduser()
        found: list[bld.Engine] = []

        def work(log, cancelled):
            eng = engine
            if eng is None:  # not found at startup; `docker info` can take a while, so not on the GUI thread
                log(STATUS + "Looking for Docker or Podman…")
                eng = bld.find_engine(pref)
                found.append(eng)
            return bld.build(req, eng, data_dir() / "work", out, lambda m: log(STATUS + m), cancelled, output=log)

        def after():
            self.building = False
            if found:
                self.engine, self.engine_error = found[0], ""
                self.show_engine()
            if self.calib_resume:
                self.calib_resume = False
                self.start_calibration(force=True)

        self.building = True
        self.run(work, lambda r: (after(), self.build_done(r)), lambda m: (after(), self.build_failed(m)),
                 "Starting build…", cancellable=True, title="Building firmware")

    def cancel(self):
        if self.task:
            self.task.cancelled = True
            self.status.setText("Stopping…")

    def build_done(self, res: bld.BuildResult):
        self.last_result = res
        usage = ""
        used = ""
        if res.flash_used and res.flash_size:
            self.meter.set(res.flash_used, res.flash_size)
            if not self.calib_op:  # else the meter is showing the resumed size measurement
                self.meter_text.setText(f"Flash: {fmt_kb(res.flash_used)} of {fmt_kb(res.flash_size)}  ·  "
                                        f"{fmt_kb(res.flash_size - res.flash_used)} free  (exact, from this build)")
            usage = f"It uses {fmt_kb(res.flash_used)} of the watch's {fmt_kb(res.flash_size)} of flash."
            used = f"  ·  {fmt_kb(res.flash_used)} of {fmt_kb(res.flash_size)} flash"
        self.status.setText(f"<b style='color:{ACCENT.name()}'>Done in {res.seconds:.0f}s</b>{used}  ·  {res.dfu.name}")
        self.open_btn.show()
        res_warning = "" if res.resources is None or res.resources_fit else (
            "<p style='color:%s'><b>Warning:</b> %s</p>" % (BAD.name(), bld.resource_warning(
                res.resource_blocks, res.resource_capacity, res.resource_bytes)))
        if res_warning:
            self.res_text.setText("Resource pack too big for the watch (exact, from this build)")
            self.res_text.setStyleSheet(f"color: {BAD.name()}")
        box = QMessageBox(self)
        box.setWindowTitle("Firmware ready")
        box.setText(f"<b>Your firmware is ready.</b><br>{res.dfu}<br>{usage}{res_warning}")
        users = self.project.resource_users()
        help_text = INSTALL_HELP
        if users and res.resources:
            help_text = (f"{', '.join(users)} also need{'s' if len(users) == 1 else ''} the resource pack: after the "
                         f"firmware, send {res.resources.name} to the watch too (Gadgetbridge, InfiniLink and ITD can "
                         "do this). Without it, these show greyed out.\n\n") + INSTALL_HELP_BASE
        else:
            help_text = INSTALL_HELP_BASE
        box.setInformativeText(help_text)
        box.addButton("Open folder", QMessageBox.ButtonRole.AcceptRole).clicked.connect(self.open_output)
        box.addButton(QMessageBox.StandardButton.Close)
        box.exec()

    def build_failed(self, msg: str):
        self.log_btn.setChecked(True)
        self.show_error(msg)

    def open_output(self):
        if self.last_result:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.last_result.output_dir)))

    def show_install_help(self):
        QMessageBox.information(self, "Installing the firmware", INSTALL_HELP)

    def closeEvent(self, ev):
        self.save()
        for t in (self.task, self.calib_task):
            if t:
                t.cancelled = True
        super().closeEvent(ev)


INSTALL_HELP_BASE = (
    "Send the pinetime-mcuboot-app-dfu-*.zip file to your watch with a companion app:\n"
    "  • Android: Gadgetbridge (Settings › File installer) or InfiniLink\n"
    "  • iOS: InfiniLink\n"
    "  • Linux: Siglo, ITD or Amazfish\n\n"
    "After the update, open Settings › Firmware on the watch and press Validate, or it will revert on the next reboot."
)
INSTALL_HELP = INSTALL_HELP_BASE + (
    "\n\nWatch faces and apps marked 'uses resource pack' also need the infinitime-resources-*.zip file "
    "sent to the watch (once per firmware version); until then they show greyed out."
)

SIZE_HELP = (
    "The PineTime has 464 KB of flash for the firmware.\n\n"
    "To estimate how big your selection will be, InfiniTime Builder compiles the firmware once with everything "
    "switched on and records how much space each part of the code takes and what it depends on. This takes about "
    "a minute and is repeated only when you change the firmware version or app sources.\n\n"
    "The estimate errs slightly on the high side (within about 1%). Every build is also checked exactly against "
    "the watch's limits, and firmware that does not fit is never saved."
)


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("InfiniTime Builder")
    app.setApplicationVersion(__version__)
    app.setDesktopFileName(APP_ID)  # lets the desktop match the window to its .desktop entry and icon
    app.setWindowIcon(QIcon.fromTheme(APP_ID, QIcon(str(Path(__file__).with_name("icon.svg")))))
    app.setStyle("Fusion")
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
