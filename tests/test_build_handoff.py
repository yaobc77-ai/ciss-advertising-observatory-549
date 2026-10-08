from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from zipfile import ZipFile

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/build_handoff.py"
SPEC = importlib.util.spec_from_file_location("build_handoff_under_test", SCRIPT)
handoff = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(handoff)
GIT_INVENTORY = handoff._git_inventory


@pytest.fixture
def source(tmp_path, monkeypatch):
    root = tmp_path / "source"
    files = {
        "README.md": "Research preview; independent acceptance remains pending.\n",
        "pyproject.toml": '[project]\nname = "fixture"\nversion = "0.4.4"\n',
        "uv.lock": "version = 1\n", ".env.example": "OPENAI_API_KEY=\n",
        "Dockerfile": "FROM python:3.12\n", ".dockerignore": ".env\nsources\n",
        "railway.json": '{"deploy":{"healthcheckPath":"/healthz"}}',
        ".github/workflows/ci.yml": "name: CI\n",
        ".github/workflows/pages.yaml": "name: Pages\n",
        "src/observatory/app.py": "# Runtime code\n",
        "src/observatory/assets/tools.svg": "<svg />\n",
        "src/observatory/sql/001.sql": "SELECT 1;\n",
        "docs/guide.md": "人工评价待办。\n",
        "reports/public_receipt.json": '{"semantic_acceptance": "pending"}\n',
        "eval/development.jsonl": '{"status": "pending_social"}\n',
        "deliverables/demo.en.md": "Native collection demonstration.\n",
        "site/index.html": "<h1>Research preview</h1>\n",
        ".env": "SECRET=private\n", "sources/ads.csv": "Private original records\n",
        "outputs/paid_answer.json": '{"text":"Private runtime result"}',
        ".runtime/private.csv": "Private runtime record\n",
        "backups/database.dump": "Private database copy\n",
        "docs/private/client.csv": "Private customer rows\n",
        "config/record-assets/record_assets.json": "Private source attachment manifest\n",
        "src/observatory/.env.example": "Unexpected nested private configuration\n",
        "reports/private_database.sqlite": "An accidentally tracked database\n",
    }
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="")
    monkeypatch.setattr(handoff, "_git_inventory", lambda _root: ("a" * 40, set(files), set()))
    return root, files


def test_current_delivery_archive_contains_runtime_deployment_and_public_artifacts(source, tmp_path):
    root, _files = source
    target = tmp_path / "review.zip"
    receipt = handoff.build_archive(root, target)
    with ZipFile(target) as archive:
        names = set(archive.namelist())
        required = {
            "Dockerfile", ".dockerignore", "railway.json", ".github/workflows/ci.yml",
            ".github/workflows/pages.yaml", "src/observatory/assets/tools.svg",
            "src/observatory/sql/001.sql", "site/index.html", "docs/guide.md",
            "reports/public_receipt.json", "eval/development.jsonl", "deliverables/demo.en.md",
        }
        assert required <= names
        assert names.isdisjoint({
            ".env", "sources/ads.csv", "outputs/paid_answer.json", ".runtime/private.csv",
            "backups/database.dump", "docs/private/client.csv", "reports/private_database.sqlite",
            "config/record-assets/record_assets.json", "src/observatory/.env.example",
        })
        assert archive.read("docs/guide.md").decode() == "人工评价待办。\n"
        assert "pending" in archive.read("README.md").decode()
        manifest = json.loads(archive.read("HANDOFF_MANIFEST.json"))
        assert manifest["git_revision"] == "a" * 40
        assert manifest["application_version"] == "0.4.4"
        assert manifest["revision_state"] == "git_revision_snapshot"
        payload_names = names - {"HANDOFF_MANIFEST.json", "CONTENTS.sha256"}
        assert set(manifest["archived_file_sha256"]) == payload_names
        for name, digest in manifest["archived_file_sha256"].items():
            assert hashlib.sha256(archive.read(name)).hexdigest() == digest
        checksums = {
            name: digest for digest, name in (
                line.split("  ", 1) for line in archive.read("CONTENTS.sha256").decode().splitlines()
            )
        }
        assert set(checksums) == names - {"CONTENTS.sha256"}
        for name, digest in checksums.items():
            assert hashlib.sha256(archive.read(name)).hexdigest() == digest
        assert "No project, semantic, client or dual-dataset acceptance" in manifest["acceptance_status"]
    assert receipt["sha256"] == hashlib.sha256(target.read_bytes()).hexdigest()
    assert target.with_suffix(".zip.sha256").read_text().split()[0] == receipt["sha256"]


