from __future__ import annotations

import argparse
import json
from pathlib import Path

from hermes_cli import hki_cmd
from hki.dossier import DossierEvidence, EvidenceMatch, EvidenceResult
from hki.manifest import Manifest, SourceRecord, source_id_for
from hki.paths import resolve_scope
from hki.timeline import build_timeline, latest_timeline_path, timeline_from_evidence, timeline_report_path


def _write_records(path: Path, records: list[dict[str, object] | str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for record in records:
        if isinstance(record, str):
            lines.append(record)
        else:
            lines.append(json.dumps(record))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _run_cli(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    hki_parser = hki_cmd.build_parser(sub)
    hki_parser.set_defaults(func=hki_cmd.hki_command)
    args = parser.parse_args(["hki", *argv])
    return hki_cmd.hki_command(args)


def test_timeline_report_and_json_creation(tmp_path):
    (tmp_path / "network.md").write_text(
        "2024-01-01 WireGuard AllowedIPs used 10.8.0.0/24\n",
        encoding="utf-8",
    )

    run = build_timeline(resolve_scope(tmp_path), "WireGuard AllowedIPs")

    assert run.report_path == tmp_path / ".hermes" / "hki" / "reports" / "timeline-wireguard-allowedips.md"
    assert run.json_path == tmp_path / ".hermes" / "hki" / "timeline" / "latest-timeline.json"
    assert run.report_path.exists()
    assert run.json_path.exists()
    content = run.report_path.read_text(encoding="utf-8")
    assert "# HKI Timeline: WireGuard AllowedIPs" in content
    assert "## Chronological Evidence Table" in content
    assert "2024-01-01T00:00:00Z" in content
    data = json.loads(run.json_path.read_text(encoding="utf-8"))
    assert data["topic"] == "WireGuard AllowedIPs"
    assert data["items"]


def test_timeline_uses_records_jsonl_when_present(tmp_path):
    records_path = tmp_path / "hki" / "records.jsonl"
    _write_records(
        records_path,
        [
            {
                "record_id": "rec-1",
                "record_type": "message",
                "title": "WireGuard setup",
                "speaker": "user",
                "created_at": "2025-01-01T00:00:00Z",
                "updated_at": "2025-01-01T00:01:00Z",
                "source_file": "hki/expanded/conversations-001.json",
                "text": "WireGuard camera route setup",
                "metadata": {
                    "conversation_id": "conv-1",
                    "message_id": "msg-1",
                    "conversation_index": 2,
                    "message_index": 3,
                },
            }
        ],
    )

    run = build_timeline(resolve_scope(tmp_path), "WireGuard camera")
    data = json.loads(run.json_path.read_text(encoding="utf-8"))
    report = run.report_path.read_text(encoding="utf-8")

    assert run.records_source == "hki/records.jsonl"
    assert data["records_source"] == "hki/records.jsonl"
    assert data["records_scanned"] == 1
    assert data["records_matched"] == 1
    assert data["items"][0]["record_id"] == "rec-1"
    assert data["items"][0]["conversation_index"] == 2
    assert data["items"][0]["message_index"] == 3
    assert "Used normalized records source" in report
    assert "Records scanned: 1" in report
    assert "speaker: `user`" in report


def test_timeline_streams_records_and_ignores_malformed_json(tmp_path):
    _write_records(
        tmp_path / "hki" / "records.jsonl",
        [
            '{"not valid"',
            {
                "record_id": "rec-good",
                "record_type": "message",
                "title": "Camera fix",
                "created_at": "2025-02-01T00:00:00Z",
                "source_file": "conversation.json",
                "text": "camera route fixed",
                "metadata": {},
            },
        ],
    )

    run = build_timeline(resolve_scope(tmp_path), "camera route")

    assert run.records_scanned == 2
    assert run.records_malformed == 1
    assert run.records_matched == 1
    assert [item.record_id for item in run.items] == ["rec-good"]


def test_timeline_records_sort_by_created_at(tmp_path):
    _write_records(
        tmp_path / "hki" / "records.jsonl",
        [
            {
                "record_id": "later",
                "record_type": "message",
                "title": "camera config",
                "created_at": "2025-05-01T00:00:00Z",
                "text": "camera config later",
                "metadata": {"conversation_index": 1, "message_index": 1},
            },
            {
                "record_id": "earlier",
                "record_type": "message",
                "title": "camera config",
                "created_at": "2024-05-01T00:00:00Z",
                "text": "camera config earlier",
                "metadata": {"conversation_index": 9, "message_index": 9},
            },
        ],
    )

    run = build_timeline(resolve_scope(tmp_path), "camera config")

    assert [item.record_id for item in run.items] == ["earlier", "later"]


def test_timeline_records_fallback_order_by_conversation_and_message_index(tmp_path):
    _write_records(
        tmp_path / "hki" / "records.jsonl",
        [
            {
                "record_id": "second",
                "record_type": "message",
                "title": "WireGuard",
                "text": "WireGuard route second",
                "metadata": {"conversation_index": 4, "message_index": 10},
            },
            {
                "record_id": "first",
                "record_type": "message",
                "title": "WireGuard",
                "text": "WireGuard route first",
                "metadata": {"conversation_index": 4, "message_index": 2},
            },
        ],
    )

    run = build_timeline(resolve_scope(tmp_path), "WireGuard route")

    assert [item.record_id for item in run.items] == ["first", "second"]
    assert all(item.timestamp is None for item in run.items)


def test_timeline_records_undated_sort_after_dated(tmp_path):
    _write_records(
        tmp_path / "hki" / "records.jsonl",
        [
            {
                "record_id": "undated",
                "record_type": "message",
                "title": "camera",
                "text": "camera config undated",
                "metadata": {"conversation_index": 1, "message_index": 1},
            },
            {
                "record_id": "dated",
                "record_type": "message",
                "title": "camera",
                "created_at": "2025-01-01T00:00:00Z",
                "text": "camera config dated",
                "metadata": {"conversation_index": 9, "message_index": 9},
            },
        ],
    )

    run = build_timeline(resolve_scope(tmp_path), "camera config")

    assert [item.record_id for item in run.items] == ["dated", "undated"]


def test_timeline_records_no_matches_still_reports_source(tmp_path):
    _write_records(
        tmp_path / "hki" / "records.jsonl",
        [
            {
                "record_id": "unrelated",
                "record_type": "message",
                "title": "Lunch",
                "text": "sandwich notes",
                "metadata": {},
            }
        ],
    )

    run = build_timeline(resolve_scope(tmp_path), "WireGuard camera")
    report = run.report_path.read_text(encoding="utf-8")

    assert run.items == ()
    assert run.records_scanned == 1
    assert run.records_matched == 0
    assert "No evidence found for this topic." in report


def test_timeline_dated_evidence_sorts_chronologically(tmp_path):
    (tmp_path / "notes.txt").write_text(
        "2025-05-01 camera config changed later\n2023-01-01 camera config initial\n",
        encoding="utf-8",
    )

    run = build_timeline(resolve_scope(tmp_path), "camera config")

    assert [item.timestamp for item in run.items[:2]] == [
        "2023-01-01T00:00:00Z",
        "2025-05-01T00:00:00Z",
    ]


def test_timeline_undated_evidence_handling(tmp_path):
    scope = resolve_scope(tmp_path)
    manifest = Manifest(
        root=str(tmp_path.resolve()),
        generated_at="2026-01-01T00:00:00Z",
        sources=(
            SourceRecord(
                source_id=source_id_for("undated.txt"),
                relative_path="undated.txt",
                kind="text",
                size=10,
                mtime=0,
                is_text=True,
            ),
        ),
    )
    evidence = DossierEvidence(
        topic="undated",
        queries=("undated",),
        generated_at="2026-01-01T00:00:00Z",
        searched_file_count=1,
        skipped_by_reason={},
        results=(
            EvidenceResult(
                source_id=source_id_for("undated.txt"),
                relative_path="undated.txt",
                kind="text",
                score=1,
                matches=(EvidenceMatch(line=1, snippet="undated evidence", queries=("undated",), score=1),),
            ),
        ),
        suggested_queries=(),
    )

    run = timeline_from_evidence(scope, manifest, evidence)

    assert run.items[0].timestamp is None
    assert run.items[0].timestamp_source == "unknown"
    assert "unknown" in run.to_dict()["items"][0]["timestamp_source"]


def test_timeline_conflict_and_stale_sections(tmp_path):
    (tmp_path / "network.md").write_text(
        "2024-01-01 WireGuard route was wrong and stale\n"
        "2024-02-01 WireGuard route fixed and updated\n",
        encoding="utf-8",
    )

    run = build_timeline(resolve_scope(tmp_path), "WireGuard route")
    content = run.report_path.read_text(encoding="utf-8")

    assert "## Stale / Superseded Candidates" in content
    assert "fixed and updated" in content
    assert "## Conflicts" in content
    assert "wrong and stale" in content


def test_timeline_topic_with_no_evidence(tmp_path):
    (tmp_path / "notes.txt").write_text("unrelated material\n", encoding="utf-8")

    run = build_timeline(resolve_scope(tmp_path), "WireGuard camera")
    content = run.report_path.read_text(encoding="utf-8")

    assert run.items == ()
    assert "No evidence found for this topic." in content
    assert "No located evidence is available" in content


def test_timeline_wireguard_checklist(tmp_path):
    (tmp_path / "network.md").write_text("2024-01-01 WireGuard camera RTSP subnet\n", encoding="utf-8")

    run = build_timeline(resolve_scope(tmp_path), "Visalia WireGuard camera network")
    content = run.report_path.read_text(encoding="utf-8")

    assert "`wg show`" in content
    assert "router WireGuard peer config" in content
    assert "camera/RTSP endpoint reachability" in content


def test_cli_timeline_writes_expected_outputs(tmp_path, capsys):
    (tmp_path / "network.md").write_text("2024-01-01 WireGuard AllowedIPs\n", encoding="utf-8")

    assert _run_cli(["timeline", "--cwd", str(tmp_path), "WireGuard AllowedIPs"]) == 0
    out = capsys.readouterr().out
    assert "Wrote HKI timeline" in out
    assert latest_timeline_path(resolve_scope(tmp_path)).exists()
    assert timeline_report_path(resolve_scope(tmp_path), "wireguard-allowedips").exists()


def test_timeline_writes_only_hki_artifacts(tmp_path):
    (tmp_path / "network.md").write_text("2024-01-01 WireGuard AllowedIPs\n", encoding="utf-8")

    build_timeline(resolve_scope(tmp_path), "WireGuard AllowedIPs")

    generated = sorted(
        path.relative_to(tmp_path).as_posix()
        for path in (tmp_path / ".hermes").rglob("*")
        if path.is_file()
    )
    assert generated == [
        ".hermes/hki/inventory.json",
        ".hermes/hki/manifest.json",
        ".hermes/hki/reports/timeline-wireguard-allowedips.md",
        ".hermes/hki/timeline/latest-timeline.json",
    ]
