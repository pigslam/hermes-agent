"""``hermes hki`` CLI for project-scoped knowledge artifacts."""

from __future__ import annotations

import argparse
import sys

from hki.dossier import build_dossier, list_dossiers, publish_dossier
from hki.inventory import build_inventory, load_inventory, write_inventory
from hki.manifest import build_manifest, load_manifest, write_manifest
from hki.paths import resolve_scope
from hki.report import write_sources_report
from hki.search import search_manifest, write_latest_search, write_search_report
from hki.timeline import build_timeline
from hki.triage import triage_inbox


def build_parser(
    parent_subparsers: argparse._SubParsersAction,
) -> argparse.ArgumentParser:
    """Attach the ``hki`` subcommand tree. Returns the top parser."""

    parser = parent_subparsers.add_parser(
        "hki",
        help="Manage project-scoped Human Knowledge Infrastructure artifacts",
        description=(
            "Create project-scoped HKI workspace artifacts under .hermes/hki/. "
            "HKI can inventory sources, build a stable manifest, write source "
            "reports, and run bounded lexical source search."
        ),
    )
    sub = parser.add_subparsers(dest="hki_action")

    inventory = sub.add_parser("inventory", help="Scan the workspace and write inventory.json")
    _add_cwd_arg(inventory)

    manifest = sub.add_parser("manifest", help="Build manifest.json from inventory.json")
    _add_cwd_arg(manifest)

    search = sub.add_parser("search", help="Search text sources from manifest.json")
    _add_cwd_arg(search)
    search.add_argument(
        "query",
        nargs="+",
        help="Lexical query to search for",
    )

    dossier = sub.add_parser("dossier", help="Generate or list first-pass HKI dossiers")
    _add_cwd_arg(dossier)
    dossier.add_argument(
        "--publish",
        action="store_true",
        help="Copy the generated dossier to the HKI vault area",
    )
    dossier.add_argument(
        "--vault-root",
        metavar="PATH",
        help="Explicit vault root for publication (writes to PATH/dossiers/)",
    )
    dossier.add_argument(
        "topic",
        nargs="*",
        help='Dossier topic/query, or "list" to list existing dossiers',
    )

    publish = sub.add_parser("publish", help="Copy a workspace dossier to the HKI vault area")
    _add_cwd_arg(publish)
    publish.add_argument(
        "--vault-root",
        metavar="PATH",
        help="Explicit vault root for publication (writes to PATH/dossiers/)",
    )
    publish.add_argument("dossier_path", help="Workspace-relative or absolute dossier Markdown path")

    inbox = sub.add_parser("inbox", help="Triage HKI inbox/workbench material")
    inbox_sub = inbox.add_subparsers(dest="hki_inbox_action")
    inbox_triage = inbox_sub.add_parser("triage", help="Write reports/intake-triage.md")
    _add_cwd_arg(inbox_triage)

    triage = sub.add_parser("triage", help="Write an HKI inbox intake triage report")
    _add_cwd_arg(triage)

    timeline = sub.add_parser("timeline", help="Build a topic evidence timeline")
    _add_cwd_arg(timeline)
    timeline.add_argument(
        "topic",
        nargs="+",
        help="Topic/query to assemble a timeline for",
    )

    report = sub.add_parser("report", help="Write HKI reports")
    report_sub = report.add_subparsers(dest="hki_report_action")
    sources = report_sub.add_parser("sources", help="Write reports/sources.md")
    _add_cwd_arg(sources)

    parser.set_defaults(_hki_parser=parser)
    report.set_defaults(_hki_report_parser=report)
    return parser


def hki_command(args: argparse.Namespace) -> int:
    """Entry point from ``hermes hki ...`` argparse dispatch."""

    action = getattr(args, "hki_action", None)
    if not action:
        parser = getattr(args, "_hki_parser", None)
        if parser is not None:
            parser.print_help()
        else:
            print("usage: reuben hki <action> [options]", file=sys.stderr)
        return 0

    try:
        if action == "inventory":
            return _cmd_inventory(args)
        if action == "manifest":
            return _cmd_manifest(args)
        if action == "search":
            return _cmd_search(args)
        if action == "dossier":
            return _cmd_dossier(args)
        if action == "publish":
            return _cmd_publish(args)
        if action == "triage":
            return _cmd_triage(args)
        if action == "timeline":
            return _cmd_timeline(args)
        if action == "inbox":
            inbox_action = getattr(args, "hki_inbox_action", None)
            if inbox_action == "triage":
                return _cmd_triage(args)
            print("usage: reuben hki inbox triage [options]", file=sys.stderr)
            return 1
        if action == "report":
            report_action = getattr(args, "hki_report_action", None)
            if report_action == "sources":
                return _cmd_report_sources(args)
            parser = getattr(args, "_hki_report_parser", None)
            if parser is not None:
                parser.print_help()
                return 0
            print("usage: reuben hki report <report> [options]", file=sys.stderr)
            return 1
    except (OSError, ValueError) as exc:
        print(f"hki: {exc}", file=sys.stderr)
        return 2

    print(f"Unknown hki action: {action}", file=sys.stderr)
    return 1


def _add_cwd_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--cwd",
        default=".",
        metavar="PATH",
        help="Workspace root to inspect (default: current directory)",
    )