def test_untracked_public_looking_files_are_not_silently_bundled(source, tmp_path):
    root, _ = source
    (root / "reports/untracked_client_data.csv").write_text("Private unapproved rows", encoding="utf-8")
    target = tmp_path / "review.zip"
    handoff.build_archive(root, target)
    with ZipFile(target) as archive:
        assert "reports/untracked_client_data.csv" not in archive.namelist()


def test_markdown_portability_retains_status_and_private_links(source, tmp_path):
    root, _ = source
    original = (f"[Guide](<{root.as_posix()}/docs/guide.md>)\n"
                f"[Private source](<{root.as_posix()}/sources/ads.csv>)\n"
                "Customer acceptance remains pending.\n")
    (root / "README.md").write_text(original, encoding="utf-8", newline="")
    target = tmp_path / "review.zip"
    handoff.build_archive(root, target)
    with ZipFile(target) as archive:
        text = archive.read("README.md").decode()
        assert "[Guide](<docs/guide.md>)" in text
        assert f"[Private source](<{root.as_posix()}/sources/ads.csv>)" in text
        assert "Customer acceptance remains pending." in text
        manifest = json.loads(archive.read("HANDOFF_MANIFEST.json"))
        assert manifest["source_file_sha256"]["README.md"] == hashlib.sha256(original.encode()).hexdigest()
        assert manifest["source_file_sha256"]["README.md"] != manifest["archived_file_sha256"]["README.md"]


@pytest.mark.parametrize("existing", ["archive", "sidecar"])
def test_existing_delivery_outputs_are_never_overwritten(source, tmp_path, existing):
    root, _ = source
    target = tmp_path / "review.zip"
    preserved = target if existing == "archive" else target.with_suffix(".zip.sha256")
    preserved.write_bytes(b"Keep this delivery artifact")
    with pytest.raises(FileExistsError):
        handoff.build_archive(root, target)
    assert preserved.read_bytes() == b"Keep this delivery artifact"
    if existing == "sidecar":
        assert not target.exists()


def test_working_source_changes_are_not_presented_as_an_exact_commit(source, tmp_path, monkeypatch):
    root, files = source
    monkeypatch.setattr(handoff, "_git_inventory", lambda _root: ("b" * 40, set(files), {"src/observatory/app.py"}))
    target = tmp_path / "review.zip"
    handoff.build_archive(root, target)
    with ZipFile(target) as archive:
        manifest = json.loads(archive.read("HANDOFF_MANIFEST.json"))
        assert manifest["git_revision"] == "b" * 40
        assert manifest["revision_state"] == "working_tree_snapshot"
        assert manifest["changed_public_paths"] == ["src/observatory/app.py"]


def test_missing_current_deployment_contract_refuses_an_incomplete_archive(source, tmp_path, monkeypatch):
    root, files = source
    monkeypatch.setattr(handoff, "_git_inventory", lambda _root: ("a" * 40, set(files) - {"Dockerfile"}, set()))
    with pytest.raises(ValueError, match="Dockerfile"):
        handoff.build_archive(root, tmp_path / "review.zip")


def test_configured_secret_in_a_public_payload_prevents_archive_creation(source, tmp_path, monkeypatch):
    root, _ = source
    token = "a-test-secret-value"
    monkeypatch.setenv("OPENAI_API_KEY", token)
    (root / "docs/guide.md").write_text(f"Unexpected secret: {token}", encoding="utf-8")
    target = tmp_path / "review.zip"
    with pytest.raises(RuntimeError, match="Credential-like content"):
        handoff.build_archive(root, target)
    assert not target.exists()


def test_repository_revision_change_during_packaging_refuses_a_mixed_archive(source, tmp_path, monkeypatch):
    root, files = source
    observed = iter([("a" * 40, set(files), set()), ("b" * 40, set(files), set())])
    monkeypatch.setattr(handoff, "_git_inventory", lambda _root: next(observed))
    target = tmp_path / "review.zip"
    with pytest.raises(RuntimeError, match="Repository state changed"):
        handoff.build_archive(root, target)
    assert not target.exists()


