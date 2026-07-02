from __future__ import annotations

import argparse

import pytest

from hermes_cli import hki_cmd
from hki.dossier import (
    build_dossier,
    dossier_path_for_slug,
    gather_dossier_evidence,
    generate_dossier_queries,
    list_dossiers,
    publish_dossier,
)
from hki.inventory import build_inventory, write_inventory
from hki.manifest import build_manifest, write_manifest
from hki.paths import resolve_scope


def _run_cli(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    hki_parser = hki_cmd.build_parser(sub)
    hki_parser.set_defaults(func=hki_cmd.hki_command)
    args = parser.parse_args(["hki", *argv])
    return hki_cmd.hki_command(args)


def _write_manifest(root):
    scope = resolve_scope(root)
    inventory = build_inventory(scope)
    write_inventory(inventory, scope)
    manifest = build_manifest(inventory, inventory_path=scope.inventory_path)
    write_manifest(manifest, scope)
    return manifest, scope


def test_dossier_query_expansion_removes_stop_words_and_adds_variants():
    queries = generate_dossier_queries("skill discovery in Reuben")

    assert queries[0] == "skill discovery in Reuben"
    assert "skill discovery" in queries
    assert "discovery reuben" in queries
    assert "in" not in queries
    assert "skill" in queries
    assert "SKILL.md" in queries
    assert "discover" in queries


def test_dossier_generation_creates_manifest_search_report_and_markdown(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "visalia.md").write_text(
        "Visalia camera access uses travelrouter over VPN.\n",
        encoding="utf-8",
    )

    scope = resolve_scope(tmp_path)
    dossier = build_dossier(scope, "Visalia camera travelrouter")

    assert dossier.slug == "visalia-camera-travelrouter"
    assert dossier.dossier_path == (
        tmp_path / ".hermes" / "hki" / "reports" / "dossiers" / "visalia-camera-travelrouter.md"
    )
    assert dossier.manifest_path.exists()
    assert dossier.search_report_path.exists()
    assert (tmp_path / ".hermes" / "hki" / "inventory.json").exists()

    content = dossier.dossier_path.read_text(encoding="utf-8")
    assert "# HKI Dossier: Visalia camera travelrouter" in content
    assert "Original topic: `Visalia camera travelrouter`" in content
    assert "## Generated Search Queries" in content
    assert "Manifest: `.hermes/hki/manifest.json`" in content
    assert "Search report: `.hermes/hki/reports/search-visalia-camera-travelrouter.md`" in content
    assert "Source ID: `src_" in content
    assert "`docs/visalia.md`" in content
    assert "L1: Visalia camera access uses travelrouter over VPN." in content
    assert "queries: `" in content
    assert "first-pass lexical/source-based reconstruction" in content


def test_multi_query_dossier_finds_evidence_when_full_phrase_fails(tmp_path):
    (tmp_path / "agent").mkdir()
    (tmp_path / "agent" / "skill_commands.py").write_text(
        "scan SKILL.md files during discovery for slash commands\n",
        encoding="utf-8",
    )

    dossier = build_dossier(resolve_scope(tmp_path), "skill discovery in Reuben")

    content = dossier.dossier_path.read_text(encoding="utf-8")
    assert dossier.search.result_count == 1
    assert "`agent/skill_commands.py`" in content
    assert "`skill discovery in Reuben`" in content
    assert "`skill discovery`" in content
    assert "`SKILL.md`" in content


def test_dossier_deduplicates_repeated_hits_across_queries(tmp_path):
    (tmp_path / "skills").mkdir()
    (tmp_path / "skills" / "guide.md").write_text("skill discovery\n", encoding="utf-8")
    manifest, scope = _write_manifest(tmp_path)

    evidence = gather_dossier_evidence(manifest, scope, "skill discovery")

    assert evidence.result_count == 1
    assert len(evidence.results[0].matches) == 1
    assert "skill discovery" in evidence.results[0].matches[0].queries
    assert "skill" in evidence.results[0].matches[0].queries
    assert "discovery" in evidence.results[0].matches[0].queries


def test_dossier_groups_implementation_docs_tests_and_incidental_evidence(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "docs").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "data").mkdir()
    (tmp_path / "src" / "app.py").write_text("needle\n", encoding="utf-8")
    (tmp_path / "docs" / "README.md").write_text("needle\n", encoding="utf-8")
    (tmp_path / "tests" / "test_app.py").write_text("needle\n", encoding="utf-8")
    (tmp_path / "data" / "facts.csv").write_text("needle\n", encoding="utf-8")

    dossier = build_dossier(resolve_scope(tmp_path), "needle")
    content = dossier.dossier_path.read_text(encoding="utf-8")

    implementation = content.split("### Likely Implementation / Source", 1)[1].split(
        "### Docs / Notes", 1
    )[0]
    docs = content.split("### Docs / Notes", 1)[1].split("### Tests / Examples", 1)[0]
    tests = content.split("### Tests / Examples", 1)[1].split("### Incidental / Other", 1)[0]
    incidental = content.split("### Incidental / Other", 1)[1].split("## Initial Reconstruction", 1)[0]
    assert "`src/app.py`" in implementation
    assert "`docs/README.md`" in docs
    assert "`tests/test_app.py`" in tests
    assert "`data/facts.csv`" in incidental


