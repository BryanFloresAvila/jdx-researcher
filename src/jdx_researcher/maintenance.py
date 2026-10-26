"""Offline backups: take the daemon lock, snapshot both databases and copy content objects."""
import json
import shutil
import sqlite3
import tarfile
import tempfile
from pathlib import Path

from .service import DaemonLock
from .storage import Store, digest


def backup(data_dir: Path, destination: Path):
    lock = DaemonLock(data_dir)
    lock.acquire()
    try:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "snapshot"
            root.mkdir()
            for name in ("evidence.sqlite3", "checkpoints.sqlite3"):
                if (data_dir / name).exists():
                    source = sqlite3.connect(data_dir / name)
                    target = sqlite3.connect(root / name)
                    try:
                        source.backup(target)
                    finally:
                        source.close()
                        target.close()
            if (data_dir / "objects").exists():
                shutil.copytree(data_dir / "objects", root / "objects")
            manifest = {str(p.relative_to(root)): digest(p.read_bytes())
                        for p in root.rglob("*") if p.is_file()}
            (root / "backup-manifest.json").write_text(json.dumps(manifest, indent=2))
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                raise FileExistsError("Refusing to overwrite an existing backup")
            with tarfile.open(destination, "w:gz") as archive:
                archive.add(root, arcname="snapshot")
        return {"path": str(destination), "files": len(manifest), "includes_api_token": False}
    finally:
        lock.release()


def restore(archive_path: Path, destination: Path):
    if destination.exists() and any(destination.iterdir()):
        raise ValueError("Restore destination must be empty")
    with tempfile.TemporaryDirectory() as temp:
        temp_path = Path(temp)
        with tarfile.open(archive_path, "r:gz") as archive:
            for member in archive.getmembers():
                if member.issym() or member.islnk() or not (member.isfile() or member.isdir()):
                    raise ValueError("Unsupported backup archive entry")
            archive.extractall(temp_path, filter="data")
        root = temp_path / "snapshot"
        manifest = json.loads((root / "backup-manifest.json").read_text())
        for name, expected in manifest.items():
            path = (root / name).resolve()
            if not path.is_relative_to(root.resolve()) or digest(path.read_bytes()) != expected:
                raise ValueError("Backup integrity check failed")
        if "evidence.sqlite3" not in manifest:
            raise ValueError("Backup has no evidence database")
        for name in ("evidence.sqlite3", "checkpoints.sqlite3"):
            if (root / name).exists():
                db = sqlite3.connect(root / name)
                try:
                    if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise ValueError("Database integrity check failed")
                finally:
                    db.close()
        destination.mkdir(parents=True, exist_ok=True)
        shutil.copytree(root, destination, dirs_exist_ok=True)
    store = Store(destination)
    # No API token in an archive. A new local token is generated on next startup.
    return {"data_dir": str(destination), "database": str(store.path)}