def test_source_edit_during_packaging_refuses_changed_payload_bytes(source, tmp_path, monkeypatch):
    root, files = source
    calls = 0

    def observe(_root):
        nonlocal calls
        calls += 1
        if calls == 2:
            (root / "README.md").write_text("A later edit must not be mixed in.", encoding="utf-8")
        return "a" * 40, set(files), set()

    monkeypatch.setattr(handoff, "_git_inventory", observe)
    target = tmp_path / "review.zip"
    with pytest.raises(RuntimeError, match="Public source bytes changed"):
        handoff.build_archive(root, target)
    assert not target.exists()


def git(root, *arguments):
    return subprocess.run(
        ["git", "-c", "core.autocrlf=false", *arguments], cwd=root, check=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    ).stdout.decode("utf-8")


@pytest.fixture
def git_source(source, tmp_path, monkeypatch):
    """Exercise real index/ignore behavior without the project's hooks or identity."""
    root, _ = source
    monkeypatch.setattr(handoff, "_git_inventory", GIT_INVENTORY)
    monkeypatch.setenv("OPENAI_API_KEY", "")
    (root / ".gitignore").write_text("docs/ignored-*\n", encoding="utf-8")
    hooks = tmp_path / "empty-hooks"
    hooks.mkdir()
    git(root, "init", "--quiet")
    git(root, "config", "core.hooksPath", str(hooks))
    git(root, "add", "--all")
    git(root, "-c", "user.name=Handoff fixture", "-c", "user.email=fixture@example.invalid",
        "commit", "--quiet", "-m", "Synthetic source fixture")
    return root


def write_source(root, name, data):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def test_opt_in_git_inventory_includes_new_modules_and_definitions_without_staging(git_source, tmp_path):
    root = git_source
    new_files = {
        "src/observatory/content_publication.py": b"# New candidate module\r\nVALUE = 'pending'\r\n",
        "scripts/prepare_content_publication.py": b"# Offline preparation helper\n",
        "eval/customer_metrics/content_questions.v1.json": b'{"status":"draft_requires_confirmation"}\n',
        "docs/current handoff.zh-CN.md": "当前未发布候选；客户验收待办。\n".encode(),
    }
    for name, data in new_files.items():
        write_source(root, name, data)
    excluded = {
        "docs/ignored-local.md", "docs/private/new_review.csv", "sources/new_input.csv",
        ".runtime/new_receipt.json", "reports/new_database.sqlite", "foreign/new.json",
        "src/observatory/.env.local",
    }
    for name in excluded:
        write_source(root, name, b"Excluded fixture sk-proj-" + b"z" * 35)
    before_index = git(root, "ls-files", "--stage", "-z")
    before_status = git(root, "status", "--porcelain=v1", "--untracked-files=all", "-z")
    default_target = tmp_path / "tracked.zip"
    handoff.build_archive(root, default_target)
    with ZipFile(default_target) as archive:
        assert set(new_files).isdisjoint(archive.namelist())
        manifest = json.loads(archive.read(handoff.MANIFEST_NAME))
        assert manifest["inventory_mode"] == "tracked_only"
        assert manifest["untracked_public_paths"] == []
        assert manifest["revision_state"] == "git_revision_snapshot"

    target = tmp_path / "candidate.zip"
    receipt = handoff.build_archive(root, target, include_untracked=True)
    with ZipFile(target) as archive:
        assert set(new_files) <= set(archive.namelist())
        assert excluded.isdisjoint(archive.namelist())
        manifest = json.loads(archive.read(handoff.MANIFEST_NAME))
        assert manifest["inventory_mode"] == "tracked_and_nonignored_untracked"
        assert manifest["untracked_public_paths"] == sorted(new_files)
        assert manifest["changed_public_paths"] == []
        assert manifest["revision_state"] == "working_tree_snapshot"
        assert manifest["git_revision"] == git(root, "rev-parse", "HEAD").strip()
        assert manifest["application_version"] == "0.4.4"
        assert "No project, semantic, client or dual-dataset acceptance" in manifest["acceptance_status"]
        for name, data in new_files.items():
            assert archive.read(name) == data
            assert manifest["source_file_sha256"][name] == hashlib.sha256(data).hexdigest()
            assert manifest["archived_file_sha256"][name] == hashlib.sha256(data).hexdigest()
    assert receipt["untracked_public_paths"] == sorted(new_files)
    assert receipt["revision_state"] == "working_tree_snapshot"
    assert git(root, "ls-files", "--stage", "-z") == before_index
    assert git(root, "status", "--porcelain=v1", "--untracked-files=all", "-z") == before_status