def _cmd_inventory(args: argparse.Namespace) -> int:
    scope = resolve_scope(args.cwd)
    inventory = build_inventory(scope)
    path = write_inventory(inventory, scope)
    print(f"Wrote HKI inventory: {path}")
    print(f"  workspace: {scope.root}")
    print(f"  files:     {len(inventory.files)}")
    print(f"  skipped:   {inventory.skipped_count}")
    return 0


def _cmd_manifest(args: argparse.Namespace) -> int:
    scope = resolve_scope(args.cwd)
    inventory = _load_or_create_inventory(scope)
    manifest = build_manifest(inventory, inventory_path=scope.inventory_path)
    path = write_manifest(manifest, scope)
    print(f"Wrote HKI manifest: {path}")
    print(f"  workspace: {scope.root}")
    print(f"  sources:   {len(manifest.sources)}")
    return 0


def _cmd_report_sources(args: argparse.Namespace) -> int:
    scope = resolve_scope(args.cwd)
    manifest = _load_or_create_manifest(scope)
    path = write_sources_report(manifest, scope)
    print(f"Wrote HKI source report: {path}")
    print(f"  workspace: {scope.root}")
    print(f"  manifest:  {scope.manifest_path}")
    return 0


def _cmd_search(args: argparse.Namespace) -> int:
    scope = resolve_scope(args.cwd)
    manifest = _load_or_create_manifest(scope)
    query = " ".join(args.query)
    search = search_manifest(manifest, scope, query, manifest_path=scope.manifest_path)
    json_path = write_latest_search(search, scope)
    report_path = write_search_report(search, scope)

    print(f"HKI search: {search.query}")
    print(f"  workspace: {scope.root}")
    print(f"  manifest:  {scope.manifest_path}")
    print(f"  searched:  {search.searched_file_count}")
    print(f"  skipped:   {search.skipped_file_count}")
    print(f"  results:   {search.result_count}")
    print(f"  json:      {json_path}")
    print(f"  report:    {report_path}")
    if search.results:
        print("")
        for index, result in enumerate(search.results[:10], start=1):
            print(f"{index}. {result.relative_path} ({result.source_id}, score {result.score})")
            for match in result.matches[:3]:
                print(f"   L{match.line}: {match.snippet}")
    else:
        print("")
        print("No HKI search results found.")
    return 0


def _cmd_dossier(args: argparse.Namespace) -> int:
    scope = resolve_scope(args.cwd)
    topic_parts = list(getattr(args, "topic", []) or [])
    if topic_parts == ["list"]:
        dossiers = list_dossiers(scope)
        print(f"HKI dossiers: {scope.root}")
        if not dossiers:
            print("  none found")
            return 0
        for dossier in dossiers:
            print(f"- {dossier.relative_path}")
            print(f"  title: {dossier.title}")
        return 0

    topic = " ".join(topic_parts).strip()
    if not topic:
        raise ValueError('dossier topic must not be empty; use "reuben hki dossier list" to list')

    dossier = build_dossier(
        scope,
        topic,
        publish=bool(args.publish or args.vault_root),
        vault_root=args.vault_root,
    )
    print(f"Wrote HKI dossier: {dossier.dossier_path}")
    print(f"  workspace:   {scope.root}")
    print(f"  topic:       {dossier.topic}")
    print(f"  manifest:    {dossier.manifest_path}")
    print(f"  search:      {dossier.search_report_path}")
    print(f"  results:     {dossier.search.result_count}")
    if dossier.publication_path is not None:
        print(f"  published:   {dossier.publication_path}")
    return 0


def _cmd_publish(args: argparse.Namespace) -> int:
    scope = resolve_scope(args.cwd)
    path = publish_dossier(scope, args.dossier_path, vault_root=args.vault_root)
    print(f"Published HKI dossier: {path}")
    print(f"  workspace: {scope.root}")
    return 0


def _cmd_triage(args: argparse.Namespace) -> int:
    scope = resolve_scope(args.cwd)
    triage = triage_inbox(scope)
    print(f"Wrote HKI inbox triage: {triage.report_path}")
    print(f"  workspace:  {scope.root}")
    print(f"  json:       {triage.json_path}")
    print(f"  sources:    {triage.source_count}")
    print(f"  candidates: {len(triage.candidates)}")
    return 0


def _cmd_timeline(args: argparse.Namespace) -> int:
    scope = resolve_scope(args.cwd)
    topic = " ".join(args.topic)
    timeline = build_timeline(scope, topic)
    print(f"Wrote HKI timeline: {timeline.report_path}")
    print(f"  workspace: {scope.root}")
    print(f"  topic:     {timeline.topic}")
    print(f"  json:      {timeline.json_path}")
    print(f"  evidence:  {len(timeline.items)}")
    return 0


def _load_or_create_inventory(scope):
    if scope.inventory_path.exists():
        return load_inventory(scope.inventory_path)
    inventory = build_inventory(scope)
    write_inventory(inventory, scope)
    return inventory


def _load_or_create_manifest(scope):
    if scope.manifest_path.exists():
        return load_manifest(scope.manifest_path)
    inventory = _load_or_create_inventory(scope)
    manifest = build_manifest(inventory, inventory_path=scope.inventory_path)
    write_manifest(manifest, scope)
    return manifest
