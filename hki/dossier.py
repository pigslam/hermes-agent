"""First-pass HKI dossier generation and publication."""

from __future__ import annotations

import re
import shutil
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from hki.inventory import build_inventory, load_inventory, utc_now_iso, write_inventory
from hki.manifest import build_manifest, load_manifest, write_manifest
from hki.paths import HkiScope, as_workspace_relative
from hki.redaction import redact_markdown_report
from hki.search import (
    SearchLimits,
    SearchMatch,
    SearchResult,
    SearchRun,
    search_manifest,
    slug_for_query,
    write_latest_search,
    write_search_report,
)


DOSSIER_RELATIVE_DIR = Path("reports") / "dossiers"
VAULT_RELATIVE_DIR = Path("vault") / "dossiers"
MAX_DOSSIER_QUERIES = 12

STOP_WORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "how",
        "in",
        "into",
        "is",
        "it",
        "of",
        "on",
        "or",
        "our",
        "should",
        "that",
        "the",
        "this",
        "to",
        "use",
        "uses",
        "using",
        "with",
    }
)


@dataclass(frozen=True)
class DossierRun:
    topic: str
    slug: str
    dossier_path: Path
    manifest_path: Path
    search_report_path: Path
    publication_path: Path | None
    search: SearchRun
    queries: tuple[str, ...]


@dataclass(frozen=True)
class DossierListing:
    path: Path
    relative_path: str
    title: str
    modified_time: int


@dataclass(frozen=True)
class EvidenceMatch:
    line: int
    snippet: str
    queries: tuple[str, ...]
    score: int


@dataclass(frozen=True)
class EvidenceResult:
    source_id: str
    relative_path: str
    kind: str
    score: int
    matches: tuple[EvidenceMatch, ...]


@dataclass(frozen=True)
class DossierEvidence:
    topic: str
    queries: tuple[str, ...]
    generated_at: str
    searched_file_count: int
    skipped_by_reason: dict[str, int]
    results: tuple[EvidenceResult, ...]
    suggested_queries: tuple[str, ...]

    @property
    def result_count(self) -> int:
        return len(self.results)

    @property
    def skipped_file_count(self) -> int:
        return sum(self.skipped_by_reason.values())


def build_dossier(
    scope: HkiScope,
    topic: str,
    *,
    publish: bool = False,
    vault_root: str | Path | None = None,
) -> DossierRun:
    """Create a first-pass Markdown dossier for ``topic`` from HKI search hits."""

    normalized_topic = topic.strip()
    if not normalized_topic:
        raise ValueError("dossier topic must not be empty")

    manifest = _load_or_create_manifest(scope)
    evidence = gather_dossier_evidence(manifest, scope, normalized_topic)
    search = _search_run_from_evidence(evidence, scope)
    write_latest_search(search, scope)
    written_search_report = write_search_report(search, scope)

    slug = slug_for_query(normalized_topic)
    dossier_path = dossier_path_for_slug(scope, slug)
    publication_path = None
    if publish:
        publication_path = publication_path_for_slug(scope, slug, vault_root=vault_root)

    content = build_dossier_markdown(
        search,
        evidence,
        scope,
        dossier_path=dossier_path,
        search_report_path=written_search_report,
        publication_path=publication_path,
    )
    dossier_path.parent.mkdir(parents=True, exist_ok=True)
    dossier_path.write_text(redact_markdown_report(content).text, encoding="utf-8")

    if publication_path is not None:
        publication_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(dossier_path, publication_path)

    return DossierRun(
        topic=normalized_topic,
        slug=slug,
        dossier_path=dossier_path,
        manifest_path=scope.manifest_path,
        search_report_path=written_search_report,
        publication_path=publication_path,
        search=search,
        queries=evidence.queries,
    )


