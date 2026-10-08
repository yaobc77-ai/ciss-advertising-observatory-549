"""Create a public source review archive with its current revision and file hashes.

The Git index and the explicit public allowlist define its default contents.
Nonignored untracked files require an explicit option and the same public checks.
Private inputs, runtime outputs, backups and credentials are transferred separately.
Public research reports can contain advertising excerpts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import posixpath
import re
import subprocess
import tomllib
from pathlib import Path, PurePosixPath
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[1]
ROOT_FILES = frozenset({
    "pyproject.toml", "uv.lock", ".env.example", ".gitignore", ".gitattributes",
    "Dockerfile", ".dockerignore", "railway.json",
})
REQUIRED_FILES = frozenset({
    "pyproject.toml", "uv.lock", ".env.example", "README.md", "Dockerfile",
    ".dockerignore", "railway.json", ".github/workflows/ci.yml",
})
FOLDERS = frozenset({"src", "tests", "scripts", "config", "docs", "eval", "reports", "deliverables", "site"})
EXTENSIONS = frozenset({
    ".py", ".ps1", ".sql", ".css", ".js", ".md", ".json", ".jsonl", ".csv",
    ".txt", ".yml", ".yaml", ".pptx", ".html", ".svg", ".png", ".jpg",
    ".jpeg", ".gif", ".webp", ".woff", ".woff2",
})
PRIVATE_DIRECTORIES = frozenset({
    ".git", ".runtime", ".venv", ".tmp", "sources", "analysis", "outputs",
    "backups", "__pycache__", ".pytest_cache", ".ruff_cache", "private",
    "record-assets", "record_assets", "source-bundle",
})
MANIFEST_NAME = "HANDOFF_MANIFEST.json"
CHECKSUM_NAME = "CONTENTS.sha256"


def _git_inventory(root: Path) -> tuple[str, set[str], set[str]]:
    """Read repository identity and index paths without changing Git state."""
    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=root, check=True, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ).stdout.decode("utf-8")

    if Path(git("rev-parse", "--show-toplevel").strip()).resolve() != root:
        raise ValueError("The handoff source must be the repository root")
    revision = git("rev-parse", "--verify", "HEAD").strip()
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("A canonical Git commit is required")
    tracked = set(filter(None, git("ls-files", "--cached", "-z").split("\0")))
    changed = set(filter(None, git("diff", "--name-only", "--no-renames", "HEAD", "-z").split("\0")))
    return revision, tracked, changed


def _untracked_inventory(root: Path) -> set[str]:
    """Read nonignored untracked paths without staging or changing Git state."""
    result = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard", "-z"],
        cwd=root, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    return set(filter(None, result.stdout.decode("utf-8").split("\0")))


def _public_path(name: str) -> bool:
    path = PurePosixPath(name)
    parts = path.parts
    if not parts or path.is_absolute() or ".." in parts or "\\" in name:
        return False
    if any(part.casefold() in PRIVATE_DIRECTORIES for part in parts[:-1]):
        return False
    if any(part.startswith(".env") for part in parts) and name != ".env.example":
        return False
    if name in ROOT_FILES:
        return True
    if len(parts) == 1:
        return path.suffix == ".md"
    if parts[:2] == (".github", "workflows"):
        return len(parts) == 3 and path.suffix in {".yml", ".yaml"}
    return parts[0] in FOLDERS and path.suffix in EXTENSIONS


def _public_file(root: Path, name: str) -> Path:
    """Reject aliases both when reading inputs and when checking them again."""
    file = root.joinpath(*PurePosixPath(name).parts)
    resolved = file.resolve(strict=True)
    current = root
    for part in PurePosixPath(name).parts:
        current = current / part
        if current.is_symlink() or current.is_junction():
            raise ValueError(f"Public archive input must be a regular workspace file: {name}")
    if not resolved.is_relative_to(root) or not resolved.is_file():
        raise ValueError(f"Public archive input must be a regular workspace file: {name}")
    return resolved


def _portable_markdown(entries: dict[str, bytes], root: Path) -> None:
    # Rewrite only an exact workspace link whose target is actually bundled.
    local_link = re.compile(r"\]\(<" + re.escape(root.as_posix()) + r"/([^>]+)>\)")
    for name, data in list(entries.items()):
        if not name.endswith(".md"):
            continue

        def portable_link(match):
            target_name = match.group(1)
            if target_name not in entries:
                return match.group(0)
            relative_target = posixpath.relpath(target_name, posixpath.dirname(name) or ".")
            return f"](<{relative_target}>)"

        entries[name] = local_link.sub(portable_link, data.decode("utf-8")).encode("utf-8")


def build_archive(root: Path, output: Path, include_untracked: bool = False) -> dict:
    """Build a new archive; never overwrite an archive or checksum sidecar."""
    root = root.resolve(strict=True)
    target = output.resolve()
    sidecar = target.with_suffix(".zip.sha256")
    if target.exists() or sidecar.exists():
        raise FileExistsError("The handoff archive and checksum destination must both be new")
    revision, tracked, changed = _git_inventory(root)
    untracked = _untracked_inventory(root) if include_untracked else set()
    untracked_public = sorted(name for name in untracked if _public_path(name))
    names = sorted(name for name in tracked | untracked if _public_path(name))
    missing = REQUIRED_FILES - set(names)
    if missing:
        raise ValueError("Required current delivery files are missing from the inventory: " + ", ".join(sorted(missing)))
    entries = {}
    secret = os.environ.get("OPENAI_API_KEY", "")
    for name in names:
        data = _public_file(root, name).read_bytes()
        if (secret and secret.encode() in data) or re.search(rb"sk-(?:proj-)?[a-zA-Z0-9_-]{30,}", data):
            raise RuntimeError(f"Credential-like content found in {name}; archive not created")
        entries[name] = data
    source_hashes = {name: hashlib.sha256(data).hexdigest() for name, data in entries.items()}
    after_revision, after_tracked, after_changed = _git_inventory(root)
    if (after_revision, after_tracked, after_changed) != (revision, tracked, changed):
        raise RuntimeError("Repository state changed while preparing the handoff")
    if include_untracked and _untracked_inventory(root) != untracked:
        raise RuntimeError("Untracked inventory changed while preparing the handoff")
    if any(hashlib.sha256(_public_file(root, name).read_bytes()).hexdigest() != digest
           for name, digest in source_hashes.items()):
        raise RuntimeError("Public source bytes changed while preparing the handoff")
    _portable_markdown(entries, root)
    archived_hashes = {name: hashlib.sha256(data).hexdigest() for name, data in entries.items()}
    public_changes = sorted(changed & set(names))
    manifest = {
        "schema_version": 1,
        "kind": "public_source_handoff",
        "git_revision": revision,
        "revision_state": "working_tree_snapshot" if public_changes or untracked_public else "git_revision_snapshot",
        "changed_public_paths": public_changes,
        "inventory_mode": "tracked_and_nonignored_untracked" if include_untracked else "tracked_only",
        "untracked_public_paths": untracked_public,
        "application_version": tomllib.loads(entries["pyproject.toml"].decode("utf-8"))["project"]["version"],
        "source_file_sha256": source_hashes,
        "archived_file_sha256": archived_hashes,
        "hash_scope": "File hash maps cover payload files; CONTENTS.sha256 also hashes this manifest. Neither hashes itself.",
        "excluded_private_scope": ["source datasets", "runtime outputs", "backups", "record asset bundles", "private configuration"],
        "contents_scope": "Public code, configuration, documentation and "
                          + ("tracked and explicitly included nonignored untracked" if include_untracked else "tracked")
                          + " research artifacts; reports may contain advertising excerpts.",
        "acceptance_status": "No project, semantic, client or dual-dataset acceptance is inferred by this archive.",
    }
    entries[MANIFEST_NAME] = (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    entries[CHECKSUM_NAME] = (
        "\n".join(f"{hashlib.sha256(data).hexdigest()}  {name}" for name, data in entries.items()) + "\n"
    ).encode("utf-8")
    target.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(target, "x", ZIP_DEFLATED) as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    with ZipFile(target) as archive:
        if archive.testzip() is not None or set(archive.namelist()) != set(entries):
            raise RuntimeError("Handoff archive integrity check failed")
        if any(archive.read(name) != data for name, data in entries.items()):
            raise RuntimeError("Handoff archive bytes differ from the reviewed payload")
    sha = hashlib.sha256(target.read_bytes()).hexdigest()
    with sidecar.open("x", encoding="utf-8") as stream:
        stream.write(f"{sha}  {target.name}\n")
    return {"archive": str(target), "entries": len(entries), "bytes": target.stat().st_size,
            "sha256": sha, "git_revision": revision, "revision_state": manifest["revision_state"],
            "inventory_mode": manifest["inventory_mode"], "untracked_public_paths": untracked_public}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--include-untracked", action="store_true",
                        help="Explicitly include nonignored untracked files that pass the public checks")
    args = parser.parse_args()
    print(json.dumps(build_archive(ROOT, args.output, include_untracked=args.include_untracked), indent=2))


if __name__ == "__main__":
    main()
