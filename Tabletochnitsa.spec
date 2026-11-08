# -*- mode: python ; coding: utf-8 -*-
# Сборка Tabletochnitsa.app (запускается из build_app.command на Mac).

a = Analysis(
    ['tabletochnitsa.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=['certifi'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='Tabletochnitsa',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    argv_emulation=False,
    target_arch=None,          # 'universal2' — одна сборка для M1–M4 и Intel (нужен Python с python.org)
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='Tabletochnitsa')

app = BUNDLE(
    coll,
    name='Tabletochnitsa.app',
    icon=None,
    bundle_identifier='local.tabletochnitsa',
    info_plist={
        'CFBundleShortVersionString': '1.0',
        'NSHighResolutionCapable': True,
        'NSRequiresAquaSystemAppearance': False,   # поддержка тёмной темы
        'NSAppSleepDisabled': True,                # App Nap не усыпляет таймер напоминаний
    },
)
