---
name: hki
description: "Use native HKI commands to inventory workspace sources, build a manifest, write source reports, and run bounded lexical source search."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [HKI, source inventory, workspace, manifest, report, search]
    category: productivity
    requires_toolsets: [terminal, files]
---

# HKI Source Inventory And Search

HKI — Human Knowledge Infrastructure — is a native project/workspace knowledge subsystem. The current slice inventories workspace sources, creates a stable source manifest, writes basic source reports, and runs bounded lexical search over manifest-listed text sources.

This is not semantic/vector search, entity indexing, dossier generation, vault/wiki publication, cron refresh, or frontend/UI work yet.

## When To Use

Use this skill when the user asks to:

- assess a repo, project, source corpus, or workspace
- build or update a source inventory
- prepare evidence for later HKI work
- understand what files or sources exist in a workspace
- create a basic source report
- find likely relevant source files or line snippets with bounded lexical search

## When Not To Use

Do not use this skill for:

- ordinary code edits or simple file reads
- semantic search across conversations
- entity extraction
- dossier generation
- vault/wiki publication
- background cron refresh
- frontend/UI work

## Commands

Run these from any shell, choosing the intended workspace as `--cwd`:

```bash
hermes hki inventory --cwd <path>
hermes hki manifest --cwd <path>
hermes hki report sources --cwd <path>
hermes hki search --cwd <path> "<query>"
```

Expected outputs:

```text
<cwd>/.hermes/hki/inventory.json
<cwd>/.hermes/hki/manifest.json
<cwd>/.hermes/hki/reports/sources.md
<cwd>/.hermes/hki/search/latest-search.json
<cwd>/.hermes/hki/reports/search-<query-slug>.md
```

## Recommended Workflow

1. Resolve the intended workspace/cwd with the user or from the active task context.
2. Run `hermes hki inventory --cwd <path>`.
3. Run `hermes hki manifest --cwd <path>`.
4. Run `hermes hki report sources --cwd <path>`.
5. For a bounded source lookup, run `hermes hki search --cwd <path> "<query>"`.
6. Read the generated source or search report first; read the manifest only as needed.
7. Cite generated paths, `source_id` values, line numbers, snippets, and report sections when summarizing.
8. Avoid loading large manifests, inventories, or search JSON wholesale into prompt context unless the user explicitly needs that detail.

## Safety And Limitations

- Scope resolves from `--cwd` to the containing Git workspace root when available, otherwise it falls back to the resolved cwd.
- Generated files live under `.hermes/hki/`.
- Source IDs are deterministic but path-based, so renames change IDs.
- Reports and searches may be stale if files changed after manifest generation.
- Search is bounded lexical matching over manifest-listed text files, not semantic search.
- Secret exclusion is currently minimal: `.env` and `.env.*` are excluded, but this is not a full secret scanner.
- Treat HKI output as a source inventory/report, not as a semantic dossier.
