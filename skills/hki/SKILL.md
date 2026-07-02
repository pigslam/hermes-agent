---
name: hki
description: "Use native HKI commands to inventory workspace sources, triage inboxes, build a manifest, write source reports, run bounded lexical source search, create first-pass dossiers, and assemble topic timelines."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [HKI, source inventory, workspace, inbox, triage, manifest, report, search, dossier, timeline]
    category: productivity
    requires_toolsets: [terminal, files]
---

# HKI Source Inventory, Inbox Triage, Search, Dossiers, And Timelines

HKI — Human Knowledge Infrastructure — is a native project/workspace knowledge subsystem. The current slice inventories workspace sources, creates a stable source manifest, writes basic source reports, triages inbox/workbench folders, runs bounded lexical search over manifest-listed text sources, creates first-pass Markdown dossiers from deterministic multi-query lexical gathering, and assembles topic timelines for evolved or conflicting evidence.

This is not semantic/vector search, entity indexing, full vault/wiki publication, cron refresh, GitHub ingestion, or frontend/UI work yet.

## When To Use

Use this skill when the user asks to:

- assess a repo, project, source corpus, or workspace
- build or update a source inventory
- prepare evidence for later HKI work
- understand what files or sources exist in a workspace
- create a basic source report
- triage an inbox/workbench folder before deciding which dossiers to build
- find likely relevant source files or line snippets with bounded lexical search
- answer operational reconstruction questions by building or finding a first-pass dossier
- build a timeline/current-state evidence report for topics that may have changed over time
- preserve a first-pass reconstruction in the workspace HKI reports area or local/configured HKI vault
- turn a natural-language source or operational question into a basic evidence dossier without asking the user to compose narrow search queries

## When Not To Use

Do not use this skill for:

- ordinary code edits or simple file reads
- semantic search across conversations
- entity extraction
- full vault/wiki publication
- background cron refresh
- GitHub repo, issue, PR, comment, or commit ingestion
- frontend/UI work

## Commands

Run these from any shell, choosing the intended workspace as `--cwd`:

```bash
hermes hki inventory --cwd <path>
hermes hki manifest --cwd <path>
hermes hki report sources --cwd <path>
hermes hki inbox triage --cwd <path>
hermes hki triage --cwd <path>
hermes hki search --cwd <path> "<query>"
hermes hki timeline --cwd <path> "<topic or query>"
hermes hki dossier --cwd <path> "<topic or query>"
hermes hki dossier --cwd <path> "<topic or query>" --publish
hermes hki dossier --cwd <path> list
hermes hki publish --cwd <path> <dossier-path>
```

Expected outputs:

```text
<cwd>/.hermes/hki/inventory.json
<cwd>/.hermes/hki/manifest.json
<cwd>/.hermes/hki/reports/sources.md
<cwd>/.hermes/hki/reports/intake-triage.md
<cwd>/.hermes/hki/triage/latest-triage.json
<cwd>/.hermes/hki/search/latest-search.json
<cwd>/.hermes/hki/reports/search-<query-slug>.md
<cwd>/.hermes/hki/timeline/latest-timeline.json
<cwd>/.hermes/hki/reports/timeline-<query-slug>.md
<cwd>/.hermes/hki/reports/dossiers/<query-slug>.md
<cwd>/.hermes/hki/vault/dossiers/<query-slug>.md
```

Use `--vault-root <path>` with `dossier --publish` or `publish` only when the user explicitly wants a configured external vault root. Without `--vault-root`, publication is dev-safe and local to the workspace under `.hermes/hki/vault/dossiers/`.

`hermes hki dossier` preserves the original topic, expands it into conservative lexical searches, runs bounded HKI search for each generated query, deduplicates source hits, and records which query labels produced each snippet. Use `hermes hki search` directly only when you need a specific one-off lexical probe.

## Recommended Workflow

1. Resolve the intended workspace/cwd with the user or from the active task context.
2. Run `hermes hki inventory --cwd <path>`.
3. Run `hermes hki manifest --cwd <path>`.
4. Run `hermes hki report sources --cwd <path>`.
5. For raw inbox/workbench intake, run `hermes hki inbox triage --cwd <path>` before building dossiers.
6. For a bounded source lookup, run `hermes hki search --cwd <path> "<query>"`.
7. For evolved operational topics, run `hermes hki timeline --cwd <path> "<topic>"` before treating a dossier as current.
8. Read the generated source, triage, timeline, or search report first; read the manifest only as needed.
9. Cite generated paths, `source_id` values, line numbers, snippets, timestamps, and report sections when summarizing.
10. Avoid loading large manifests, inventories, triage JSON, timeline JSON, or search JSON wholesale into prompt context unless the user explicitly needs that detail.

## Inbox Triage Workflow

Inbox triage is the first step after the user places raw/source material in an inbox or workbench folder. Run `hermes hki inbox triage --cwd <path>` and read `.hermes/hki/reports/intake-triage.md`.

The triage report proposes broad projects/topics, representative evidence, suggested dossier slugs, suggested actions, and an organization. It should ask the human to approve, rename, merge, split, ignore, or mark topics sensitive/private before any bulk dossier generation.

