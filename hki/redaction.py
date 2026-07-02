"""First-pass redaction helpers for generated HKI artifacts."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import Any


URL_CREDENTIAL_RE = re.compile(
    r"(?P<prefix>\b[a-z][a-z0-9+.-]*://[^/\s:@]+:)(?P<secret>[^@\s/]+)(?P<suffix>@)",
    re.IGNORECASE,
)
WIREGUARD_PRIVATE_KEY_RE = re.compile(
    r"(?im)(?P<prefix>\bPrivateKey\s*=\s*)(?P<secret>[A-Za-z0-9+/=]{20,})"
)
CREDENTIAL_ASSIGNMENT_RE = re.compile(
    r"(?im)(?P<prefix>\b(?:[A-Z0-9_ -]*password|passwd|passcode|api[_ -]?key|access[_ -]?token|auth[_ -]?token|token|secret)\b\s*[:=]\s*)(?P<secret>\"[^\"]+\"|'[^']+'|[^,\s\])}>]+)"
)
BEARER_TOKEN_RE = re.compile(r"(?i)(?P<prefix>\bBearer\s+)(?P<secret>[A-Za-z0-9._~+/-]{20,})")
TOKEN_SHAPE_RE = re.compile(
    r"\b(?P<secret>(?:sk|rk|ghp|github_pat|xoxb|xoxp|xoxa|xoxr)-[A-Za-z0-9._-]{16,})\b"
)


@dataclass(frozen=True)
class RedactionSummary:
    counts: dict[str, int]

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    @property
    def has_findings(self) -> bool:
        return self.total > 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "redactions_applied": self.total,
            "categories": dict(sorted(self.counts.items())),
        }


@dataclass(frozen=True)
class RedactionResult:
    text: str
    summary: RedactionSummary


def redact_text(text: str) -> RedactionResult:
    """Redact obvious secrets from generated HKI text."""

    counts: Counter[str] = Counter()

    def replace_url(match: re.Match[str]) -> str:
        counts["url_password"] += 1
        return f"{match.group('prefix')}[REDACTED_PASSWORD]{match.group('suffix')}"

    def replace_private_key(match: re.Match[str]) -> str:
        counts["wireguard_private_key"] += 1
        return f"{match.group('prefix')}[REDACTED_PRIVATE_KEY]"

    def replace_assignment(match: re.Match[str]) -> str:
        key = match.group("prefix").lower()
        if "password" in key or "passwd" in key or "passcode" in key:
            category = "password"
            replacement = "[REDACTED_PASSWORD]"
        elif "token" in key or "api" in key:
            category = "token"
            replacement = "[REDACTED_TOKEN]"
        else:
            category = "credential"
            replacement = "[REDACTED_CREDENTIAL]"
        counts[category] += 1
        return f"{match.group('prefix')}{replacement}"

    def replace_bearer(match: re.Match[str]) -> str:
        counts["token"] += 1
        return f"{match.group('prefix')}[REDACTED_TOKEN]"

    def replace_token_shape(match: re.Match[str]) -> str:
        counts["token"] += 1
        return "[REDACTED_TOKEN]"

    redacted = URL_CREDENTIAL_RE.sub(replace_url, text)
    redacted = WIREGUARD_PRIVATE_KEY_RE.sub(replace_private_key, redacted)
    redacted = CREDENTIAL_ASSIGNMENT_RE.sub(replace_assignment, redacted)
    redacted = BEARER_TOKEN_RE.sub(replace_bearer, redacted)
    redacted = TOKEN_SHAPE_RE.sub(replace_token_shape, redacted)
    return RedactionResult(text=redacted, summary=RedactionSummary(dict(counts)))


def redact_markdown_report(content: str) -> RedactionResult:
    """Redact a Markdown report and append a value-free sensitivity summary."""

    result = redact_text(content)
    summary = build_sensitivity_summary(result.summary)
    return RedactionResult(text=f"{result.text.rstrip()}\n\n{summary}\n", summary=result.summary)


def redact_json_value(value: Any) -> tuple[Any, RedactionSummary]:
    """Redact strings inside a JSON-compatible value."""

    counts: Counter[str] = Counter()
    redacted = _redact_json_value(value, counts)
    return redacted, RedactionSummary(dict(counts))


def redact_json_artifact(value: dict[str, Any]) -> dict[str, Any]:
    """Return a redacted JSON artifact with a top-level sensitivity summary."""

    redacted, summary = redact_json_value(value)
    if isinstance(redacted, dict):
        redacted["sensitivity"] = summary.to_dict()
        return redacted
    return {
        "value": redacted,
        "sensitivity": summary.to_dict(),
    }


def build_sensitivity_summary(summary: RedactionSummary) -> str:
    lines = ["## Sensitivity Summary", ""]
    if not summary.has_findings:
        lines.append("- No obvious secrets detected by first-pass HKI redaction.")
        return "\n".join(lines)
    lines.extend(
        [
            "- Obvious sensitive values were redacted from this generated artifact.",
            "- Categories detected:",
        ]
    )
    for category, count in sorted(summary.counts.items()):
        lines.append(f"  - `{category}`: {count}")
    return "\n".join(lines)


def _redact_json_value(value: Any, counts: Counter[str]) -> Any:
    if isinstance(value, str):
        result = redact_text(value)
        counts.update(result.summary.counts)
        return result.text
    if isinstance(value, list):
        return [_redact_json_value(item, counts) for item in value]
    if isinstance(value, tuple):
        return [_redact_json_value(item, counts) for item in value]
    if isinstance(value, dict):
        return {key: _redact_json_value(item, counts) for key, item in value.items()}
    return value