def gather_dossier_evidence(manifest, scope: HkiScope, topic: str) -> DossierEvidence:
    """Run deterministic multi-query lexical searches and merge their hits."""

    queries = generate_dossier_queries(topic)
    skipped: Counter[str] = Counter()
    searched_file_count = 0
    match_map: dict[tuple[str, str, int, str], dict[str, object]] = {}
    result_meta: dict[tuple[str, str], dict[str, object]] = {}

    for query in queries:
        run = search_manifest(manifest, scope, query, manifest_path=scope.manifest_path)
        searched_file_count += run.searched_file_count
        skipped.update(run.skipped_by_reason)
        for result in run.results:
            result_key = (result.source_id, result.relative_path)
            meta = result_meta.setdefault(
                result_key,
                {
                    "source_id": result.source_id,
                    "relative_path": result.relative_path,
                    "kind": result.kind,
                    "score": 0,
                },
            )
            meta["score"] = int(meta["score"]) + result.score
            for match in result.matches:
                match_key = (result.source_id, result.relative_path, match.line, match.snippet)
                existing = match_map.setdefault(
                    match_key,
                    {
                        "line": match.line,
                        "snippet": match.snippet,
                        "score": 0,
                        "queries": [],
                    },
                )
                existing["score"] = int(existing["score"]) + match.score
                query_list = existing["queries"]
                if isinstance(query_list, list) and query not in query_list:
                    query_list.append(query)

    matches_by_result: dict[tuple[str, str], list[EvidenceMatch]] = {}
    for (source_id, relative_path, _line, _snippet), item in match_map.items():
        query_list = item["queries"]
        queries_for_match = tuple(query_list) if isinstance(query_list, list) else ()
        matches_by_result.setdefault((source_id, relative_path), []).append(
            EvidenceMatch(
                line=int(item["line"]),
                snippet=str(item["snippet"]),
                queries=queries_for_match,
                score=int(item["score"]),
            )
        )

    results: list[EvidenceResult] = []
    for result_key, meta in result_meta.items():
        matches = sorted(
            matches_by_result.get(result_key, []),
            key=lambda item: (item.line, item.snippet, item.queries),
        )
        results.append(
            EvidenceResult(
                source_id=str(meta["source_id"]),
                relative_path=str(meta["relative_path"]),
                kind=str(meta["kind"]),
                score=int(meta["score"]),
                matches=tuple(matches),
            )
        )

    results.sort(key=_evidence_sort_key)
    return DossierEvidence(
        topic=topic,
        queries=queries,
        generated_at=utc_now_iso(),
        searched_file_count=searched_file_count,
        skipped_by_reason=dict(skipped),
        results=tuple(results),
        suggested_queries=tuple(query for query in queries[1:6]),
    )


def generate_dossier_queries(topic: str) -> tuple[str, ...]:
    """Generate conservative deterministic lexical queries for a dossier topic."""

    normalized = " ".join(topic.strip().split())
    if not normalized:
        raise ValueError("dossier topic must not be empty")

    tokens = _topic_tokens(normalized)
    meaningful = tuple(token for token in tokens if token not in STOP_WORDS and len(token) > 1)

    queries: list[str] = [normalized]
    for size in (3, 2):
        for index in range(0, max(0, len(meaningful) - size + 1)):
            _append_query(queries, " ".join(meaningful[index : index + size]))
    for token in meaningful:
        _append_query(queries, token)
    for variant in _topic_variants(meaningful):
        _append_query(queries, variant)

    return tuple(queries[:MAX_DOSSIER_QUERIES])


def publish_dossier(
    scope: HkiScope,
    dossier_path: str | Path,
    *,
    vault_root: str | Path | None = None,
) -> Path:
    """Copy an existing workspace-local dossier into the HKI vault area."""

    source = _resolve_workspace_file(scope, dossier_path)
    if not source.is_file():
        raise ValueError(f"dossier does not exist: {dossier_path}")
    if source.suffix.lower() != ".md":
        raise ValueError(f"dossier must be a Markdown file: {dossier_path}")

    destination = publication_path_for_slug(scope, source.stem, vault_root=vault_root)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    return destination


def list_dossiers(scope: HkiScope) -> tuple[DossierListing, ...]:
    """List workspace-local dossier Markdown files."""

    root = scope.root.resolve()
    dossier_dir = scope.output_dir / DOSSIER_RELATIVE_DIR
    if not dossier_dir.exists():
        return ()

    listings: list[DossierListing] = []
    for path in sorted(dossier_dir.glob("*.md")):
        try:
            stat = path.stat()
            title = _title_for_dossier(path)
        except OSError:
            continue
        listings.append(
            DossierListing(
                path=path,
                relative_path=as_workspace_relative(path, root),
                title=title,
                modified_time=int(stat.st_mtime),
            )
        )
    return tuple(listings)


