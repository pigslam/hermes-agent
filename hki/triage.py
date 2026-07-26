"""First-pass HKI inbox/workbench triage."""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hki.inventory import build_inventory, load_inventory, utc_now_iso, write_inventory
from hki.manifest import Manifest, SourceRecord, build_manifest, load_manifest, write_manifest
from hki.paths import HkiScope, as_workspace_relative
from hki.redaction import redact_json_artifact, redact_markdown_report
from hki.search import SearchResult, search_manifest, slug_for_query


TRIAGE_REPORT_RELATIVE_PATH = Path("reports") / "intake-triage.md"
TRIAGE_JSON_RELATIVE_PATH = Path("triage") / "latest-triage.json"
PARSED_ARTIFACT_NAMES = frozenset(
    {
        "projects.json",
        "entities.json",
        "tasks.json",
        "events.json",
        "records.jsonl",
    }
)


@dataclass(frozen=True)
class ProbeFamily:
    title: str
    category: str
    terms: tuple[str, ...]


PROBE_FAMILIES = (
    ProbeFamily(
        "WireGuard / Network / Router / Camera Access",
        "operational topics",
        ("WireGuard", "AllowedIPs", "travelrouter", "GL.iNet", "WGCC", "camera", "RTSP", "subnet"),
    ),
    ProbeFamily(
        "Local AI / Hermes / ToBAI / HKI Infrastructure",
        "build/research topics",
        ("Hermes", "Reuben", "Cherise", "ToBAI", "Chik", "Ramon", "TheRidge", "KVMBox", "llama"),
    ),
    ProbeFamily(
        "Woodworking / Cut Lists",
        "project-like groupings",
        ("butcher block", "cut list", "seam", "board", "cabinetry"),
    ),
    ProbeFamily(
        "Tile / Bathroom Work",
        "project-like groupings",
        ("tile", "marble", "thinset", "RedGard", "shower", "niche"),
    ),
    ProbeFamily(
        "Gaming / eGPU / Valheim",
        "operational topics",
        ("Valheim", "Steam", "Proton", "eGPU", "RX 9070"),
    ),
    ProbeFamily(
        "Home / Finance / Property",
        "project-like groupings",
        ("home purchase", "family finance", "mortgage", "condo"),
    ),
    ProbeFamily(
        "Vehicle / Repair",
        "project-like groupings",
        ("Celica", "Dwyer", "refrigerator", "repair"),
    ),
)


@dataclass(frozen=True)
class TriageEvidence:
    source_id: str
    relative_path: str
    line: int
    snippet: str
    matched_terms: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "relative_path": self.relative_path,
            "line": self.line,
            "snippet": self.snippet,
            "matched_terms": list(self.matched_terms),
        }


@dataclass(frozen=True)
class TriageCandidate:
    title: str
    evidence_strength: str
    key_terms: tuple[str, ...]
    evidence: tuple[TriageEvidence, ...]
    suggested_dossier_slug: str
    suggested_action: str
    category: str
    source: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "evidence_strength": self.evidence_strength,
            "key_terms": list(self.key_terms),
            "evidence": [item.to_dict() for item in self.evidence],
            "suggested_dossier_slug": self.suggested_dossier_slug,
            "suggested_action": self.suggested_action,
            "category": self.category,
            "source": self.source,
        }


@dataclass(frozen=True)
class TriageRun:
    root: str
    generated_at: str
    manifest_path: str
    report_path: Path
    json_path: Path
    source_count: int
    skipped_count: int
    kind_counts: dict[str, int]
    largest_sources: tuple[SourceRecord, ...]
    artifacts: tuple[str, ...]
    candidates: tuple[TriageCandidate, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "root": self.root,
            "manifest_path": self.manifest_path,
            "report_path": self.report_path.as_posix(),
            "source_count": self.source_count,
            "skipped_count": self.skipped_count,
            "kind_counts": dict(sorted(self.kind_counts.items())),
            "largest_sources": [
                {
                    "source_id": source.source_id,
                    "relative_path": source.relative_path,
                    "kind": source.kind,
                    "size": source.size,
                }
                for source in self.largest_sources
            ],
            "artifacts": list(self.artifacts),
            "candidates": [candidate.to_dict() for candidate in self.candidates],
        }


