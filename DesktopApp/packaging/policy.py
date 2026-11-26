"""Fail-closed Windows packaging policy; never edits an installed bundle."""
import hashlib
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROFILES = {
    'baseline': (),
    'minimal-candidate': ('webgl_qml_quick', 'alternate_platforms', 'portal_dbus'),
    'conservative-candidate': ('webgl_qml_quick', 'alternate_platforms', 'portal_dbus',
                               'translations_except_ru'),
}
REASONS = {
    'webgl_qml_quick': 'Windows Widgets application does not use WebGL, Quick or QML',
    'alternate_platforms': 'User portable uses qwindows; diagnostic runtime retains offscreen/minimal',
    'portal_dbus': 'XDG portal/DBus is not used by Windows portable',
    'translations_except_ru': 'Approved language contract: Russian and built-in English',
}

# These support development/building; they are not opened by the application.
BUILD_ONLY_RESOURCES = frozenset(('assets/fonts/README.md', 'public/icon.ico'))


def runtime_resources():
    root = HERE.parent
    return {p.relative_to(root).as_posix(): p for folder in ('assets', 'public')
            for p in sorted((root / folder).rglob('*'))
            if p.is_file() and p.relative_to(root).as_posix() not in BUILD_ONLY_RESOURCES}

def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def apply_policy(binaries, datas, profile, report):
    if profile not in PROFILES:
        raise RuntimeError(f'Unknown/unapproved build profile: {profile}')
    policy = json.loads((HERE / 'policy-files.json').read_text(encoding='utf-8'))
    if digest(HERE.parent / 'requirements.lock') != policy['lock_sha256']:
        raise RuntimeError('Lock changed: review and regenerate packaging policy')
    entries = {dst.replace('\\', '/'): (src, kind)
               for dst, src, kind in [*binaries, *datas]}
    expected_qt = set(policy['qt_paths'])
    actual_qt = {p for p in entries if p.startswith('PyQt5/')}
    if actual_qt != expected_qt:
        raise RuntimeError(f'Qt inventory changed: added={actual_qt-expected_qt}, missing={expected_qt-actual_qt}')
    removed = []
    for group in PROFILES[profile]:
        for row in policy['groups'][group]:
            dst = row['path']
            if dst not in entries:
                raise RuntimeError(f'Policy path missing: {dst}')
            src, kind = entries[dst]
            if Path(src).stat().st_size != row['bytes'] or digest(src) != row['sha256']:
                raise RuntimeError(f'Policy file changed: {dst}')
            removed.append(dict(row, source=str(src), destination=dst, group=group,
                                reason=REASONS[group]))
    excluded = {r['path'] for r in removed}
    report.mkdir(parents=True, exist_ok=True)
    (report / 'exclusions.json').write_text(json.dumps({
        'profile': profile, 'groups': list(PROFILES[profile]), 'files': removed,
        'bytes': sum(r['bytes'] for r in removed)}, indent=2), encoding='utf-8')
    def keep(toc):
        return [row for row in toc if row[0].replace('\\', '/') not in excluded]
    return keep(binaries), keep(datas)