def build_dossier_markdown(
    search: SearchRun,
    evidence: DossierEvidence,
    scope: HkiScope,
    *,
    dossier_path: Path,
    search_report_path: Path,
    publication_path: Path | None = None,
) -> str:
    """Return first-pass Markdown dossier content for a search run."""

    root = scope.root.resolve()
    grouped = _group_results_by_role(evidence.results)
    publication_ref = (
        as_workspace_relative(publication_path, root) if publication_path is not None else "Not published"
    )
    lines = [
        f"# HKI Dossier: {_markdown_inline(evidence.topic)}",
        "",
        f"Generated: {evidence.generated_at}",
        f"Original topic: `{_markdown_inline(evidence.topic)}`",
        f"Workspace root: `{root}`",
        f"Manifest: `{as_workspace_relative(scope.manifest_path, root)}`",
        f"Search report: `{as_workspace_relative(search_report_path, root)}`",
        f"Dossier path: `{as_workspace_relative(dossier_path, root)}`",
        f"Publication path: `{publication_ref}`",
        "",
        "## Summary",
        "",
        _summary_for_evidence(evidence),
        "",
        "## Generated Search Queries",
        "",
    ]
    for query in evidence.queries:
        lines.append(f"- `{_markdown_inline(query)}`")
    lines.extend(
        [
            "",
            "## Aggregate Search Counts",
            "",
            f"- Query runs: {len(evidence.queries)}",
            f"- Result files after dedupe: {evidence.result_count}",
            f"- Files searched across query runs: {evidence.searched_file_count}",
            f"- Files skipped across query runs: {evidence.skipped_file_count}",
            "",
        ]
    )
    if evidence.skipped_by_reason:
        for reason, count in sorted(evidence.skipped_by_reason.items()):
            lines.append(f"- `{reason}`: {count}")
        lines.append("")

    lines.extend(
        [
            "## Relevant Source Hits",
            "",
        ]
    )

    if not evidence.results:
        lines.extend(["No evidence found.", ""])
        if evidence.suggested_queries:
            lines.extend(["Suggested narrower searches:", ""])
            for query in evidence.suggested_queries:
                lines.append(f"- `{_markdown_inline(query)}`")
            lines.append("")

    for role, heading in (
        ("implementation/source", "Likely Implementation / Source"),
        ("docs/notes", "Docs / Notes"),
        ("tests/examples", "Tests / Examples"),
        ("incidental/other", "Incidental / Other"),
    ):
        lines.extend([f"### {heading}", ""])
        role_results = grouped[role]
        if not role_results:
            lines.extend(["- None", ""])
            continue
        for result in role_results:
            lines.extend(
                [
                    f"- `{result.relative_path}`",
                    f"  - Source ID: `{result.source_id}`",
                    f"  - Kind: `{result.kind}`",
                    f"  - Score: {result.score}",
                ]
            )
            for match in result.matches:
                labels = ", ".join(f"`{_markdown_inline(query)}`" for query in match.queries)
                lines.append(f"  - L{match.line}: {match.snippet} (queries: {labels})")
            lines.append("")

    lines.extend(
        [
            "## Initial Reconstruction",
            "",
            _initial_reconstruction(evidence),
            "",
            "## Diagnostic Checklist / Next Questions",
            "",
            "- Read the highest-scoring implementation and documentation hits before making operational claims.",
            "- Confirm whether the dossier topic names map to the current real-world project, hostnames, routers, VPNs, and access paths.",
            "- Try the suggested narrower searches if this dossier has no evidence or weak evidence.",
            "- Check for missing runtime evidence such as logs, device status, network reachability, credentials, or GitHub issues once those sources are available.",
            "- Ask a focused follow-up question if the lexical hits identify multiple plausible systems or stale project names.",
            "",
            "## Limitations",
            "",
            "This is a first-pass lexical/source-based reconstruction. It is not complete semantic knowledge, does not call an LLM during generation, does not inspect GitHub evidence yet, and may miss relevant material that uses different words from the generated queries.",
            "",
        ]
    )
    return "\n".join(lines)


def _search_run_from_evidence(evidence: DossierEvidence, scope: HkiScope) -> SearchRun:
    results = tuple(
        SearchResult(
            source_id=result.source_id,
            relative_path=result.relative_path,
            kind=result.kind,
            score=result.score,
            matches=tuple(
                SearchMatch(
                    line=match.line,
                    snippet=match.snippet,
                    matched_terms=match.queries,
                    score=match.score,
                )
                for match in result.matches
            ),
        )
        for result in evidence.results
    )
    return SearchRun(
        root=str(scope.root.resolve()),
        query=evidence.topic,
        manifest_path=as_workspace_relative(scope.manifest_path, scope.root.resolve()),
        generated_at=evidence.generated_at,
        searched_file_count=evidence.searched_file_count,
        skipped_by_reason=evidence.skipped_by_reason,
        results=results,
        limits=SearchLimits(max_total_results=len(results)),
    )


def dossier_path_for_slug(scope: HkiScope, slug: str) -> Path:
    return scope.output_dir / DOSSIER_RELATIVE_DIR / f"{slug}.md"


def publication_path_for_slug(
    scope: HkiScope,
    slug: str,
    *,
    vault_root: str | Path | None = None,
) -> Path:
    if vault_root is None:
        return scope.output_dir / VAULT_RELATIVE_DIR / f"{slug}.md"
    return Path(vault_root).expanduser().resolve() / "dossiers" / f"{slug}.md"


def _load_or_create_manifest(scope: HkiScope):
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


