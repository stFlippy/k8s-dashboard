"""Verify final portable composition and PE closure; ZIP and verify extraction."""
import csv
import json
import os
from pathlib import Path
import sys
import tempfile
import zipfile

from policy import digest

REQUIRED = (
    ['Quaber.exe', 'python311.dll', 'VCRUNTIME140.dll', 'VCRUNTIME140_1.dll',
     'PyQt5/sip.cp311-win_amd64.pyd', 'cryptography/hazmat/bindings/_rust.pyd',
     'yaml/_yaml.cp311-win_amd64.pyd', 'public/logo.png', 'assets/fonts/OFL.txt']
    + [f'PyQt5/Qt5/bin/Qt5{n}.dll' for n in ('Core', 'Gui', 'Widgets', 'Svg', 'Network')]
    + [f'PyQt5/Qt{n}.pyd' for n in ('Core', 'Gui', 'Widgets', 'Svg')]
    + [f'PyQt5/Qt5/plugins/{p}.dll' for p in (
        'platforms/qwindows', 'iconengines/qsvgicon', 'generic/qtuiotouchplugin',
        'imageformats/qsvg', 'imageformats/qjpeg', 'imageformats/qwebp',
        'imageformats/qico', 'imageformats/qgif', 'imageformats/qicns',
        'imageformats/qtga', 'imageformats/qtiff', 'imageformats/qwbmp')])


def verify_clean_payload(root, paths):
    from policy import BUILD_ONLY_RESOURCES
    forbidden_folders = {'addons', 'dumps', 'userdata', 'reports', 'tests', 'builds', '__pycache__',
                         'tzdata', 'zoneinfo', 'dateutil', 'croniter', 'six'}
    unwanted = sorted(p.relative_to(root).as_posix() for p in root.rglob('*')
                      if p.is_dir() and p.name.casefold() in forbidden_folders)
    unwanted.extend(sorted(set(paths) & (BUILD_ONLY_RESOURCES | {'assets/logo.png'})))
    if unwanted:
        raise RuntimeError(f'Unexpected release folders/resources: {unwanted}')


def verify(root, report, profile):
    import pefile
    from policy import HERE, PROFILES, runtime_resources
    paths = {p.relative_to(root).as_posix(): p for p in root.rglob('*') if p.is_file()}
    verify_clean_payload(root, paths)
    # Build-only ICO/docs remain in source; the executable has the embedded icon.
    required = set(REQUIRED)
    required.update(runtime_resources())
    missing = required - paths.keys()
    if missing:
        raise RuntimeError(f'Missing required files: {sorted(missing)}')
    forbidden = [n for n in paths if any(part.casefold() in ('userdata', '__pycache__')
                 for part in Path(n).parts) or n.lower().endswith(('.log', '.kubeconfig'))
                 or 'qt6' in n.lower() or 'pyside' in n.lower()
                 or Path(n).name.lower() in ('kubeconfig.yaml', 'quaber.config.json')
                 or n.lower().startswith('addons/')]
    if forbidden:
        raise RuntimeError(f'Unexpected release data: {forbidden}')
    policy = json.loads((HERE / 'policy-files.json').read_text())
    excluded = {r['path'] for g in PROFILES[profile] for r in policy['groups'][g]}
    actual_qt = {n for n in paths if n.startswith('PyQt5/')}
    if actual_qt != set(policy['qt_paths']) - excluded:
        raise RuntimeError('Final Qt composition differs from policy')
    by_name = {}
    for name in paths:
        by_name.setdefault(Path(name).name.lower(), []).append(name)
    system = Path(os.environ['SystemRoot']) / 'System32'
    edges, unresolved = {}, []
    for name, path in paths.items():
        if path.suffix.lower() not in ('.dll', '.pyd', '.exe'):
            continue
        with pefile.PE(str(path), fast_load=True) as pe:
            pe.parse_data_directories(directories=[1, 13])
            imports = sorted({e.dll.decode('ascii').lower()
                              for attr in ('DIRECTORY_ENTRY_IMPORT', 'DIRECTORY_ENTRY_DELAY_IMPORT')
                              for e in getattr(pe, attr, [])})
        edges[name] = []
        for dep in imports:
            bundled = by_name.get(dep, [])
            # Native Qt searches plugins/bin itself; record exact matching destinations.
            if bundled:
                resolution = bundled
            elif dep.startswith(('api-ms-win-', 'ext-ms-win-')):
                resolution = ['windows-api-set']
            elif not dep.startswith(('qt5', 'python', 'libssl', 'libcrypto', 'vcruntime', 'msvcp')) and (system / dep).is_file():
                resolution = [str(system / dep)]
            else:
                resolution = []
                unresolved.append({'importer': name, 'dependency': dep})
            edges[name].append({'dependency': dep, 'resolved': resolution})
    (report / 'pe-closure.json').write_text(json.dumps({'edges': edges, 'unresolved': unresolved}, indent=2))
    if unresolved:
        raise RuntimeError(f'Unresolved PE imports: {unresolved}')
    rows = [{'path': n, 'bytes': p.stat().st_size, 'sha256': digest(p)} for n, p in sorted(paths.items())]
    with (report / 'files.csv').open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=['path', 'bytes', 'sha256'])
        writer.writeheader()
        writer.writerows(rows)
    archive = root.parent / f'Quaber-{profile}.zip'
    with zipfile.ZipFile(archive, 'x', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for row in rows:
            z.write(paths[row['path']], 'Quaber/' + row['path'])
    # A clean path with spaces and Cyrillic also checks Unicode ZIP names.
    with tempfile.TemporaryDirectory(prefix='Quaber проверка ') as temp:
        with zipfile.ZipFile(archive) as z:
            z.extractall(temp)
        for row in rows:
            if digest(Path(temp) / 'Quaber' / row['path']) != row['sha256']:
                raise RuntimeError(f'ZIP roundtrip mismatch: {row["path"]}')
    result = {'profile': profile, 'bytes': sum(r['bytes'] for r in rows),
              'zip_bytes': archive.stat().st_size, 'zip_sha256': digest(archive),
              'excluded_directories': ['Addons', 'UserData'], 'files': len(rows),
              'acceptance': 'candidate; functional/environment acceptance still required'}
    (report / 'size.json').write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))

if __name__ == '__main__':
    verify(Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3])
