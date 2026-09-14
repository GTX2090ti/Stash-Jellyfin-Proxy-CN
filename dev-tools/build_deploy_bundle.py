"""Build the deploy bundle for stash-jellyfin-proxy.

Produces, from the working tree, exactly the artifacts the NAS deploy
expects:

    deploy/app/stash_jellyfin_proxy/   clean copy of the package
    deploy/app.tar.gz                  tar.gz of deploy/app
    deploy/app.sha256                  per-file hashes (sha256sum format)
    deploy/MANIFEST.txt                human-readable manifest + archive size

The deploy/app copy is a *snapshot*: it is deleted and rebuilt so the
bundle can never drift from the source tree. Run from anywhere:

    python dev-tools/build_deploy_bundle.py

Prints which files were added / changed / removed relative to the previous
manifest, so a release can be reviewed before it ships.
"""
import hashlib
import shutil
import sys
import tarfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PACKAGE = "stash_jellyfin_proxy"
SOURCE = REPO / PACKAGE
DEPLOY = REPO / "deploy"
APP = DEPLOY / "app"
DEST = APP / PACKAGE
ARCHIVE = DEPLOY / "app.tar.gz"
HASH_FILE = DEPLOY / "app.sha256"
MANIFEST = DEPLOY / "MANIFEST.txt"

SKIP_DIRS = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
SKIP_SUFFIXES = {".pyc", ".pyo"}


def iter_files(root: Path):
    """Deterministic file list: relative POSIX paths, sorted."""
    found = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.suffix in SKIP_SUFFIXES:
            continue
        if path.name in {".DS_Store", "Thumbs.db"}:
            continue
        found.append(path)
    return sorted(found, key=lambda p: p.relative_to(root).as_posix())


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_previous_hashes():
    """Parse the existing manifest so we can report a diff."""
    if not MANIFEST.exists():
        return {}
    previous = {}
    for line in MANIFEST.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) >= 3 and len(parts[0]) == 64 and parts[1].isdigit():
            previous[parts[2]] = parts[0]
    return previous


def main() -> int:
    if not SOURCE.is_dir():
        print(f"source package not found: {SOURCE}", file=sys.stderr)
        return 1

    previous = read_previous_hashes()

    # --- 1. clean snapshot of the package -------------------------------
    if DEST.exists():
        shutil.rmtree(DEST)
    DEST.mkdir(parents=True)
    files = iter_files(SOURCE)
    for src in files:
        rel = src.relative_to(SOURCE)
        target = DEST / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)

    # --- 2. archive ------------------------------------------------------
    # mtime is pinned so identical trees produce identical archives; gzip
    # still embeds a timestamp, but the member list at least is stable.
    with tarfile.open(ARCHIVE, "w:gz") as tar:
        for path in iter_files(APP):
            arcname = Path("app") / path.relative_to(APP)
            info = tar.gettarinfo(str(path), arcname=str(arcname))
            info.mtime = 0
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            with path.open("rb") as handle:
                tar.addfile(info, handle)

    # --- 3. hashes + manifest -------------------------------------------
    entries = []
    for path in iter_files(APP):
        rel = path.relative_to(APP).as_posix()
        entries.append((sha256_of(path), path.stat().st_size, rel))

    HASH_FILE.write_text(
        "".join(f"{digest}  {rel}\n" for digest, _, rel in entries),
        encoding="utf-8",
    )

    total_bytes = sum(size for _, size, _ in entries)
    archive_bytes = ARCHIVE.stat().st_size
    lines = [
        "deployment bundle for stash-jellyfin-proxy",
        f"package files : {len(entries)}",
        f"total bytes  : {total_bytes}",
        f"archive      : app.tar.gz ({archive_bytes} bytes)",
        "",
        f"{'sha256':<64}  {'size':>10}  path",
        "-" * 100,
    ]
    lines += [f"{digest}  {size:>10}  {rel}" for digest, size, rel in entries]
    MANIFEST.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # --- 4. diff vs the previous manifest --------------------------------
    current = {rel: digest for digest, _, rel in entries}
    added = sorted(set(current) - set(previous))
    removed = sorted(set(previous) - set(current))
    changed = sorted(rel for rel in set(current) & set(previous) if current[rel] != previous[rel])

    print(f"bundle rebuilt: {len(entries)} files, {total_bytes} bytes")
    print(f"archive: {ARCHIVE.name} ({archive_bytes} bytes)")
    for label, items in (("added", added), ("changed", changed), ("removed", removed)):
        if items:
            print(f"{label} ({len(items)}):")
            for rel in items:
                print(f"  {rel}")
    if not (added or changed or removed):
        print("no content changes vs the previous manifest")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
