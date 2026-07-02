from __future__ import annotations

import argparse
import json

from hermes_cli import hki_cmd
from hki.paths import resolve_scope
from hki.triage import triage_inbox, triage_json_path, triage_report_path


def _run_cli(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    hki_parser = hki_cmd.build_parser(sub)
    hki_parser.set_defaults(func=hki_cmd.hki_command)
    args = parser.parse_args(["hki", *argv])
    return hki_cmd.hki_command(args)


def test_triage_report_and_json_creation(tmp_path):
    (tmp_path / "notes.txt").write_text("Hermes Reuben HKI intake notes\n", encoding="utf-8")

    run = triage_inbox(resolve_scope(tmp_path))

    assert run.report_path == tmp_path / ".hermes" / "hki" / "reports" / "intake-triage.md"
    assert run.json_path == tmp_path / ".hermes" / "hki" / "triage" / "latest-triage.json"
    assert run.report_path.exists()
    assert run.json_path.exists()

    content = run.report_path.read_text(encoding="utf-8")
    assert "# HKI Inbox Intake Triage" in content
    assert "## Intake Summary" in content
    assert "## Candidate Topics / Projects" in content
    assert "## Next-Step Prompt" in content

    data = json.loads(run.json_path.read_text(encoding="utf-8"))
    assert data["root"] == str(tmp_path.resolve())
    assert data["source_count"] == 1
    assert data["candidates"]


def test_triage_detects_candidates_from_parsed_artifacts(tmp_path):
    (tmp_path / "projects.json").write_text(
        json.dumps({"projects": [{"name": "Visalia Camera Access"}, {"name": "Ramon HKI Workbench"}]}),
        encoding="utf-8",
    )

    run = triage_inbox(resolve_scope(tmp_path))
    titles = {candidate.title for candidate in run.candidates}

    assert "projects.json" in run.artifacts
    assert "Visalia Camera Access" in titles
    assert "Ramon HKI Workbench" in titles
    artifact_candidate = next(candidate for candidate in run.candidates if candidate.title == "Visalia Camera Access")
    assert artifact_candidate.source == "parsed artifacts"
    assert artifact_candidate.suggested_dossier_slug == "visalia-camera-access"


def test_triage_detects_candidates_from_lexical_probes(tmp_path):
    (tmp_path / "network.md").write_text(
        "WireGuard AllowedIPs route travelrouter camera RTSP subnet details\n",
        encoding="utf-8",
    )
    (tmp_path / "ai.md").write_text("Hermes Reuben ToBAI HKI Chik notes\n", encoding="utf-8")

    run = triage_inbox(resolve_scope(tmp_path))
    titles = {candidate.title for candidate in run.candidates}

    assert "WireGuard / Network / Router / Camera Access" in titles
    assert "Local AI / Hermes / ToBAI / HKI Infrastructure" in titles
    network = next(candidate for candidate in run.candidates if candidate.title.startswith("WireGuard"))
    assert network.evidence_strength in {"medium", "high"}
    assert network.suggested_dossier_slug == "wireguard-network-router-camera-access"
    assert network.evidence[0].snippet


def test_triage_omits_unsupported_probe_topics(tmp_path):
    (tmp_path / "notes.txt").write_text("only WireGuard camera travelrouter evidence\n", encoding="utf-8")

    run = triage_inbox(resolve_scope(tmp_path))
    titles = {candidate.title for candidate in run.candidates}

    assert "WireGuard / Network / Router / Camera Access" in titles
    assert "Tile / Bathroom Work" not in titles
    assert "Gaming / eGPU / Valheim" not in titles


def test_triage_avoids_probe_substring_false_positives(tmp_path):
    (tmp_path / "hardware.txt").write_text(
        "Volatile memory mode. keyboard present. baseboard serial visible.\n",
        encoding="utf-8",
    )

    run = triage_inbox(resolve_scope(tmp_path))
    titles = {candidate.title for candidate in run.candidates}

    assert "Tile / Bathroom Work" not in titles
    assert "Woodworking / Cut Lists" not in titles


def test_triage_no_evidence_report_is_clear_and_does_not_build_dossiers(tmp_path):
    (tmp_path / "blank.txt").write_text("unrelated sparse material\n", encoding="utf-8")

    run = triage_inbox(resolve_scope(tmp_path))
    content = run.report_path.read_text(encoding="utf-8")

    assert run.candidates == ()
    assert "No strong candidate topics found" in content
    assert not (tmp_path / ".hermes" / "hki" / "reports" / "dossiers").exists()


def test_cli_inbox_triage_and_shortcut_write_expected_outputs(tmp_path, capsys):
    (tmp_path / "notes.txt").write_text("Hermes Reuben HKI\n", encoding="utf-8")

    assert _run_cli(["inbox", "triage", "--cwd", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "Wrote HKI inbox triage" in out
    assert triage_report_path(resolve_scope(tmp_path)).exists()
    assert triage_json_path(resolve_scope(tmp_path)).exists()

    assert _run_cli(["triage", "--cwd", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "candidates:" in out


def test_triage_writes_only_hki_artifacts(tmp_path):
    (tmp_path / "notes.txt").write_text("Hermes Reuben HKI\n", encoding="utf-8")

    triage_inbox(resolve_scope(tmp_path))

    generated = sorted(path.relative_to(tmp_path).as_posix() for path in (tmp_path / ".hermes").rglob("*") if path.is_file())
    assert generated == [
        ".hermes/hki/inventory.json",
        ".hermes/hki/manifest.json",
        ".hermes/hki/reports/intake-triage.md",
        ".hermes/hki/triage/latest-triage.json",
    ]