The CLI is internal plumbing: the user should be able to ask naturally, such as "Use HKI to triage this inbox folder and tell me what topics appear to be represented." Summarize the triage in natural language and ask which dossiers to build next.

## Dossier Workflow

For operational reconstruction questions, first build or find an HKI dossier. Examples include questions like "how is this access path supposed to work?" or "help me diagnose why this project setup is unreachable."

1. Resolve the intended workspace/cwd from task context.
2. Check for existing dossiers with `hermes hki dossier --cwd <path> list`.
3. If no suitable dossier exists, run `hermes hki dossier --cwd <path> "<topic>"`. The topic can be natural language; HKI will generate narrower lexical queries automatically.
4. If the user asks to store/publish it durably, add `--publish`, or use `--vault-root <path>` only for an explicit configured vault.
5. Read the generated dossier under `.hermes/hki/reports/dossiers/` and use it as bounded context.
6. In final answers, summarize the dossier in natural language. Mention command syntax only when it helps the user reproduce or inspect the artifact.

The first reality-test topic is the Visalia house camera/travelrouter project. Do not rewrite this as a condo camera network: the condo does not have a camera network. Use search terms around Visalia, camera, travelrouter, network, VPN, router, and camera access as appropriate.

Future HKI evidence should be compatible with read-only GitHub sources such as repos, issues, PRs, comments, and commit history, but GitHub ingestion is not implemented in this slice.

## Timeline Workflow

Use timelines when a topic has evolved or may contain stale/conflicting evidence, especially for operational troubleshooting. Run `hermes hki timeline --cwd <path> "<topic>"` and read `.hermes/hki/reports/timeline-<topic-slug>.md`.

Timeline reports preserve source paths, source IDs, snippets, query labels, and best-effort timestamps. When `<cwd>/hki/records.jsonl` exists, timeline uses those normalized records first because they often preserve conversation/message order, `created_at`, `updated_at`, record type, speaker, title, conversation ID, message ID, and conversation/message indexes. Event order is often more useful than exact dates for operational reconstruction: use the timeline to distinguish older evidence, setup/configuration phases, later corrections, and latest-located evidence.

For operational troubleshooting, ask HKI for timeline/current-state evidence before treating a dossier as current truth. Do not claim a fact is current solely because it appears later in the timeline. Phrase conclusions as "latest located evidence suggests..." unless live verification was performed. Call out stale, conflicting, and security-sensitive evidence explicitly, and use timeline reports as bounded context instead of loading raw huge JSON into prompt context.

## Sensitive Evidence Handling

HKI generated reports and JSON artifacts apply first-pass redaction for obvious secrets such as URL-embedded passwords, WireGuard private keys, API keys, tokens, password assignments, and common password labels. Redaction preserves diagnostic shape while replacing sensitive values with markers such as `[REDACTED_PASSWORD]`, `[REDACTED_PRIVATE_KEY]`, `[REDACTED_TOKEN]`, and `[REDACTED_CREDENTIAL]`. Public WireGuard keys are not redacted unless they are explicitly private-key-labeled.

When summarizing HKI output, flag security-sensitive material without repeating secret values. Do not quote passwords, private keys, tokens, or embedded URL credentials in ordinary summaries. If the user explicitly asks to inspect raw secrets, require a deliberate action and prefer pointing to source file paths, source IDs, and line numbers rather than printing the values.

## Safety And Limitations

- Scope resolves from `--cwd` to the containing Git workspace root when available, otherwise it falls back to the resolved cwd.
- Generated files live under `.hermes/hki/`.
- First-pass dossiers live under `.hermes/hki/reports/dossiers/`.
- Intake triage reports live at `.hermes/hki/reports/intake-triage.md`; the latest triage JSON lives at `.hermes/hki/triage/latest-triage.json`.
- Timeline reports live at `.hermes/hki/reports/timeline-<slug>.md`; the latest timeline JSON lives at `.hermes/hki/timeline/latest-timeline.json`.
- Local published dossiers live under `.hermes/hki/vault/dossiers/` unless an explicit `--vault-root` is provided.
- Source IDs are deterministic but path-based, so renames change IDs.
- Reports and searches may be stale if files changed after manifest generation.
- Search is bounded lexical matching over manifest-listed text files, not semantic search.
- Dossiers are template/extractive, first-pass, and source-search-based. They expand the topic into deterministic lexical queries, but they do not call an LLM during generation.
- Inbox triage is deterministic, seed-probe-based, and intentionally human-curated. It suggests topics but does not automatically build dossiers.
- Timelines are deterministic and best-effort. They use normalized HKI records when available, inline/export-like timestamps when easy, file mtimes as fallback, and do not prove current state without live verification.
- Redaction is a thin pattern-based safety layer, not a complete secret scanner. Treat any redaction summary as a warning that raw source material may require careful handling.
- Secret exclusion is currently minimal: `.env` and `.env.*` are excluded, but this is not a full secret scanner.
- Treat HKI output as bounded evidence for Reuben's natural-language synthesis, not as complete semantic knowledge.
