"""Check the pinned build environment and inventory release artifacts."""
import hashlib
from importlib.metadata import PackageNotFoundError, distributions, version
import json
import os
from datetime import datetime, timezone
from pathlib import Path
import sys


TARGET_QT = {
    "PyQt5": "5.15.9",
    "PyQt5-Qt5": "5.15.2",
    "PyQt5-sip": "12.18.0",
}
ALTERNATIVE_QT_BINDINGS = ("PyQt6", "PySide6", "PySide2")


def _inside(path, root):
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except ValueError:
        return False


def check_environment():
    if sys.version_info[:2] != (3, 11):
        raise RuntimeError("Release build requires Python 3.11 x64.")
    if sys.maxsize <= 2**32:
        raise RuntimeError("Release build requires a 64-bit interpreter.")
    lock = Path(__file__).with_name("requirements.lock")
    for line in lock.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        name, expected = line.split("==", 1)
        if version(name) != expected:
            raise RuntimeError(f"Build dependency differs from lock: {name}")

    for name, expected in TARGET_QT.items():
        if version(name) != expected:
            raise RuntimeError(f"Release requires {name}=={expected}.")
    for name in ALTERNATIVE_QT_BINDINGS:
        try:
            installed = version(name)
        except PackageNotFoundError:
            continue
        raise RuntimeError(f"Alternative Qt binding must not be installed: {name}=={installed}")

    import PyQt5
    from PyQt5 import QtCore, sip
    from PyQt5.QtCore import QLibraryInfo

    if QtCore.PYQT_VERSION_STR != TARGET_QT["PyQt5"]:
        raise RuntimeError(f"Unexpected PyQt binding: {QtCore.PYQT_VERSION_STR}")
    if QtCore.qVersion() != TARGET_QT["PyQt5-Qt5"]:
        raise RuntimeError(f"Unexpected loaded Qt runtime: {QtCore.qVersion()}")

    prefix = Path(sys.prefix).resolve()
    imported = (PyQt5.__file__, QtCore.__file__, sip.__file__)
    if any(not _inside(path, prefix) for path in imported):
        raise RuntimeError(f"PyQt5 was imported outside the release environment: {imported}")
    qt_paths = (
        QLibraryInfo.location(QLibraryInfo.LibrariesPath),
        QLibraryInfo.location(QLibraryInfo.PluginsPath),
    )
    if any(not _inside(path, prefix) for path in qt_paths):
        raise RuntimeError(f"Qt libraries/plugins resolve outside the release environment: {qt_paths}")


def inventory(root):
    from PyQt5 import QtCore, sip
    from PyQt5.QtCore import QLibraryInfo

    files = {str(path.relative_to(root).as_posix()): hashlib.sha256(path.read_bytes()).hexdigest()
             for path in sorted(root.rglob("*")) if path.is_file() and path.name not in ("release-manifest.json", "sbom.json")}
    # Build-environment inventory is distinct from components actually shipped.
    runtime_names = {'pyqt5', 'pyqt5-qt5', 'pyqt5-sip', 'pyyaml', 'cryptography', 'cffi'}
    environment = [{"type": "library", "name": dist.metadata["Name"], "version": dist.version}
                   for dist in sorted(distributions(), key=lambda d: d.metadata["Name"].lower())]
    components = [d for d in environment if d['name'].lower() in runtime_names]
    components.append({'type': 'library', 'name': 'CPython', 'version': sys.version.split()[0]})
    components.append({'type': 'library', 'name': 'Roboto', 'description': 'Bundled font; see assets/fonts/OFL.txt'})
    import ssl
    components.append({'type': 'library', 'name': 'OpenSSL (CPython ssl)',
                       'version': ssl.OPENSSL_VERSION,
                       'description': 'Native libssl/libcrypto; cryptography also embeds its own OpenSSL'})
    from cryptography.hazmat.backends.openssl.backend import backend
    components.append({'type': 'library', 'name': 'OpenSSL (cryptography)',
                       'version': backend.openssl_version_text()})
    components.append({'type': 'library', 'name': 'Microsoft Visual C++ runtime',
                       'description': 'Bundled VCRUNTIME/MSVCP DLLs; exact files and hashes in manifest'})
    (root / "sbom.json").write_text(json.dumps({"bomFormat": "CycloneDX", "specVersion": "1.5", "version": 1,
                                               "components": components,
                                               "properties": [{"name": "quaber:scope", "value": "Bundled runtime; build environment listed separately in release-manifest.json"}]}, indent=2), encoding="utf-8")
    files["sbom.json"] = hashlib.sha256((root / "sbom.json").read_bytes()).hexdigest()
    runtime = {
        "python": sys.version,
        "architecture": "x64" if sys.maxsize > 2**32 else "x86",
        "pyqt": QtCore.PYQT_VERSION_STR,
        "qt_runtime": QtCore.qVersion(),
        "qt_build": QtCore.QT_VERSION_STR,
        "sip_distribution": version("PyQt5-sip"),
        "qt_libraries": QLibraryInfo.location(QLibraryInfo.LibrariesPath),
        "qt_plugins": QLibraryInfo.location(QLibraryInfo.PluginsPath),
        "sip_module": sip.__file__,
    }
    source = Path(__file__).parent
    source_files = [source / 'main.py', source / 'release_manifest.py', source / 'build_windows.ps1',
                    source / 'requirements.lock', *sorted((source / 'quaber').rglob('*.py')),
                    *sorted((source / 'packaging').glob('*.py')),
                    *sorted((source / 'packaging').glob('*.ps1')),
                    *sorted((source / 'packaging').glob('*.json')), source / 'packaging/Quaber.spec']
    report_dir = os.environ.get('QUABER_BUILD_REPORT')
    exclusions = (json.loads((Path(report_dir) / 'exclusions.json').read_text(encoding='utf-8'))
                  if report_dir else {'profile': 'unclassified', 'groups': [], 'files': []})
    (root / "release-manifest.json").write_text(json.dumps({
        'created_utc': datetime.now(timezone.utc).isoformat(),
        'profile': os.environ.get('QUABER_BUILD_PROFILE', 'unclassified'),
        'source_sha256': {p.relative_to(source).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                          for p in source_files},
        'build_environment': environment, 'exclusions': exclusions,
        'payload_bytes_excluding_manifest': sum((root / p).stat().st_size for p in files),
        'packaging': {'mode': 'onedir', 'contents_directory': '.', 'optimize': 0,
                      'strip': False, 'upx': False, 'zip': 'DEFLATE level 9'},
        'runtime': runtime, 'sha256': files}, indent=2), encoding='utf-8')


if __name__ == "__main__":
    check_environment()
    if len(sys.argv) == 2:
        inventory(Path(sys.argv[1]))
