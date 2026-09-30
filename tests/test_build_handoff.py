from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
from zipfile import ZipFile

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/build_handoff.py"
SPEC = importlib.util.spec_from_file_location("build_handoff_under_test", SCRIPT)
handoff = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(handoff)


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
