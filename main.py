"""
SnP Image Labeler
Entry point with global crash logger.
Works as a plain Python script and as a PyInstaller-bundled EXE.
"""

import os
import sys
import traceback
from datetime import datetime
from pathlib import Path

os.environ.setdefault("QT_AUTO_SCREEN_SCALE_FACTOR", "1")


def _resource(relative: str) -> str:
    """Resolve path to bundled asset (works in both source and EXE mode)."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    base = os.environ.get("SNP_BUNDLE_DIR", base)
    return os.path.join(base, relative)


def _crash_log_path() -> Path:
    if getattr(sys, "frozen", False):
        base = Path(sys.executable).parent
    else:
        base = Path(__file__).parent
    return base / "snp_crash.log"


def _install_crash_hook():
    """Route ALL unhandled exceptions to a log file + Qt dialog."""
    log_path = _crash_log_path()

    def handle(exc_type, exc_value, exc_tb):
        # Don't intercept KeyboardInterrupt
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return

        msg = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            with open(log_path, "a", encoding="utf-8") as fh:
                fh.write(f"\n{'='*60}\n[{timestamp}]\n{msg}\n")
        except Exception:
            pass

        try:
            from PyQt5.QtWidgets import QApplication, QMessageBox
            if QApplication.instance():
                box = QMessageBox()
                box.setWindowTitle("SnP Image Labeler — Crash")
                box.setIcon(QMessageBox.Critical)
                box.setText(
                    f"An unexpected error occurred.\n\n"
                    f"Crash log: {log_path}\n\n"
                    f"{exc_type.__name__}: {exc_value}"
                )
                box.setDetailedText(msg)
                box.exec_()
        except Exception:
            pass

    sys.excepthook = handle


def _set_taskbar_identity():
    """
    Windows groups taskbar buttons by AppUserModelID. Without an explicit one
    a script run is grouped under python.exe and shows the Python icon, no
    matter what the window icon is.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "SnP.ImageLabeler.Desktop.1"
        )
    except Exception:
        pass


def _application_icon():
    """Prefer the multi-resolution .ico, fall back to the 256 px PNG."""
    from PyQt5.QtGui import QIcon
    for name in ("icon.ico", "icon.png"):
        path = _resource(name)
        if os.path.isfile(path):
            icon = QIcon(path)
            if not icon.isNull():
                return icon
    return None


def main():
    _install_crash_hook()
    _set_taskbar_identity()

    from PyQt5.QtCore import Qt
    from PyQt5.QtWidgets import QApplication

    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)

    app = QApplication(sys.argv)
    app.setApplicationName("SnP Image Labeler")
    app.setApplicationVersion("1.0.0")
    app.setOrganizationName("SnP")

    icon = _application_icon()
    if icon is not None:
        app.setWindowIcon(icon)      # window, taskbar and every dialog

    qss_path = _resource("style.qss")
    try:
        with open(qss_path, "r", encoding="utf-8") as fh:
            app.setStyleSheet(fh.read())
    except OSError:
        pass

    from main_window import MainWindow
    window = MainWindow()
    window.show()

    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