def triage_inbox(scope: HkiScope) -> TriageRun:
    """Create a first-pass intake triage report for ``scope``."""

    manifest = _load_or_create_manifest(scope)
    candidates = _build_candidates(manifest, scope)
    run = TriageRun(
        root=str(scope.root.resolve()),
        generated_at=utc_now_iso(),
        manifest_path=as_workspace_relative(scope.manifest_path, scope.root.resolve()),
        report_path=triage_report_path(scope),
        json_path=triage_json_path(scope),
        source_count=len(manifest.sources),
        skipped_count=manifest.inventory_skipped_count,
        kind_counts=dict(Counter(source.kind for source in manifest.sources)),
        largest_sources=tuple(
            sorted(manifest.sources, key=lambda source: (-source.size, source.relative_path))[:10]
        ),
        artifacts=tuple(_artifact_paths(manifest)),
        candidates=candidates,
    )
    write_triage(run)
    return run


def write_triage(run: TriageRun) -> tuple[Path, Path]:
    run.report_path.parent.mkdir(parents=True, exist_ok=True)
    run.json_path.parent.mkdir(parents=True, exist_ok=True)
    run.report_path.write_text(redact_markdown_report(build_triage_report(run)).text, encoding="utf-8")
    run.json_path.write_text(
        json.dumps(redact_json_artifact(run.to_dict()), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return run.report_path, run.json_path


def build_triage_report(run: TriageRun) -> str:
    lines = [
        "# HKI Inbox Intake Triage",
        "",
        f"Generated: {run.generated_at}",
        f"Root path: `{run.root}`",
        f"Manifest: `{run.manifest_path}`",
        f"Latest JSON: `{as_workspace_relative(run.json_path, Path(run.root))}`",
        "",
        "## Intake Summary",
        "",
        f"- Source count: {run.source_count}",
        f"- Skipped/excluded count: {run.skipped_count}",
        "",
        "### Major File Kinds",
        "",
    ]
    if run.kind_counts:
        for kind, count in sorted(run.kind_counts.items(), key=lambda item: (-item[1], item[0])):
            lines.append(f"- `{kind}`: {count}")
    else:
        lines.append("- None")

    lines.extend(["", "### Largest Files / Sources", ""])
    if run.largest_sources:
        for source in run.largest_sources:
            lines.append(f"- `{source.relative_path}` ({source.size} bytes, {source.kind})")
    else:
        lines.append("- None")

    lines.extend(["", "### Existing HKI / Parsed Artifacts", ""])
    if run.artifacts:
        for artifact in run.artifacts:
            lines.append(f"- `{artifact}`")
    else:
        lines.append("- None discovered")

    lines.extend(["", "## Candidate Topics / Projects", ""])
    if run.candidates:
        for index, candidate in enumerate(run.candidates, start=1):
            lines.extend(
                [
                    f"### {index}. {candidate.title}",
                    "",
                    f"- Evidence strength: {candidate.evidence_strength}",
                    f"- Suggested action: {candidate.suggested_action}",
                    f"- Suggested dossier slug: `{candidate.suggested_dossier_slug}`",
                    f"- Category: {candidate.category}",
                    f"- Source: {candidate.source}",
                    f"- Key matched terms/entities: {', '.join(candidate.key_terms)}",
                    "",
                    "Representative evidence:",
                    "",
                ]
            )
            for evidence in candidate.evidence:
                terms = ", ".join(evidence.matched_terms)
                lines.append(
                    f"- `{evidence.relative_path}` L{evidence.line}: {evidence.snippet} (matched: {terms})"
                )
            lines.append("")
    else:
        lines.extend(
            [
                "No strong candidate topics found. This may be a sparse inbox, a binary-heavy folder, or material whose vocabulary is outside the current seed probes.",
                "",
            ]
        )

    lines.extend(
        [
            "## Suggested Organization",
            "",
        ]
    )
    for category in (
        "project-like groupings",
        "operational topics",
        "build/research topics",
        "incidental/noisy topics",
    ):
        lines.extend([f"### {category.title()}", ""])
        category_candidates = [candidate for candidate in run.candidates if candidate.category == category]
        if category_candidates:
            for candidate in category_candidates:
                lines.append(f"- {candidate.title} ({candidate.evidence_strength}, {candidate.suggested_action})")
        else:
            lines.append("- None suggested")
        lines.append("")

    lines.extend(
        [
            "## Next-Step Prompt",
            "",
            "Choose which topics should become dossiers. You can rename topics, merge related topics, split broad topics, ignore noisy topics, or mark topics sensitive/private before any dossier generation.",
            "",
            "Suggested reply format:",
            "",
            "- Build: <topic names>",
            "- Rename: <old> -> <new>",
            "- Merge: <topic A> + <topic B>",
            "- Split: <topic> into <parts>",
            "- Ignore: <topic names>",
            "- Sensitive/private: <topic names>",
            "",
            "## Limitations",
            "",
            "This is deterministic first-pass triage using parsed HKI artifacts when present and bounded lexical probe families. It is not semantic clustering, does not call an LLM, does not ingest GitHub, and does not automatically build dossiers.",
            "",
        ]
    )
    return "\n".join(lines)


def triage_report_path(scope: HkiScope) -> Path:
    return scope.output_dir / TRIAGE_REPORT_RELATIVE_PATH


def triage_json_path(scope: HkiScope) -> Path:
    return scope.output_dir / TRIAGE_JSON_RELATIVE_PATH


def _build_candidates(manifest: Manifest, scope: HkiScope) -> tuple[TriageCandidate, ...]:
    candidates = list(_artifact_candidates(manifest, scope))
    candidates.extend(_probe_candidates(manifest, scope))
    candidates.sort(key=_candidate_sort_key)
    return tuple(candidates)


def _probe_candidates(manifest: Manifest, scope: HkiScope) -> tuple[TriageCandidate, ...]:
    candidates: list[TriageCandidate] = []
    for probe in PROBE_FAMILIES:
        evidence_by_key: dict[tuple[str, str, int, str], dict[str, object]] = {}
        matched_terms: Counter[str] = Counter()
        for term in probe.terms:
            run = search_manifest(manifest, scope, term, manifest_path=scope.manifest_path)
            for result in run.results[:5]:
                for match in result.matches[:2]:
                    if not _valid_probe_match(term, match.snippet):
                        continue
                    key = (result.source_id, result.relative_path, match.line, match.snippet)
                    matched = tuple(match.matched_terms or (term,))
                    matched_terms.update(matched)
                    item = evidence_by_key.setdefault(
                        key,
                        {
                            "source_id": result.source_id,
                            "relative_path": result.relative_path,
                            "line": match.line,
                            "snippet": match.snippet,
                            "matched_terms": [],
                        },
                    )
                    term_list = item["matched_terms"]
                    if isinstance(term_list, list):
                        for matched_term in matched:
                            if matched_term not in term_list:
                                term_list.append(matched_term)
        evidence = tuple(
            sorted(
                (
                    TriageEvidence(
                        source_id=str(item["source_id"]),
                        relative_path=str(item["relative_path"]),
                        line=int(item["line"]),
                        snippet=str(item["snippet"]),
                        matched_terms=tuple(item["matched_terms"])
                        if isinstance(item["matched_terms"], list)
                        else (),
                    )
                    for item in evidence_by_key.values()
                ),
                key=lambda item: (item.relative_path, item.line, item.snippet),
            )[:6]
        )
        if not evidence:
            continue
        if not _has_probe_breadth(tuple(matched_terms)):
            continue
        strength = _strength_for_evidence(evidence, tuple(matched_terms))
        candidates.append(
            TriageCandidate(
                title=probe.title,
                evidence_strength=strength,
                key_terms=tuple(term for term, _count in matched_terms.most_common(8)),
                evidence=evidence,
                suggested_dossier_slug=slug_for_query(probe.title),
                suggested_action=_action_for_strength(strength),
                category=probe.category,
                source="lexical probes",
            )
        )
    return tuple(candidates)


def _artifact_candidates(manifest: Manifest, scope: HkiScope) -> tuple[TriageCandidate, ...]:
    candidates: list[TriageCandidate] = []
    for source in manifest.sources:
        if Path(source.relative_path).name not in PARSED_ARTIFACT_NAMES:
            continue
        path = scope.root / source.relative_path
        for title, terms in _candidate_titles_from_artifact(path):
            evidence = (
                TriageEvidence(
                    source_id=source.source_id,
                    relative_path=source.relative_path,
                    line=1,
                    snippet=f"Parsed artifact mentions: {title}",
                    matched_terms=terms,
                ),
            )
            candidates.append(
                TriageCandidate(
                    title=title,
                    evidence_strength="medium",
                    key_terms=terms,
                    evidence=evidence,
                    suggested_dossier_slug=slug_for_query(title),
                    suggested_action="review",
                    category="project-like groupings",
                    source="parsed artifacts",
                )
            )
    return tuple(_dedupe_candidates(candidates))


def _candidate_titles_from_artifact(path: Path) -> tuple[tuple[str, tuple[str, ...]], ...]:
    try:
        if path.suffix == ".jsonl":
            records = []
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if len(records) >= 20:
                        break
                    stripped = line.strip()
                    if not stripped:
                        continue
                    try:
                        records.append(json.loads(stripped))
                    except json.JSONDecodeError:
                        continue
            data: Any = records
        else:
            data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return ()

    titles: list[tuple[str, tuple[str, ...]]] = []
    for item in _iter_candidate_objects(data):
        title = _title_from_object(item)
        if not title or not _useful_artifact_title(title):
            continue
        terms = tuple(dict.fromkeys(_tokens(title)))[:8]
        titles.append((title, terms or (title,)))
        if len(titles) >= 10:
            break
    if titles:
        return tuple(titles)

    frequent = [
        token
        for token, count in Counter(_tokens_from_json(data)).most_common(8)
        if count >= 2 and len(token) > 3 and "/" not in token and "\\" not in token
    ]
    if not frequent:
        return ()
    title = "Repeated Artifact Entities: " + ", ".join(frequent[:4])
    return ((title, tuple(frequent[:8])),)


def _iter_candidate_objects(data: Any):
    if isinstance(data, dict):
        if any(key in data for key in ("name", "title", "project", "topic", "entity")):
            yield data
        for value in data.values():
            yield from _iter_candidate_objects(value)
    elif isinstance(data, list):
        for item in data:
            yield from _iter_candidate_objects(item)


def _title_from_object(item: dict[str, Any]) -> str | None:
    for key in ("title", "name", "project", "topic", "entity"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return " ".join(value.strip().split())[:120]
    return None


def _useful_artifact_title(title: str) -> bool:
    if "/" in title or "\\" in title:
        return False
    if not re.search(r"[A-Za-z]", title):
        return False
    alnum = re.sub(r"[^A-Za-z0-9]+", "", title)
    if not alnum:
        return False
    digits = sum(ch.isdigit() for ch in alnum)
    return digits / len(alnum) <= 0.6


def _tokens_from_json(data: Any) -> tuple[str, ...]:
    tokens: list[str] = []
    if isinstance(data, dict):
        for value in data.values():
            tokens.extend(_tokens_from_json(value))
    elif isinstance(data, list):
        for item in data:
            tokens.extend(_tokens_from_json(item))
    elif isinstance(data, str):
        tokens.extend(_tokens(data))
    return tuple(tokens)


def _tokens(value: str) -> tuple[str, ...]:
    stop = {"the", "and", "for", "with", "from", "this", "that", "into", "about"}
    return tuple(
        token
        for token in re.findall(r"[a-z0-9][a-z0-9_.:/-]*", value.lower())
        if token not in stop
    )


def _valid_probe_match(term: str, snippet: str) -> bool:
    term_tokens = _tokens(term)
    snippet_tokens = set(_tokens(snippet))
    return bool(term_tokens) and all(token in snippet_tokens for token in term_tokens)


def _dedupe_candidates(candidates: list[TriageCandidate]) -> tuple[TriageCandidate, ...]:
    seen: set[str] = set()
    deduped: list[TriageCandidate] = []
    for candidate in candidates:
        key = candidate.title.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(candidate)
    return tuple(deduped)


def _artifact_paths(manifest: Manifest) -> list[str]:
    return sorted(
        source.relative_path
        for source in manifest.sources
        if Path(source.relative_path).name in PARSED_ARTIFACT_NAMES
    )


def _strength_for_evidence(evidence: tuple[TriageEvidence, ...], matched_terms: tuple[str, ...] = ()) -> str:
    unique_paths = {item.relative_path for item in evidence}
    unique_terms = set(matched_terms)
    if len(unique_paths) >= 3 or len(evidence) >= 5 or len(unique_terms) >= 5:
        return "high"
    if len(unique_paths) >= 2 or len(evidence) >= 2 or len(unique_terms) >= 3:
        return "medium"
    return "low"


def _has_probe_breadth(matched_terms: tuple[str, ...]) -> bool:
    return len(set(matched_terms)) >= 2


def _action_for_strength(strength: str) -> str:
    if strength == "high":
        return "build"
    if strength == "medium":
        return "review"
    return "review"


def _candidate_sort_key(candidate: TriageCandidate) -> tuple[int, str, str]:
    strength_order = {"high": 0, "medium": 1, "low": 2}
    source_order = {"parsed artifacts": 0, "lexical probes": 1}
    return (
        strength_order.get(candidate.evidence_strength, 3),
        source_order.get(candidate.source, 2),
        candidate.title.lower(),
    )


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