def test_no_results_dossier_includes_suggested_narrower_searches(tmp_path):
    (tmp_path / "notes.txt").write_text("nothing relevant\n", encoding="utf-8")

    dossier = build_dossier(resolve_scope(tmp_path), "skill discovery in Reuben")
    content = dossier.dossier_path.read_text(encoding="utf-8")

    assert "No evidence found." in content
    assert "Suggested narrower searches:" in content
    assert "`skill discovery`" in content


def test_dossier_path_slug_creation_is_stable(tmp_path):
    scope = resolve_scope(tmp_path)

    path = dossier_path_for_slug(scope, "visalia-camera-travelrouter")

    assert path == tmp_path / ".hermes" / "hki" / "reports" / "dossiers" / "visalia-camera-travelrouter.md"


def test_dossier_publish_uses_local_vault_by_default(tmp_path):
    (tmp_path / "notes.txt").write_text("Visalia camera travelrouter VPN\n", encoding="utf-8")

    dossier = build_dossier(resolve_scope(tmp_path), "Visalia camera travelrouter", publish=True)

    assert dossier.publication_path == (
        tmp_path / ".hermes" / "hki" / "vault" / "dossiers" / "visalia-camera-travelrouter.md"
    )
    assert dossier.publication_path.read_text(encoding="utf-8") == dossier.dossier_path.read_text(
        encoding="utf-8"
    )


def test_dossier_publish_uses_explicit_vault_root(tmp_path):
    workspace = tmp_path / "workspace"
    vault = tmp_path / "vault"
    workspace.mkdir()
    (workspace / "notes.txt").write_text("Visalia camera travelrouter VPN\n", encoding="utf-8")

    dossier = build_dossier(
        resolve_scope(workspace),
        "Visalia camera travelrouter",
        publish=True,
        vault_root=vault,
    )

    assert dossier.publication_path == vault.resolve() / "dossiers" / "visalia-camera-travelrouter.md"
    assert dossier.publication_path.exists()


def test_publish_dossier_rejects_outside_workspace_source(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("# Outside\n", encoding="utf-8")

    with pytest.raises(ValueError, match="outside workspace root"):
        publish_dossier(resolve_scope(workspace), outside)

    assert not (workspace / ".hermes" / "hki" / "vault").exists()


def test_dossier_listing_discovers_existing_dossiers(tmp_path):
    (tmp_path / "notes.txt").write_text("Visalia camera travelrouter VPN\n", encoding="utf-8")
    scope = resolve_scope(tmp_path)
    build_dossier(scope, "Visalia camera travelrouter")

    listings = list_dossiers(scope)

    assert len(listings) == 1
    assert listings[0].relative_path == ".hermes/hki/reports/dossiers/visalia-camera-travelrouter.md"
    assert listings[0].title == "HKI Dossier: Visalia camera travelrouter"


def test_cli_dossier_generation_publish_and_listing(tmp_path, capsys):
    (tmp_path / "notes.txt").write_text("Visalia camera travelrouter VPN\n", encoding="utf-8")

    assert _run_cli(["dossier", "--cwd", str(tmp_path), "--publish", "Visalia camera travelrouter"]) == 0
    out = capsys.readouterr().out
    assert "Wrote HKI dossier" in out
    assert "published:" in out

    assert _run_cli(["dossier", "--cwd", str(tmp_path), "list"]) == 0
    out = capsys.readouterr().out
    assert ".hermes/hki/reports/dossiers/visalia-camera-travelrouter.md" in out


def test_cli_publish_existing_dossier_to_explicit_vault(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    vault = tmp_path / "vault"
    workspace.mkdir()
    (workspace / "notes.txt").write_text("Visalia camera travelrouter VPN\n", encoding="utf-8")
    scope = resolve_scope(workspace)
    dossier = build_dossier(scope, "Visalia camera travelrouter")

    assert _run_cli(
        [
            "publish",
            "--cwd",
            str(workspace),
            "--vault-root",
            str(vault),
            dossier.dossier_path.relative_to(workspace).as_posix(),
        ]
    ) == 0

    out = capsys.readouterr().out
    assert "Published HKI dossier" in out
    assert (vault / "dossiers" / "visalia-camera-travelrouter.md").exists()
