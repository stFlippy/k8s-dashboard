"""Create a source rollback without UserData, credentials or installed tools."""
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import zipfile


def create_snapshot(root):
    root = root.resolve()
    destination = root / "_rollback" / (datetime.now().strftime("%Y%m%d-%H%M%S-%f")+"-code-only")
    destination.mkdir(parents=True)
    exclude = {"UserData", "venv", ".venv", "_rollback", ".build-windows", "build-artifacts", "__pycache__", ".git", "dist", "build", "Addons", "offline-deps", "wheelhouse"}
    allowed = {".py", ".md", ".txt", ".ps1", ".svg", ".ico", ".toml", ".lock"}
    excluded = {name.casefold() for name in exclude}
    hashes = {}
    archive = destination / "DesktopApp-code.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as output:
        for directory, directories, files in os.walk(root, followlinks=False):
            directories[:] = sorted(
                name for name in directories
                if name.casefold() not in excluded
                and not name.casefold().startswith((".venv-", ".build-windows-", "build-artifacts-", "wheelhouse-"))
                and not (Path(directory)/name).is_symlink()
            )
            for filename in sorted(files):
                path = Path(directory) / filename
                relative = path.relative_to(root)
                asset = relative.parts[0] in ("assets", "public") and path.suffix.lower() in (".png", ".webp", ".ttf")
                if path.is_symlink() or (path.suffix not in allowed and not asset):
                    continue
                content = path.read_bytes()
                name = relative.as_posix()
                output.writestr(name, content)
                hashes[name] = hashlib.sha256(content).hexdigest()
    (destination / "manifest.json").write_text(json.dumps({"archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(), "files": hashes}, indent=2), encoding="utf-8")
    return archive


if __name__ == "__main__":
    print(create_snapshot(Path(__file__).resolve().parent))
