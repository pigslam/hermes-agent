"""Bounded lexical source search for HKI manifests."""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hki.inventory import SAMPLE_BYTES, is_excluded_directory_name, utc_now_iso
from hki.manifest import Manifest, SourceRecord
from hki.paths import HkiScope, as_workspace_relative


SCHEMA_VERSION = 1
MAX_SEARCH_FILE_BYTES = 512 * 1024
MAX_SEARCHED_FILES = 2000
MAX_MATCHES_PER_FILE = 5
MAX_TOTAL_RESULTS = 25
SNIPPET_CONTEXT_CHARS = 80
SEARCH_RELATIVE_DIR = Path("search")


@dataclass(frozen=True)
class SearchLimits:
    max_file_size_bytes: int = MAX_SEARCH_FILE_BYTES
    max_searched_files: int = MAX_SEARCHED_FILES
    max_matches_per_file: int = MAX_MATCHES_PER_FILE
    max_total_results: int = MAX_TOTAL_RESULTS

    def to_dict(self) -> dict[str, int]:
        return {
            "max_file_size_bytes": self.max_file_size_bytes,
            "max_searched_files": self.max_searched_files,
            "max_matches_per_file": self.max_matches_per_file,
            "max_total_results": self.max_total_results,
        }


@dataclass(frozen=True)
class SearchMatch:
    line: int
    snippet: str
    matched_terms: tuple[str, ...] = ()
    score: int = 0

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "line": self.line,
            "snippet": self.snippet,
        }
        if self.matched_terms:
            data["matched_terms"] = list(self.matched_terms)
        return data


@dataclass(frozen=True)
class SearchResult:
    source_id: str
    relative_path: str
    kind: str
    score: int
    matches: tuple[SearchMatch, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "relative_path": self.relative_path,
            "kind": self.kind,
            "score": self.score,
            "matches": [match.to_dict() for match in self.matches],
        }


@dataclass(frozen=True)
class SearchRun:
    root: str
    query: str
    manifest_path: str
    generated_at: str
    searched_file_count: int
    skipped_by_reason: dict[str, int]
    results: tuple[SearchResult, ...]
    limits: SearchLimits = field(default_factory=SearchLimits)
    schema_version: int = SCHEMA_VERSION

    @property
    def result_count(self) -> int:
        return len(self.results)

    @property
    def skipped_file_count(self) -> int:
        return sum(self.skipped_by_reason.values())

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "generated_at": self.generated_at,
            "root": self.root,
            "query": self.query,
            "manifest_path": self.manifest_path,
            "result_count": self.result_count,
            "searched_file_count": self.searched_file_count,
            "skipped_file_count": self.skipped_file_count,
            "skipped_by_reason": dict(sorted(self.skipped_by_reason.items())),
            "limits": self.limits.to_dict(),
            "results": [result.to_dict() for result in self.results],
        }


@dataclass(frozen=True)
class ParsedQuery:
    raw: str
    phrase: str
    terms: tuple[str, ...]


def search_manifest(
    manifest: Manifest,
    scope: HkiScope,
    query: str,
    *,
    manifest_path: Path | None = None,
    limits: SearchLimits | None = None,
) -> SearchRun:
    """Search text sources from ``manifest`` with bounded lexical matching."""

    parsed = _parse_query(query)
    active_limits = limits or SearchLimits()
    root = scope.root.resolve()
    skipped: Counter[str] = Counter()
    results: list[SearchResult] = []
    searched_file_count = 0

    for source in sorted(manifest.sources, key=lambda item: item.relative_path):
        source_path, reason = _source_path_for_search(source, root)
        if reason:
            skipped[reason] += 1
            continue
        if not source.is_text:
            skipped["binary_source"] += 1
            continue
        if searched_file_count >= active_limits.max_searched_files:
            skipped["file_limit"] += 1
            continue
        try:
            stat = source_path.stat()
        except OSError:
            skipped["missing_or_unreadable"] += 1
            continue
        if stat.st_size > active_limits.max_file_size_bytes:
            skipped["too_large"] += 1
            continue
        if not _actual_file_looks_text(source_path):
            skipped["binary_source"] += 1
            continue

        searched_file_count += 1
        result = _search_source_file(source, source_path, parsed, active_limits)
        if result is not None:
            results.append(result)

    results.sort(key=lambda item: (-item.score, item.relative_path, item.source_id))
    results = results[: active_limits.max_total_results]
    manifest_ref = as_workspace_relative(manifest_path or scope.manifest_path, root)
    return SearchRun(
        root=str(root),
        query=parsed.raw,
        manifest_path=manifest_ref,
        generated_at=utc_now_iso(),
        searched_file_count=searched_file_count,
        skipped_by_reason=dict(skipped),
        results=tuple(results),
        limits=active_limits,
    )


