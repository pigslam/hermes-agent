"""First-pass HKI topic timeline generation."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hki.dossier import (
    DossierEvidence,
    EvidenceMatch,
    EvidenceResult,
    gather_dossier_evidence,
    generate_dossier_queries,
)
from hki.inventory import build_inventory, load_inventory, utc_now_iso, write_inventory
from hki.manifest import Manifest, SourceRecord, build_manifest, load_manifest, write_manifest
from hki.paths import HkiScope, as_workspace_relative
from hki.redaction import redact_json_artifact, redact_markdown_report
from hki.search import slug_for_query


TIMELINE_RELATIVE_DIR = Path("timeline")
LATEST_TIMELINE_RELATIVE_PATH = TIMELINE_RELATIVE_DIR / "latest-timeline.json"
TIMELINE_REPORT_RELATIVE_DIR = Path("reports")
RECORDS_RELATIVE_PATH = Path("hki") / "records.jsonl"
MAX_RECORD_TIMELINE_ITEMS = 300
LOW_VALUE_SINGLE_RECORD_QUERIES = frozenset(
    {
        "camera",
        "config",
        "configuration",
        "file",
        "network",
        "project",
        "setup",
        "task",
    }
)

TIMESTAMP_PATTERNS = (
    re.compile(r"\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z\b"),
    re.compile(r"\b\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}\b"),
    re.compile(r"\b\d{4}-\d{2}-\d{2}\b"),
)

CONFIG_TERMS = (
    "allowedips",
    "wireguard",
    "wg",
    "subnet",
    "route",
    "router",
    "peer",
    "config",
    "firewall",
    "forwarding",
    "nat",
)
CORRECTION_TERMS = (
    "fix",
    "fixed",
    "correct",
    "correction",
    "update",
    "updated",
    "change",
    "changed",
    "later",
    "now",
    "actually",
)
CONFLICT_TERMS = (
    "conflict",
    "conflicting",
    "stale",
    "superseded",
    "wrong",
    "instead",
    "but",
    "however",
)


@dataclass(frozen=True)
class TimelineItem:
    source_id: str
    relative_path: str
    line: int
    snippet: str
    queries: tuple[str, ...]
    timestamp: str | None
    timestamp_source: str
    group: str
    record_id: str | None = None
    record_type: str | None = None
    title: str | None = None
    speaker: str | None = None
    updated_at: str | None = None
    conversation_id: str | None = None
    message_id: str | None = None
    conversation_index: int | None = None
    message_index: int | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "source_id": self.source_id,
            "relative_path": self.relative_path,
            "line": self.line,
            "snippet": self.snippet,
            "queries": list(self.queries),
            "timestamp": self.timestamp,
            "timestamp_source": self.timestamp_source,
            "group": self.group,
        }
        for key, value in (
            ("record_id", self.record_id),
            ("record_type", self.record_type),
            ("title", self.title),
            ("speaker", self.speaker),
            ("updated_at", self.updated_at),
            ("conversation_id", self.conversation_id),
            ("message_id", self.message_id),
            ("conversation_index", self.conversation_index),
            ("message_index", self.message_index),
        ):
            if value is not None:
                data[key] = value
        return data


@dataclass(frozen=True)
class TimelineRun:
    topic: str
    slug: str
    root: str
    generated_at: str
    manifest_path: str
    json_path: Path
    report_path: Path
    queries: tuple[str, ...]
    items: tuple[TimelineItem, ...]
    records_source: str | None = None
    records_scanned: int = 0
    records_matched: int = 0
    records_malformed: int = 0
    ordering_keys: tuple[str, ...] = ("timestamp", "relative_path", "line", "snippet")

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "topic": self.topic,
            "slug": self.slug,
            "root": self.root,
            "manifest_path": self.manifest_path,
            "json_path": self.json_path.as_posix(),
            "report_path": self.report_path.as_posix(),
            "queries": list(self.queries),
            "records_source": self.records_source,
            "records_scanned": self.records_scanned,
            "records_matched": self.records_matched,
            "records_malformed": self.records_malformed,
            "ordering_keys": list(self.ordering_keys),
            "items": [item.to_dict() for item in self.items],
            "groups": {
                group: [item.to_dict() for item in _items_for_group(self.items, group)]
                for group in _group_order()
            },
        }


def build_timeline(scope: HkiScope, topic: str) -> TimelineRun:
    """Build and write a first-pass chronological evidence report for ``topic``."""

    normalized_topic = topic.strip()
    if not normalized_topic:
        raise ValueError("timeline topic must not be empty")
    manifest = _load_or_create_manifest(scope)
    records_path = _records_jsonl_path(scope)
    if records_path.exists():
        run = timeline_from_records(scope, manifest, normalized_topic, records_path)
        write_timeline(run)
        return run
    evidence = gather_dossier_evidence(manifest, scope, normalized_topic)
    run = timeline_from_evidence(scope, manifest, evidence)
    write_timeline(run)
    return run


def timeline_from_evidence(
    scope: HkiScope,
    manifest: Manifest,
    evidence: DossierEvidence,
) -> TimelineRun:
    source_by_key = {
        (source.source_id, source.relative_path): source
        for source in manifest.sources
    }
    items: list[TimelineItem] = []
    for result, matches in _timeline_evidence_matches(evidence):
        source = source_by_key.get((result.source_id, result.relative_path))
        for match in matches:
            timestamp, timestamp_source = _timestamp_for_match(match, source)
            item = TimelineItem(
                source_id=result.source_id,
                relative_path=result.relative_path,
                line=match.line,
                snippet=match.snippet,
                queries=match.queries,
                timestamp=timestamp,
                timestamp_source=timestamp_source,
                group=_group_for_match(match),
            )
            items.append(item)

    items.sort(key=_timeline_sort_key)
    slug = slug_for_query(evidence.topic)
    return TimelineRun(
        topic=evidence.topic,
        slug=slug,
        root=str(scope.root.resolve()),
        generated_at=utc_now_iso(),
        manifest_path=as_workspace_relative(scope.manifest_path, scope.root.resolve()),
        json_path=latest_timeline_path(scope),
        report_path=timeline_report_path(scope, slug),
        queries=evidence.queries,
        items=tuple(items),
    )


def timeline_from_records(
    scope: HkiScope,
    _manifest: Manifest,
    topic: str,
    records_path: Path,
) -> TimelineRun:
    """Build a timeline by streaming normalized HKI records."""

    queries = _timeline_record_queries(topic)
    scanned = 0
    matched = 0
    malformed = 0
    items: list[TimelineItem] = []

    with records_path.open("r", encoding="utf-8", errors="replace") as handle:
        for line_number, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            scanned += 1
            try:
                record = json.loads(stripped)
            except json.JSONDecodeError:
                malformed += 1
                continue
            if not isinstance(record, dict):
                malformed += 1
                continue

            labels = _matching_query_labels(record, queries)
            if not labels:
                continue
            matched += 1
            items.append(_timeline_item_from_record(scope, records_path, line_number, record, labels))

    items.sort(key=_timeline_sort_key)
    slug = slug_for_query(topic)
    root = scope.root.resolve()
    return TimelineRun(
        topic=topic,
        slug=slug,
        root=str(root),
        generated_at=utc_now_iso(),
        manifest_path=as_workspace_relative(scope.manifest_path, root),
        json_path=latest_timeline_path(scope),
        report_path=timeline_report_path(scope, slug),
        queries=queries,
        items=_bound_record_items(tuple(items), limit=MAX_RECORD_TIMELINE_ITEMS),
        records_source=as_workspace_relative(records_path, root),
        records_scanned=scanned,
        records_matched=matched,
        records_malformed=malformed,
        ordering_keys=(
            "created_at",
            "metadata.conversation_index",
            "metadata.message_index",
            "source_file",
            "records.jsonl line",
        ),
    )


def _timeline_evidence_matches(evidence: DossierEvidence) -> tuple[tuple[EvidenceResult, tuple[EvidenceMatch, ...]], ...]:
    preferred: list[tuple[EvidenceResult, tuple[EvidenceMatch, ...]]] = []
    fallback: list[tuple[EvidenceResult, tuple[EvidenceMatch, ...]]] = []
    for result in evidence.results:
        multi_query_matches = tuple(
            match for match in result.matches if any(_query_token_count(query) >= 2 for query in match.queries)
        )
        if multi_query_matches:
            preferred.append((result, multi_query_matches))
        fallback.append((result, result.matches))
    return tuple(preferred or fallback)


def write_timeline(run: TimelineRun) -> tuple[Path, Path]:
    run.json_path.parent.mkdir(parents=True, exist_ok=True)
    run.report_path.parent.mkdir(parents=True, exist_ok=True)
    run.json_path.write_text(
        json.dumps(redact_json_artifact(run.to_dict()), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    run.report_path.write_text(redact_markdown_report(build_timeline_report(run)).text, encoding="utf-8")
    return run.json_path, run.report_path


def build_timeline_report(run: TimelineRun) -> str:
    lines = [
        f"# HKI Timeline: {_markdown_inline(run.topic)}",
        "",
        f"Generated: {run.generated_at}",
        f"Topic/query: `{_markdown_inline(run.topic)}`",
        f"Workspace root: `{run.root}`",
        f"Manifest: `{run.manifest_path}`",
        f"Timeline JSON: `{as_workspace_relative(run.json_path, Path(run.root))}`",
        "",
        "## Records Source",
        "",
    ]
    if run.records_source is None:
        lines.append("- `hki/records.jsonl` was not used; timeline used bounded search evidence.")
    else:
        lines.extend(
            [
                f"- Used normalized records source: `{run.records_source}`",
                f"- Records scanned: {run.records_scanned}",
                f"- Matching records: {run.records_matched}",
                f"- Malformed JSON lines: {run.records_malformed}",
                f"- Ordering keys: {', '.join(f'`{key}`' for key in run.ordering_keys)}",
            ]
        )

    lines.extend(
        [
            "",
            "## Search / Evidence Queries",
            "",
        ]
    )
    for query in run.queries:
        lines.append(f"- `{_markdown_inline(query)}`")

    lines.extend(["", "## Chronological Evidence Table", ""])
    if run.items:
        lines.extend(
            [
                "| Time | Source | Line | Evidence | Query Labels |",
                "| --- | --- | ---: | --- | --- |",
            ]
        )
        for item in run.items:
            labels = ", ".join(f"`{_markdown_inline(query)}`" for query in item.queries)
            lines.append(
                f"| {_time_label(item)} | {_table_source_label(item)} | {item.line} | {_table_cell(item.snippet)} | {labels} |"
            )
    else:
        lines.append("No evidence found for this topic.")

    lines.extend(["", "## Earliest Evidence", ""])
    _append_item_list(lines, _first_dated_or_unknown(run.items, limit=5))

    lines.extend(["", "## Inferred Phases", ""])
    _append_inferred_phases(lines, run.items)

    lines.extend(["", "## Notable State Changes", ""])
    _append_item_list(lines, _items_for_group(run.items, "configuration_changes"))

    lines.extend(["", "## Stale / Superseded Candidates", ""])
    stale = tuple(item for item in _items_for_group(run.items, "later_corrections") if item.timestamp is not None)
    _append_item_list(lines, stale)

    lines.extend(["", "## Conflicts", ""])
    _append_item_list(lines, _items_for_group(run.items, "conflicting_facts"))

    lines.extend(["", "## Likely Current-State Summary", ""])
    latest = _latest_dated(run.items, limit=5)
    if latest:
        lines.append("Latest located evidence suggests the following candidates, but this is not live verification:")
        lines.append("")
        _append_item_list(lines, latest)
    elif run.items:
        lines.append("Only undated evidence was located. Treat all current-state claims as unverified.")
    else:
        lines.append("No located evidence is available for a current-state guess.")

    lines.extend(["", "## Needs Live Verification", ""])
    for item in _live_verification_checklist(run.topic):
        lines.append(f"- {item}")

    lines.extend(
        [
            "",
            "## Limitations",
            "",
            "This timeline is deterministic and source-based. It uses HKI lexical evidence, inline/export-like timestamps when easy to locate, and file mtimes as fallback. It does not prove current state, perform semantic temporal reasoning, inspect GitHub, or run live network checks. Phrase conclusions as 'latest located evidence suggests...' unless live verification has been performed.",
            "",
        ]
    )
    return "\n".join(lines)


def latest_timeline_path(scope: HkiScope) -> Path:
    return scope.output_dir / LATEST_TIMELINE_RELATIVE_PATH


def timeline_report_path(scope: HkiScope, slug: str) -> Path:
    return scope.output_dir / TIMELINE_REPORT_RELATIVE_DIR / f"timeline-{slug}.md"


def _timestamp_for_match(match: EvidenceMatch, source: SourceRecord | None) -> tuple[str | None, str]:
    inline = _timestamp_from_text(match.snippet)
    if inline is not None:
        return inline, "snippet"
    if source is not None and source.mtime > 0:
        return _iso_from_epoch(source.mtime), "file_mtime"
    return None, "unknown"


def _timeline_item_from_record(
    scope: HkiScope,
    records_path: Path,
    line_number: int,
    record: dict[str, Any],
    labels: tuple[str, ...],
) -> TimelineItem:
    metadata = record.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    source_file = _string_or_none(record.get("source_file"))
    relative_path = _record_relative_path(scope, records_path, source_file)
    record_id = _string_or_none(record.get("record_id"))
    snippet = _record_snippet(record)
    timestamp = _string_or_none(record.get("created_at"))
    timestamp_source = "record.created_at" if timestamp else "unknown"
    match = EvidenceMatch(line=line_number, snippet=snippet, queries=labels, score=len(labels))
    return TimelineItem(
        source_id=record_id or _source_id_for_record(relative_path, line_number),
        relative_path=relative_path,
        line=line_number,
        snippet=snippet,
        queries=labels,
        timestamp=timestamp,
        timestamp_source=timestamp_source,
        group=_group_for_match(match),
        record_id=record_id,
        record_type=_string_or_none(record.get("record_type")),
        title=_string_or_none(record.get("title")),
        speaker=_string_or_none(record.get("speaker")),
        updated_at=_string_or_none(record.get("updated_at")),
        conversation_id=_string_or_none(metadata.get("conversation_id")),
        message_id=_string_or_none(metadata.get("message_id")),
        conversation_index=_int_or_none(metadata.get("conversation_index")),
        message_index=_int_or_none(metadata.get("message_index")),
    )


def _timestamp_from_text(text: str) -> str | None:
    for pattern in TIMESTAMP_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        value = match.group(0)
        if "T" not in value and len(value) == 10:
            return value + "T00:00:00Z"
        if value.endswith("Z"):
            return value
        return value.replace(" ", "T") + "Z"
    return None


def _iso_from_epoch(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _group_for_match(match: EvidenceMatch) -> str:
    text = match.snippet.lower()
    if any(term in text for term in CONFLICT_TERMS):
        return "conflicting_facts"
    if any(term in text for term in CORRECTION_TERMS):
        return "later_corrections"
    if any(term in text for term in CONFIG_TERMS):
        return "configuration_changes"
    return "earliest_evidence"


def _timeline_sort_key(item: TimelineItem) -> tuple[int, str, int, int, str, int, str]:
    return (
        1 if item.timestamp is None else 0,
        item.timestamp or "",
        _none_last_int(item.conversation_index),
        _none_last_int(item.message_index),
        item.relative_path,
        item.line,
        item.snippet,
    )


def _items_for_group(items: tuple[TimelineItem, ...], group: str) -> tuple[TimelineItem, ...]:
    return tuple(item for item in items if item.group == group)


def _group_order() -> tuple[str, ...]:
    return (
        "earliest_evidence",
        "configuration_changes",
        "later_corrections",
        "conflicting_facts",
    )


def _first_dated_or_unknown(items: tuple[TimelineItem, ...], *, limit: int) -> tuple[TimelineItem, ...]:
    return tuple(items[:limit])


def _latest_dated(items: tuple[TimelineItem, ...], *, limit: int) -> tuple[TimelineItem, ...]:
    dated = [item for item in items if item.timestamp is not None]
    dated.sort(
        key=lambda item: (
            item.timestamp or "",
            _none_last_int(item.conversation_index),
            _none_last_int(item.message_index),
            item.relative_path,
            item.line,
        ),
        reverse=True,
    )
    return tuple(dated[:limit])


def _append_item_list(lines: list[str], items: tuple[TimelineItem, ...]) -> None:
    if not items:
        lines.append("- None located")
        return
    for item in items:
        labels = ", ".join(item.queries)
        lines.append(
            f"- {_time_label(item)} `{item.relative_path}`{_record_order_suffix(item)} L{item.line}: {item.snippet} "
            f"(source `{item.source_id}`, queries: {labels})"
        )


def _time_label(item: TimelineItem) -> str:
    if item.timestamp is None:
        return "unknown"
    return f"{item.timestamp} ({item.timestamp_source})"


def _live_verification_checklist(topic: str) -> tuple[str, ...]:
    lowered = topic.lower()
    generic = [
        "Confirm whether the latest located evidence still reflects the live system.",
        "Check authoritative runtime/config sources before treating any fact as current.",
        "Record any verified current-state facts in a new dossier or timeline follow-up.",
    ]
    if any(term in lowered for term in ("wireguard", "vpn", "router", "camera", "rtsp", "subnet")):
        generic.extend(
            [
                "`wg show` on relevant WireGuard hosts",
                "router WireGuard peer config",
                "route table on both sides of the tunnel",
                "firewall/forwarding/NAT rules",
                "ping/traceroute across tunnel subnets",
            ]
        )
    if any(term in lowered for term in ("camera", "rtsp")):
        generic.append("camera/RTSP endpoint reachability")
    return tuple(generic)


def _query_token_count(query: str) -> int:
    return len(re.findall(r"[a-z0-9][a-z0-9_.:/-]*", query.lower()))


def _load_or_create_manifest(scope: HkiScope) -> Manifest:
    if scope.manifest_path.exists():
        return load_manifest(scope.manifest_path)
    if scope.inventory_path.exists():
        inventory = load_inventory(scope.inventory_path)
    else:
        inventory = build_inventory(scope)
        write_inventory(inventory, scope)
    manifest = build_manifest(inventory, inventory_path=scope.inventory_path)
    write_manifest(manifest, scope)
    return manifest


def _records_jsonl_path(scope: HkiScope) -> Path:
    return scope.root.resolve() / RECORDS_RELATIVE_PATH


def _matching_query_labels(record: dict[str, Any], queries: tuple[str, ...]) -> tuple[str, ...]:
    haystack = _record_haystack(record)
    labels: list[str] = []
    for query in queries:
        if _query_matches_text(query, haystack):
            labels.append(query)
    return tuple(labels)


def _timeline_record_queries(topic: str) -> tuple[str, ...]:
    queries = list(generate_dossier_queries(topic))
    lowered = topic.lower()
    if "wireguard" in lowered and "vpn" not in {query.lower() for query in queries}:
        queries.append("vpn")
    if "camera" in lowered and "rtsp" not in {query.lower() for query in queries}:
        queries.append("rtsp")
    return tuple(queries)


def _query_matches_text(query: str, haystack: str) -> bool:
    needle = query.strip().lower()
    if not needle:
        return False
    if needle in haystack:
        return True
    terms = re.findall(r"[a-z0-9][a-z0-9_.:/-]*", needle)
    return bool(terms) and all(term in haystack for term in terms)


def _record_haystack(record: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("title", "text", "content", "body", "speaker", "source_file", "source_path", "path"):
        value = record.get(key)
        if isinstance(value, str):
            parts.append(value)
    return "\n".join(parts).lower()


def _record_snippet(record: dict[str, Any]) -> str:
    for key in ("text", "content", "body", "title"):
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return _compact(value, limit=220)
    return "(no text)"


def _record_relative_path(scope: HkiScope, records_path: Path, source_file: str | None) -> str:
    if not source_file:
        return as_workspace_relative(records_path, scope.root.resolve())
    path = Path(source_file)
    if path.is_absolute():
        return as_workspace_relative(path, scope.root.resolve())
    return path.as_posix()


def _source_id_for_record(relative_path: str, line_number: int) -> str:
    return f"record:{relative_path}:{line_number}"


def _string_or_none(value: object) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None


def _int_or_none(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _none_last_int(value: int | None) -> int:
    return value if value is not None else 1_000_000_000


def _bound_record_items(items: tuple[TimelineItem, ...], *, limit: int) -> tuple[TimelineItem, ...]:
    preferred = tuple(item for item in items if _preferred_record_item(item))
    if preferred:
        items = preferred
    if len(items) <= limit:
        return items
    anchors = tuple(item for item in items if item.record_type == "conversation" or item.title)
    if len(anchors) >= limit:
        return tuple(sorted(_evenly_spaced_items(anchors, limit=limit), key=_timeline_sort_key))
    edge = max(1, limit // 4)
    selected_by_key = {_item_identity(item): item for item in anchors}
    remaining_limit = limit - len(selected_by_key)
    middle_limit = max(0, remaining_limit - (edge * 2))
    for item in list(items[:edge]) + _evenly_spaced_items(items[edge:-edge], limit=middle_limit) + list(items[-edge:]):
        if len(selected_by_key) >= limit:
            break
        selected_by_key.setdefault(_item_identity(item), item)
    selected = list(selected_by_key.values())
    selected.sort(key=_timeline_sort_key)
    return tuple(selected)


def _evenly_spaced_items(items: tuple[TimelineItem, ...], *, limit: int) -> list[TimelineItem]:
    if limit <= 0 or not items:
        return []
    if len(items) <= limit:
        return list(items)
    if limit == 1:
        return [items[len(items) // 2]]
    last_index = len(items) - 1
    indexes = {
        round(index * last_index / (limit - 1))
        for index in range(limit)
    }
    return [items[index] for index in sorted(indexes)]


def _preferred_record_item(item: TimelineItem) -> bool:
    for query in item.queries:
        if _query_token_count(query) >= 2:
            return True
        if query.lower() not in LOW_VALUE_SINGLE_RECORD_QUERIES:
            return True
    return False


def _item_identity(item: TimelineItem) -> tuple[str, int, str | None]:
    return (item.relative_path, item.line, item.record_id)


def _append_inferred_phases(lines: list[str], items: tuple[TimelineItem, ...]) -> None:
    if not items:
        lines.append("- None inferred")
        return
    dated = tuple(item for item in items if item.timestamp is not None)
    early = _first_dated_or_unknown(items, limit=3)
    setup = _items_for_group(items, "configuration_changes")[:3]
    troubleshooting = _items_for_group(items, "later_corrections")[:3]
    latest = tuple(reversed(dated[-3:])) if dated else ()
    phase_items = (
        ("Early evidence", early),
        ("Setup/configuration phase", setup),
        ("Troubleshooting/fix phase", troubleshooting),
        ("Latest located evidence", latest),
    )
    for label, phase in phase_items:
        lines.append(f"- {label}:")
        if not phase:
            lines.append("  - None located")
            continue
        for item in phase:
            lines.append(f"  - {_time_label(item)} `{item.relative_path}`{_record_order_suffix(item)}: {item.snippet}")


def _table_source_label(item: TimelineItem) -> str:
    bits = [f"`{item.relative_path}`"]
    details: list[str] = []
    if item.title:
        details.append(_table_cell(item.title))
    if item.speaker:
        details.append(f"speaker: `{_markdown_inline(item.speaker)}`")
    if item.record_type:
        details.append(f"type: `{_markdown_inline(item.record_type)}`")
    if item.conversation_index is not None or item.message_index is not None:
        details.append(f"order: `{_order_label(item)}`")
    if details:
        bits.append("<br>" + "<br>".join(details))
    return "".join(bits)


def _record_order_suffix(item: TimelineItem) -> str:
    if item.conversation_index is None and item.message_index is None:
        return ""
    return f" ({_order_label(item)})"


def _order_label(item: TimelineItem) -> str:
    conversation = "?" if item.conversation_index is None else str(item.conversation_index)
    message = "?" if item.message_index is None else str(item.message_index)
    return f"conversation {conversation}, message {message}"


def _compact(value: str, *, limit: int) -> str:
    compacted = " ".join(value.split())
    if len(compacted) <= limit:
        return compacted
    return compacted[: limit - 3].rstrip() + "..."


def _table_cell(value: str) -> str:
    return " ".join(value.replace("|", "\\|").split())


def _markdown_inline(value: str) -> str:
    return value.replace("`", "'")