@pytest.mark.parametrize("change", ["add", "remove", "edit"])
def test_opt_in_refuses_untracked_source_drift(git_source, tmp_path, monkeypatch, change):
    root = git_source
    new_module = write_source(root, "src/observatory/new_component.py", b"# First source version\n")
    inventory = handoff._untracked_inventory
    calls = 0

    def observe(path):
        nonlocal calls
        calls += 1
        if calls == 2:
            if change == "add":
                write_source(root, "docs/later.md", b"# Added after source reads\n")
            elif change == "remove":
                new_module.unlink()
            else:
                new_module.write_bytes(b"# Source edited after its initial read\n")
        return inventory(path)

    monkeypatch.setattr(handoff, "_untracked_inventory", observe)
    target = tmp_path / "candidate.zip"
    message = "Public source bytes changed" if change == "edit" else "Untracked inventory changed"
    with pytest.raises(RuntimeError, match=message):
        handoff.build_archive(root, target, include_untracked=True)
    assert not target.exists()
    assert not target.with_suffix(".zip.sha256").exists()


@pytest.mark.parametrize("configured", [False, True])
def test_opt_in_secret_in_new_public_file_refuses_archive(git_source, tmp_path, monkeypatch, configured):
    token = "synthetic-configured-key-value" if configured else "sk-proj-" + "x" * 35
    if configured:
        monkeypatch.setenv("OPENAI_API_KEY", token)
    write_source(git_source, "docs/new-review.md", f"Unexpected fixture key: {token}".encode())
    target = tmp_path / "candidate.zip"
    with pytest.raises(RuntimeError, match="Credential-like content"):
        handoff.build_archive(git_source, target, include_untracked=True)
    assert not target.exists()


def symlink(link, target, *, directory=False):
    try:
        link.symlink_to(target, target_is_directory=directory)
    except (OSError, NotImplementedError):
        pytest.skip("Creating test symlinks is unavailable on this Windows host")


def test_opt_in_rejects_untracked_symlink_input_even_with_identical_workspace_bytes(git_source, tmp_path):
    alias = git_source / "src/observatory/alias.py"
    symlink(alias, git_source / "src/observatory/app.py")
    target = tmp_path / "candidate.zip"
    with pytest.raises(ValueError, match="regular workspace file"):
        handoff.build_archive(git_source, target, include_untracked=True)
    assert not target.exists()


@pytest.mark.parametrize("change", ["delete", "file_alias", "parent_alias"])
def test_final_source_check_revalidates_untracked_paths_after_inventory(git_source, tmp_path, monkeypatch, change):
    root = git_source
    data = b"# Same candidate bytes\n"
    source = write_source(root, "src/observatory/new/component.py", data)
    replacement = write_source(tmp_path, "replacement/component.py", data)
    if change == "parent_alias":
        probe = tmp_path / "symlink-probe"
        symlink(probe, replacement.parent, directory=True)
        probe.unlink()
    elif change == "file_alias":
        probe = tmp_path / "symlink-probe"
        symlink(probe, replacement)
        probe.unlink()
    inventory = handoff._untracked_inventory
    calls = 0

    def observe(path):
        nonlocal calls
        calls += 1
        result = inventory(path)
        if calls == 2:
            source.unlink()
            if change == "file_alias":
                source.symlink_to(replacement)
            elif change == "parent_alias":
                source.parent.rmdir()
                source.parent.symlink_to(replacement.parent, target_is_directory=True)
        return result

    monkeypatch.setattr(handoff, "_untracked_inventory", observe)
    target = tmp_path / "candidate.zip"
    with pytest.raises(FileNotFoundError if change == "delete" else ValueError):
        handoff.build_archive(root, target, include_untracked=True)
    assert not target.exists()
    assert not target.with_suffix(".zip.sha256").exists()


def test_cli_include_untracked_builds_an_explicit_working_tree_candidate(git_source, tmp_path, monkeypatch, capsys):
    write_source(git_source, "src/observatory/new_module.py", b"# Candidate module\n")
    target = tmp_path / "cli-candidate.zip"
    monkeypatch.setattr(handoff, "ROOT", git_source)
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--output", str(target), "--include-untracked"])
    handoff.main()
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["inventory_mode"] == "tracked_and_nonignored_untracked"
    assert receipt["revision_state"] == "working_tree_snapshot"
    with ZipFile(target) as archive:
        assert archive.read("src/observatory/new_module.py") == b"# Candidate module\n"
