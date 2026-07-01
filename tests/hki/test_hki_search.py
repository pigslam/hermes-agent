from __future__ import annotations

import argparse
import json
from pathlib import Path

from hermes_cli import hki_cmd
from hki.inventory import build_inventory, write_inventory
from hki.manifest import Manifest, SourceRecord, build_manifest, source_id_for, write_manifest
from hki.paths import resolve_scope
from hki.search import (
    SearchLimits,
    latest_search_path,
    search_manifest,
    search_report_path,
    write_latest_search,
    write_search_report,
)


def _run_cli(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    hki_parser = hki_cmd.build_parser(sub)
    hki_parser.set_defaults(func=hki_cmd.hki_command)
    args = parser.parse_args(["hki", *argv])
    return hki_cmd.hki_command(args)


def _write_manifest(root: Path) -> tuple[Manifest, object]:
    scope = resolve_scope(root)
    inventory = build_inventory(scope)
    write_inventory(inventory, scope)
    manifest = build_manifest(inventory, inventory_path=scope.inventory_path)
    write_manifest(manifest, scope)
    return manifest, scope


def test_search_finds_matching_text_files_from_manifest(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "hki.md").write_text(
        "HKI skill discovery belongs in source guidance.\n",
        encoding="utf-8",
    )
    (tmp_path / "docs" / "other.md").write_text("unrelated\n", encoding="utf-8")
    manifest, scope = _write_manifest(tmp_path)

    search = search_manifest(manifest, scope, "skill discovery", manifest_path=scope.manifest_path)

    assert search.result_count == 1
    assert search.searched_file_count == 2
    result = search.results[0]
    assert result.relative_path == "docs/hki.md"
    assert result.source_id == source_id_for("docs/hki.md")
    assert result.matches[0].line == 1
    assert "skill discovery" in result.matches[0].snippet


def test_search_uses_only_manifest_sources(tmp_path):
    (tmp_path / "listed.txt").write_text("listed file\n", encoding="utf-8")
    manifest, scope = _write_manifest(tmp_path)
    (tmp_path / "unlisted.txt").write_text("needle only lives here\n", encoding="utf-8")

    search = search_manifest(manifest, scope, "needle", manifest_path=scope.manifest_path)

    assert search.result_count == 0
    assert search.searched_file_count == 1


def test_search_ignores_binary_files(tmp_path):
    (tmp_path / "blob.bin").write_bytes(b"skill discovery\x00secret")
    manifest, scope = _write_manifest(tmp_path)

    search = search_manifest(manifest, scope, "skill discovery", manifest_path=scope.manifest_path)

    assert search.result_count == 0
    assert search.skipped_by_reason["binary_source"] == 1


def test_search_respects_size_limits(tmp_path):
    (tmp_path / "large.txt").write_text("needle\n" + ("x" * 100), encoding="utf-8")
    manifest, scope = _write_manifest(tmp_path)

    search = search_manifest(
        manifest,
        scope,
        "needle",
        manifest_path=scope.manifest_path,
        limits=SearchLimits(max_file_size_bytes=10),
    )

    assert search.result_count == 0
    assert search.searched_file_count == 0
    assert search.skipped_by_reason["too_large"] == 1


def test_search_returns_line_numbered_snippets(tmp_path):
    (tmp_path / "notes.txt").write_text(
        "first line\nsecond line has skill discovery here\nthird line\n",
        encoding="utf-8",
    )
    manifest, scope = _write_manifest(tmp_path)

    search = search_manifest(manifest, scope, "skill discovery", manifest_path=scope.manifest_path)

    assert search.results[0].matches[0].line == 2
    assert "second line has skill discovery here" in search.results[0].matches[0].snippet


def test_search_writes_latest_json_and_markdown_report(tmp_path):
    (tmp_path / "notes.txt").write_text("skill discovery\n", encoding="utf-8")
    manifest, scope = _write_manifest(tmp_path)
    search = search_manifest(manifest, scope, "skill discovery", manifest_path=scope.manifest_path)

    json_path = write_latest_search(search, scope)
    report_path = write_search_report(search, scope)

    assert json_path == tmp_path / ".hermes" / "hki" / "search" / "latest-search.json"
    assert report_path == tmp_path / ".hermes" / "hki" / "reports" / "search-skill-discovery.md"

    data = json.loads(json_path.read_text(encoding="utf-8"))
    assert data["schema_version"] == 1
    assert data["query"] == "skill discovery"
    assert data["manifest_path"] == ".hermes/hki/manifest.json"
    assert data["result_count"] == 1
    assert data["results"][0]["matches"][0]["line"] == 1

    content = report_path.read_text(encoding="utf-8")
    assert "# HKI Source Search: skill discovery" in content
    assert "bounded lexical source search" in content
    assert "L1: skill discovery" in content


def test_search_ordering_is_deterministic_for_tied_results(tmp_path):
    (tmp_path / "b.txt").write_text("needle\n", encoding="utf-8")
    (tmp_path / "a.txt").write_text("needle\n", encoding="utf-8")
    manifest, scope = _write_manifest(tmp_path)

    search = search_manifest(manifest, scope, "needle", manifest_path=scope.manifest_path)

    assert [result.relative_path for result in search.results] == ["a.txt", "b.txt"]


def test_cli_search_creates_missing_manifest_and_writes_outputs(tmp_path, capsys):
    (tmp_path / "notes.txt").write_text("skill discovery\n", encoding="utf-8")

    assert _run_cli(["search", "--cwd", str(tmp_path), "skill discovery"]) == 0
    assert "HKI search: skill discovery" in capsys.readouterr().out

    assert (tmp_path / ".hermes" / "hki" / "inventory.json").exists()
    assert (tmp_path / ".hermes" / "hki" / "manifest.json").exists()
    assert latest_search_path(resolve_scope(tmp_path)).exists()
    assert search_report_path(resolve_scope(tmp_path), "skill discovery").exists()


def test_search_does_not_traverse_outside_root(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("needle outside\n", encoding="utf-8")
    scope = resolve_scope(root)
    manifest = Manifest(
        root=str(root.resolve()),
        generated_at="2026-01-01T00:00:00Z",
        sources=(
            SourceRecord(
                source_id=source_id_for("../outside.txt"),
                relative_path="../outside.txt",
                kind="text",
                size=outside.stat().st_size,
                mtime=int(outside.stat().st_mtime),
                is_text=True,
            ),
        ),
    )

    search = search_manifest(manifest, scope, "needle", manifest_path=scope.manifest_path)

    assert search.result_count == 0
    assert search.skipped_by_reason["outside_root"] == 1


def test_search_ignores_excluded_manifest_entries(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("SECRET_TOKEN=needle\n", encoding="utf-8")
    scope = resolve_scope(tmp_path)
    manifest = Manifest(
        root=str(tmp_path.resolve()),
        generated_at="2026-01-01T00:00:00Z",
        sources=(
            SourceRecord(
                source_id=source_id_for(".env"),
                relative_path=".env",
                kind="config",
                size=env_path.stat().st_size,
                mtime=int(env_path.stat().st_mtime),
                is_text=True,
            ),
        ),
    )

    search = search_manifest(manifest, scope, "needle", manifest_path=scope.manifest_path)

    assert search.result_count == 0
    assert search.skipped_by_reason["excluded_path"] == 1


def test_search_excludes_packaging_metadata_after_manifest_generation(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "guide.txt").write_text("skill discovery source\n", encoding="utf-8")
    (tmp_path / "example.egg-info").mkdir()
    (tmp_path / "example.egg-info" / "SOURCES.txt").write_text(
        "skill discovery generated metadata\n",
        encoding="utf-8",
    )
    (tmp_path / "example.dist-info").mkdir()
    (tmp_path / "example.dist-info" / "METADATA").write_text(
        "skill discovery generated metadata\n",
        encoding="utf-8",
    )
    (tmp_path / ".eggs").mkdir()
    (tmp_path / ".eggs" / "cached.txt").write_text(
        "skill discovery generated metadata\n",
        encoding="utf-8",
    )
    manifest, scope = _write_manifest(tmp_path)

    search = search_manifest(manifest, scope, "skill discovery", manifest_path=scope.manifest_path)

    assert [result.relative_path for result in search.results] == ["src/guide.txt"]
    assert all("egg-info" not in source.relative_path for source in manifest.sources)
    assert all("dist-info" not in source.relative_path for source in manifest.sources)
    assert all(".eggs" not in source.relative_path for source in manifest.sources)


def test_search_excludes_packaging_metadata_from_stale_manifest(tmp_path):
    egg_info_path = tmp_path / "example.egg-info" / "SOURCES.txt"
    egg_info_path.parent.mkdir()
    egg_info_path.write_text("skill discovery generated metadata\n", encoding="utf-8")
    dist_info_path = tmp_path / "example.dist-info" / "METADATA"
    dist_info_path.parent.mkdir()
    dist_info_path.write_text("skill discovery generated metadata\n", encoding="utf-8")
    eggs_path = tmp_path / ".eggs" / "cached.txt"
    eggs_path.parent.mkdir()
    eggs_path.write_text("skill discovery generated metadata\n", encoding="utf-8")
    scope = resolve_scope(tmp_path)
    manifest = Manifest(
        root=str(tmp_path.resolve()),
        generated_at="2026-01-01T00:00:00Z",
        sources=(
            SourceRecord(
                source_id=source_id_for("example.egg-info/SOURCES.txt"),
                relative_path="example.egg-info/SOURCES.txt",
                kind="text",
                size=egg_info_path.stat().st_size,
                mtime=int(egg_info_path.stat().st_mtime),
                is_text=True,
            ),
            SourceRecord(
                source_id=source_id_for("example.dist-info/METADATA"),
                relative_path="example.dist-info/METADATA",
                kind="text",
                size=dist_info_path.stat().st_size,
                mtime=int(dist_info_path.stat().st_mtime),
                is_text=True,
            ),
            SourceRecord(
                source_id=source_id_for(".eggs/cached.txt"),
                relative_path=".eggs/cached.txt",
                kind="text",
                size=eggs_path.stat().st_size,
                mtime=int(eggs_path.stat().st_mtime),
                is_text=True,
            ),
        ),
    )

    search = search_manifest(manifest, scope, "skill discovery", manifest_path=scope.manifest_path)

    assert search.result_count == 0
    assert search.skipped_by_reason["excluded_path"] == 3
