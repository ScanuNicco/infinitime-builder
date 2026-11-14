"""The Qt binding: PySide6 if installed, otherwise PyQt6.

Both are Python bindings for Qt 6 and work the same for this app, as long as enums are written in
full (Qt.AlignmentFlag.AlignCenter, not Qt.AlignCenter), which both accept. Having both matters for
packaging: Ubuntu 24.04 ships PyQt6 but not PySide6.
"""
try:
    from PySide6.QtCore import *  # noqa: F401,F403
    from PySide6.QtGui import *  # noqa: F401,F403
    from PySide6.QtWidgets import *  # noqa: F401,F403

    BINDING = "PySide6"
except ImportError:
    from PyQt6.QtCore import *  # noqa: F401,F403
    from PyQt6.QtCore import pyqtSignal as Signal, pyqtSlot as Slot  # noqa: F401
    from PyQt6.QtGui import *  # noqa: F401,F403
    from PyQt6.QtWidgets import *  # noqa: F401,F403

    BINDING = "PyQt6"