def _resolve_workspace_file(scope: HkiScope, path: str | Path) -> Path:
    raw = Path(path).expanduser()
    candidate = raw if raw.is_absolute() else scope.root / raw
    try:
        resolved = candidate.resolve(strict=True)
    except FileNotFoundError as exc:
        raise ValueError(f"dossier does not exist: {path}") from exc
    try:
        resolved.relative_to(scope.root.resolve())
    except ValueError as exc:
        raise ValueError(f"dossier path is outside workspace root: {path}") from exc
    return resolved


def _group_results_by_role(results: tuple[EvidenceResult, ...]) -> dict[str, list[EvidenceResult]]:
    grouped: dict[str, list[EvidenceResult]] = {
        "implementation/source": [],
        "docs/notes": [],
        "tests/examples": [],
        "incidental/other": [],
    }
    for result in results:
        grouped[_role_for_result(result)].append(result)
    return grouped


def _role_for_result(result: EvidenceResult) -> str:
    rel = result.relative_path.lower()
    parts = Path(rel).parts
    suffix = Path(rel).suffix
    name = Path(rel).name
    if (
        "test" in parts
        or "tests" in parts
        or "example" in parts
        or "examples" in parts
        or name.startswith("test_")
        or "_test." in name
    ):
        return "tests/examples"
    if suffix in {".md", ".mdx", ".rst", ".txt"} or any(
        part in {"doc", "docs", "notes", "note"} for part in parts
    ):
        return "docs/notes"
    if result.kind in {
        "python",
        "javascript",
        "typescript",
        "shell",
        "html",
        "css",
        "go",
        "rust",
        "sql",
        "dockerfile",
        "makefile",
        "config",
        "yaml",
        "toml",
        "json",
    }:
        return "implementation/source"
    return "incidental/other"


def _summary_for_evidence(evidence: DossierEvidence) -> str:
    if not evidence.results:
        return (
            "No evidence found for this topic after deterministic multi-query lexical gathering. "
            "The dossier records the attempted queries and suggested narrower searches."
        )
    top_paths = ", ".join(f"`{result.relative_path}`" for result in evidence.results[:3])
    return (
        f"Found {evidence.result_count} deduplicated source file(s) for "
        f"`{_markdown_inline(evidence.topic)}` from {len(evidence.queries)} generated query run(s). "
        f"Files searched across runs: {evidence.searched_file_count}. Highest-scoring paths: {top_paths}."
    )


def _initial_reconstruction(evidence: DossierEvidence) -> str:
    if not evidence.results:
        if evidence.suggested_queries:
            suggested = ", ".join(f"`{query}`" for query in evidence.suggested_queries)
            return f"No reconstruction is possible from the current lexical hits. Try narrower searches such as {suggested}."
        return "No reconstruction is possible from the current lexical hits. Try alternate project names, file names, hostnames, or access-path terms."
    first = evidence.results[0]
    return (
        f"The strongest lexical evidence currently points to `{first.relative_path}` "
        f"(`{first.source_id}`), with supporting snippets from the grouped source hits above. "
        "Use this as bounded context for a human-readable operational reconstruction."
    )


def _evidence_sort_key(result: EvidenceResult) -> tuple[int, int, str, str]:
    role_order = {
        "implementation/source": 0,
        "docs/notes": 1,
        "tests/examples": 2,
        "incidental/other": 3,
    }
    return (role_order[_role_for_result(result)], -result.score, result.relative_path, result.source_id)


def _topic_tokens(topic: str) -> tuple[str, ...]:
    return tuple(
        token
        for token in re.findall(r"[a-z0-9][a-z0-9_.:/-]*", topic.lower())
        if token
    )


def _append_query(queries: list[str], query: str) -> None:
    normalized = " ".join(query.strip().split())
    if not normalized:
        return
    existing = {item.lower() for item in queries}
    if normalized.lower() not in existing:
        queries.append(normalized)


def _topic_variants(meaningful: tuple[str, ...]) -> tuple[str, ...]:
    terms = set(meaningful)
    variants: list[str] = []
    if "skill" in terms or "skills" in terms:
        variants.extend(["SKILL.md", "skill_commands", "skills"])
    if "discovery" in terms or "discover" in terms:
        variants.extend(["discover", "discovery"])
    if "configuration" in terms or "config" in terms:
        variants.extend(["config", "configuration"])
    if "command" in terms or "commands" in terms:
        variants.extend(["commands", "slash command"])
    return tuple(variants)


def _title_for_dossier(path: Path) -> str:
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            stripped = line.strip()
            if stripped.startswith("# "):
                return stripped[2:].strip()
    return path.stem


def _markdown_inline(value: str) -> str:
    return value.replace("`", "'")