def write_latest_search(search: SearchRun, scope: HkiScope) -> Path:
    """Write ``latest-search.json`` for a search run."""

    path = latest_search_path(scope)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(search.to_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def write_search_report(search: SearchRun, scope: HkiScope) -> Path:
    """Write a Markdown report for a search run."""

    scope.reports_dir.mkdir(parents=True, exist_ok=True)
    path = search_report_path(scope, search.query)
    path.write_text(build_search_report(search), encoding="utf-8")
    return path


def latest_search_path(scope: HkiScope) -> Path:
    return scope.output_dir / SEARCH_RELATIVE_DIR / "latest-search.json"


def search_report_path(scope: HkiScope, query: str) -> Path:
    return scope.reports_dir / f"search-{slug_for_query(query)}.md"


def slug_for_query(query: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", _strip_wrapping_quotes(query).lower()).strip("-")
    if not slug:
        return "query"
    return slug[:80].strip("-") or "query"


def build_search_report(search: SearchRun) -> str:
    """Return a Markdown report for a bounded lexical search run."""

    lines = [
        f"# HKI Source Search: {_markdown_inline(search.query)}",
        "",
        f"Generated: {search.generated_at}",
        f"Workspace root: `{search.root}`",
        f"Manifest: `{search.manifest_path}`",
        "",
        "This is bounded lexical source search, not semantic search.",
        "",
        "## Summary",
        "",
        f"- Results: {search.result_count}",
        f"- Files searched: {search.searched_file_count}",
        f"- Files skipped: {search.skipped_file_count}",
        "",
        "## Results",
        "",
    ]
    if not search.results:
        lines.append("- No matches found.")
        lines.append("")
        return "\n".join(lines)

    for index, result in enumerate(search.results, start=1):
        lines.extend(
            [
                f"### {index}. `{result.relative_path}`",
                "",
                f"- Source ID: `{result.source_id}`",
                f"- Kind: `{result.kind}`",
                f"- Score: {result.score}",
                "",
            ]
        )
        for match in result.matches:
            terms = ""
            if match.matched_terms:
                terms = f" (matched: {', '.join(match.matched_terms)})"
            lines.append(f"- L{match.line}: {match.snippet}{terms}")
        lines.append("")
    return "\n".join(lines)


def _search_source_file(
    source: SourceRecord,
    path: Path,
    query: ParsedQuery,
    limits: SearchLimits,
) -> SearchResult | None:
    matches: list[SearchMatch] = []
    score = 0
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line_number, line in enumerate(handle, start=1):
                match = _match_line(line, line_number, query)
                if match is None:
                    continue
                matches.append(match)
                score += match.score
                if len(matches) >= limits.max_matches_per_file:
                    break
    except OSError:
        return None

    if not matches:
        return None
    score += len(matches)
    return SearchResult(
        source_id=source.source_id,
        relative_path=source.relative_path,
        kind=source.kind,
        score=score,
        matches=tuple(matches),
    )


def _match_line(line: str, line_number: int, query: ParsedQuery) -> SearchMatch | None:
    haystack = line.lower()
    phrase = query.phrase.lower()
    phrase_hit = bool(phrase and phrase in haystack)
    matched_terms = tuple(term for term in query.terms if term in haystack)
    all_terms_hit = bool(query.terms) and len(matched_terms) == len(query.terms)
    if not phrase_hit and not all_terms_hit:
        return None

    score = 10 if phrase_hit else 0
    score += len(matched_terms)
    if all_terms_hit:
        score += 3

    needle = phrase if phrase_hit else matched_terms[0]
    return SearchMatch(
        line=line_number,
        snippet=_snippet_for_line(line, needle),
        matched_terms=matched_terms,
        score=score,
    )


def _source_path_for_search(source: SourceRecord, root: Path) -> tuple[Path, str | None]:
    relative = Path(source.relative_path)
    if relative.is_absolute():
        return root, "outside_root"
    if _is_excluded_search_path(relative):
        return root / relative, "excluded_path"
    try:
        path = (root / relative).resolve(strict=True)
    except OSError:
        return root / relative, "missing_or_unreadable"
    try:
        path.relative_to(root)
    except ValueError:
        return path, "outside_root"
    if not path.is_file():
        return path, "not_file"
    return path, None


def _is_excluded_search_path(relative: Path) -> bool:
    parts = relative.parts
    if not parts:
        return True
    if any(is_excluded_directory_name(part) for part in parts[:-1]):
        return True
    if len(parts) >= 2 and parts[0] == ".hermes" and parts[1] == "hki":
        return True
    name = parts[-1]
    return name == ".env" or name.startswith(".env.")


def _actual_file_looks_text(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            sample = handle.read(SAMPLE_BYTES)
    except OSError:
        return False
    if not sample:
        return True
    if b"\x00" in sample:
        return False
    try:
        sample.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _parse_query(query: str) -> ParsedQuery:
    raw = query.strip()
    phrase = _strip_wrapping_quotes(raw)
    if not phrase:
        raise ValueError("search query must not be empty")
    terms = tuple(dict.fromkeys(re.findall(r"[a-z0-9][a-z0-9_.:/-]*", phrase.lower())))
    if not terms:
        terms = (phrase.lower(),)
    return ParsedQuery(raw=phrase, phrase=phrase, terms=terms)


def _strip_wrapping_quotes(query: str) -> str:
    stripped = query.strip()
    if len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in {"'", '"'}:
        return stripped[1:-1].strip()
    return stripped


def _snippet_for_line(line: str, needle: str) -> str:
    compact = " ".join(line.strip().split())
    lower = compact.lower()
    start = lower.find(needle.lower()) if needle else -1
    if start < 0:
        start = 0
    left = max(0, start - SNIPPET_CONTEXT_CHARS)
    right = min(len(compact), start + len(needle) + SNIPPET_CONTEXT_CHARS)
    snippet = compact[left:right]
    if left > 0:
        snippet = "..." + snippet
    if right < len(compact):
        snippet += "..."
    return snippet


def _markdown_inline(value: str) -> str:
    return value.replace("`", "'")
