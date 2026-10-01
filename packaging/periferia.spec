# PyInstaller collects the interpreter, PySide6 and ruamel.yaml into one
# executable. The AppDir then holds nothing but that executable, which is what
# keeps the finished image to a single file instead of a runtime plus a payload.
#
# Excludes matter more than they look. PySide6 ships translators for dozens of
# locales and every one of them is dead weight in a single-file image. The
# pruning below is deliberately blunt: if a module fails to import, the app
# crashes at first launch on a user's machine rather than in front of us, so
# only things we are certain are unreachable are named here.

analysis = Analysis(
    ["launcher.py"],
    pathex=["../src"],
    binaries=[],
    datas=[],
    hiddenimports=[
        "periferia.core.daemon",
        "periferia.core.envcheck",
        "periferia.modules.hotkey",
        "periferia.modules.remap",
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        "PySide6.QtQml",
        "PySide6.QtQuick",
        "PySide6.QtQuick3D",
        "PySide6.Qt3DCore",
        "PySide6.QtWebEngineCore",
        "PySide6.QtWebEngineWidgets",
        "PySide6.QtMultimedia",
        "PySide6.QtBluetooth",
        "PySide6.QtNetworkAuth",
        "PySide6.QtDesigner",
        "PySide6.QtHelp",
        "PySide6.QtTest",
        "tkinter",
        "unittest",
        "pydoc",
        "numpy",
    ],
    noarchive=False,
)

pyz = PYZ(analysis.pure)

exe = EXE(
    pyz,
    analysis.scripts,
    analysis.binaries,
    analysis.datas,
    [],
    name="periferia-gui",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
)
