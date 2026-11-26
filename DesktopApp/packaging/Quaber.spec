# -*- mode: python ; coding: utf-8 -*-
import os
import sys
from pathlib import Path

root = Path(SPECPATH).parent
sys.path.insert(0, str(root / 'packaging'))
from policy import apply_policy, runtime_resources

profile = os.environ.get('QUABER_BUILD_PROFILE', 'baseline')
a = Analysis([str(root / 'main.py')], pathex=[str(root)], binaries=[],
             datas=[(str(path), str(Path(name).parent)) for name, path in runtime_resources().items()],
             hiddenimports=[], hookspath=[], hooksconfig={}, runtime_hooks=[str(root/'packaging/runtime_schedule_check.py')],
             excludes=['tzdata', 'zoneinfo', 'croniter', 'dateutil', 'six'], noarchive=False, optimize=0)
a.binaries, a.datas = apply_policy(a.binaries, a.datas, profile,
                                  Path(os.environ['QUABER_BUILD_REPORT']))
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='Quaber',
          icon=str(root / 'public' / 'icon.ico'),
          debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
          console=False, disable_windowed_traceback=False, argv_emulation=False,
          target_arch=None, codesign_identity=None, entitlements_file=None,
          contents_directory='.')
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='Quaber')
