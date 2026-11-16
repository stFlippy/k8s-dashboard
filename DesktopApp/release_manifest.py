"""Check the pinned build environment and inventory release artifacts."""
import hashlib
from importlib.metadata import version, distributions
import json
from pathlib import Path
import sys


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


def inventory(root):
    files = {str(path.relative_to(root).as_posix()): hashlib.sha256(path.read_bytes()).hexdigest()
             for path in sorted(root.rglob("*")) if path.is_file() and path.name not in ("release-manifest.json", "sbom.json")}
    components = [{"type": "library", "name": dist.metadata["Name"], "version": dist.version}
                  for dist in sorted(distributions(), key=lambda d: d.metadata["Name"].lower())]
    (root / "sbom.json").write_text(json.dumps({"bomFormat": "CycloneDX", "specVersion": "1.5", "version": 1,
                                               "components": components}, indent=2), encoding="utf-8")
    files["sbom.json"] = hashlib.sha256((root / "sbom.json").read_bytes()).hexdigest()
    (root / "release-manifest.json").write_text(json.dumps({"python": sys.version, "sha256": files}, indent=2), encoding="utf-8")


if __name__ == "__main__":
    check_environment()
    if len(sys.argv) == 2:
        inventory(Path(sys.argv[1]))
