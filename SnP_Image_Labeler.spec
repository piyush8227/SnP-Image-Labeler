# -*- mode: python ; coding: utf-8 -*-
# SnP Image Labeler — PyInstaller Spec
# Produces a single-folder dist with all dependencies bundled.
# Run: pyinstaller SnP_Image_Labeler.spec

import sys
from pathlib import Path

block_cipher = None
HERE = Path(SPECPATH)   # directory containing this spec file

# ------------------------------------------------------------------ #
#  Analysis                                                            #
# ------------------------------------------------------------------ #
a = Analysis(
    [str(HERE / 'main.py')],
    pathex=[str(HERE)],
    binaries=[],
    datas=[
        # Bundle the stylesheet
        (str(HERE / 'style.qss'), '.'),
        # Bundle the icons (icon.ico is also the exe resource below)
        (str(HERE / 'icon.png'), '.'),
        (str(HERE / 'icon.ico'), '.'),
    ],
    hiddenimports=[
        # PyQt5 modules that PyInstaller may miss
        'PyQt5',
        'PyQt5.QtCore',
        'PyQt5.QtGui',
        'PyQt5.QtWidgets',
        'PyQt5.sip',
        # Standard library modules used at runtime
        'csv',
        'hashlib',
        'json',
        'pathlib',
        'shutil',
        'uuid',
        # Our own packages
        'core',
        'core.config_manager',
        'core.duplicate_finder',
        'core.label_manager',
        'core.rejection_manager',
        'core.segregation_manager',
        'core.image_cache',
        'core.thumbnail_cache',
        'core.label_index',
        'core.session_store',
        'core.undo_stack',
        'widgets',
        'widgets.image_viewer',
        'widgets.annotation_viewer',
        'widgets.image_queue',
        'widgets.telemetry_panel',
        'widgets.field_editor',
        'widgets.bbox_panel',
        'widgets.segregation_panel',
        'widgets.class_editor',
        'widgets.duplicate_dialog',
        'widgets.duplicate_scan_dialog',
        'widgets.review_grid',
        'main_window',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[str(HERE / 'runtime_hook.py')],
    excludes=[
        # Trim unused heavy packages
        'matplotlib',
        'numpy',
        'pandas',
        'scipy',
        'PIL',
        'cv2',
        'sklearn',
        'tensorflow',
        'torch',
        'tkinter',
        '_tkinter',
        'PyQt5.QtWebEngine',
        'PyQt5.QtWebEngineWidgets',
        'PyQt5.QtNetwork',
        'PyQt5.QtBluetooth',
        'PyQt5.QtNfc',
        'PyQt5.QtPositioning',
        'PyQt5.QtLocation',
        'PyQt5.QtQuick',
        'PyQt5.QtQml',
        'PyQt5.QtSql',
        'PyQt5.QtTest',
        'PyQt5.QtXml',
        'PyQt5.QtXmlPatterns',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

# ------------------------------------------------------------------ #
#  PYZ (compressed Python bytecode)                                   #
# ------------------------------------------------------------------ #
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# ------------------------------------------------------------------ #
#  EXE                                                                 #
# ------------------------------------------------------------------ #
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,          # onedir mode (faster startup)
    name='SnP_Image_Labeler',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,                       # compress with UPX if available
    console=False,                  # no black console window on startup
    icon=str(HERE / 'icon.ico'),
    version_file=str(HERE / 'version_info.txt') if (HERE / 'version_info.txt').exists() else None,
)

# ------------------------------------------------------------------ #
#  COLLECT  (assemble the dist folder)                                 #
# ------------------------------------------------------------------ #
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='SnP_Image_Labeler',
)
