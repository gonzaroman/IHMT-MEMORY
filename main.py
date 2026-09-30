#!/usr/bin/env python3
"""Command-line interface for IHMT.

    python main.py demo                      # full end-to-end walkthrough
    python main.py ingest examples/*.txt     # ingest files or directories
    python main.py consolidate --force       # close the tree up to the root
    python main.py search "reserveStock"     # walk root -> branch -> leaf
    python main.py ask "Luis"                # search with the clue loop
    python main.py tree                      # outline of the hierarchy
    python main.py conflicts                 # timeline contradictions

Every command accepts ``--path`` to select the directory containing
``ihmt_memory`` (default: the current directory).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

from ihmt import IHMT, DataType
from ihmt.chunkers import BraceScanner
from ihmt.models import IHMTError
from ihmt.semantic_navigator import ClueRequest, SearchResponse

EXAMPLES_DIR = Path(__file__).resolve().parent / "examples"
RULE = "─" * 78


# --------------------------------------------------------------------- output
def heading(text: str) -> None:
    """Print a section heading."""
    print(f"\n{RULE}\n{text}\n{RULE}")


def emit(payload: Any, as_json: bool) -> bool:
    """Print ``payload`` as JSON when requested. Returns True if it did."""
    if as_json:
        print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
    return as_json


def print_response(response: SearchResponse, *, limit: int = 3, show_content: bool = False) -> None:
    """Render a search response for a terminal reader."""
    print(
        f'query: "{response.query}"  ·  confidence {response.confidence:.2f}  ·  '
        f"{response.node_reads} node reads, {response.leaf_reads} leaf reads, depth {response.depth}"
    )
    if response.clues_used:
        print(f"clues: {' + '.join(response.clues_used)}")
    if not response.results:
        print("no matching memory found.")
        return

    for rank, result in enumerate(response.results[:limit], start=1):
        print(f"\n  {rank}. [{result.domain}] {result.title}")
        print(f"     score {result.score:.3f} · {result.timestamp[:10] or '????'} · {result.leaf_id}")
        print(f"     path  {' → '.join(result.path)}")
        print(f"     file  {result.file}")
        print(f"     {result.excerpt}")
        for notice in result.notices:
            print(f"     ⚠ {notice}")
        if show_content and result.content:
            print("     ---")
            for line in result.content.splitlines()[:12]:
                print(f"     | {line}")


def ask_for_clue(request: ClueRequest) -> Optional[str]:
    """Interactive clue provider backed by ``input``."""
    print()
    print(request.prompt())
    try:
        return input("> ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None


def scripted_clue(answer: str):  # type: ignore[no-untyped-def]
    """Non-interactive clue provider used by the demo."""

    def provider(request: ClueRequest) -> str:
        print()
        print(request.prompt())
        print(f"> {answer}    (scripted answer for the demo)")
        return answer

    return provider


# ------------------------------------------------------------------- commands
def cmd_init(args: argparse.Namespace) -> int:
    """Create the store."""
    overrides: Dict[str, Any] = {}
    if args.branch_factor:
        overrides["branch_factor"] = args.branch_factor
    if args.target_tokens:
        overrides["target_tokens"] = args.target_tokens
    memory = IHMT.initialize(args.path, force=args.force, **overrides)
    print(f"initialized {memory.config.memory_dir}")
    return 0


def cmd_ingest(args: argparse.Namespace) -> int:
    """Ingest files, directories or stdin."""
    memory = IHMT(args.path, auto_consolidate=not args.no_consolidate)
    data_type = DataType.coerce(args.type) if args.type else None
    reports = []

    if args.sources == ["-"]:
        text = sys.stdin.read()
        reports.append(
            memory.ingest_text(text, source="<stdin>", domain=args.domain, data_type=data_type, tags=args.tag)
        )
    else:
        for raw in args.sources:
            path = Path(raw)
            if path.is_dir():
                reports.extend(memory.ingest_directory(path, domain=args.domain, tags=args.tag))
            else:
                reports.append(memory.ingest_file(path, domain=args.domain, data_type=data_type, tags=args.tag))

    if emit([r.to_dict() for r in reports], args.json):
        return 0

    for report in reports:
        flag = f" · {report.oversized} oversized" if report.oversized else ""
        print(
            f"{report.source}\n"
            f"  → {report.data_type.value} / {report.domain} "
            f"(confidence {report.detection.confidence:.2f})\n"
            f"  → {report.chunks} leaves, ~{report.tokens} tokens, {report.facts} facts{flag}"
        )
    if args.no_consolidate:
        print("\n(consolidation skipped: run 'python main.py consolidate --force')")
    else:
        print(f"\ntree: {memory.stats()['nodes']} nodes, depth {memory.stats()['depth']}")
    return 0


def cmd_consolidate(args: argparse.Namespace) -> int:
    """Fire pending Summarization Events."""
    memory = IHMT(args.path)
    report = memory.consolidate(force=args.force)
    if emit(report.to_dict(), args.json):
        return 0
    if not report.events:
        print("nothing to consolidate (tree is up to date)")
    for event in report.events:
        print(
            f"summarization event · layer {event.layer} · {event.domain} · "
            f"{len(event.child_ids)} children ({event.trigger}) → {event.node_id}"
        )
    print(f"depth {report.depth}; pending leaves per domain: {report.pending or '{}'}")
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    """Walk the tree for a query."""
    memory = IHMT(args.path)
    response = memory.search(args.query, top_k=args.top_k)
    if emit(response.to_dict(include_content=args.full), args.json):
        return 0
    print_response(response, limit=args.top_k, show_content=args.full)
    if response.needs_clue:
        print("\nthis query is ambiguous — use 'python main.py ask' to run the clue loop")
    return 0


def cmd_ask(args: argparse.Namespace) -> int:
    """Search with the interactive clue loop."""
    memory = IHMT(args.path)
    provider = scripted_clue(args.clue) if args.clue else ask_for_clue
    response = memory.ask(args.query, provider, top_k=args.top_k)
    if emit(response.to_dict(include_content=args.full), args.json):
        return 0
    heading(f"answer for: {args.query}")
    print_response(response, limit=args.top_k, show_content=args.full)
    if response.needs_clue:
        print("\nstill ambiguous — no confident answer; try another clue.")
    return 0


def cmd_tree(args: argparse.Namespace) -> int:
    """Print an outline of the hierarchy."""
    memory = IHMT(args.path)
    for line in memory.outline(max_depth=args.depth):
        print(line)
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    """Print store counters."""
    memory = IHMT(args.path)
    stats = memory.stats()
    if emit(stats, args.json):
        return 0
    for key, value in stats.items():
        print(f"{key:>16}: {value}")
    return 0


def cmd_facts(args: argparse.Namespace) -> int:
    """Print the active state of the timeline."""
    memory = IHMT(args.path)
    report = memory.resolver.report(subject=args.subject)
    if emit(report, args.json):
        return 0
    print(f"{len(report['active_state'])} active facts of {report['fact_count']} recorded\n")
    for key, fact in report["active_state"].items():
        print(f"  {key:<28} = {fact['value']}   ({fact['timestamp'][:10]})")
    return 0


def cmd_conflicts(args: argparse.Namespace) -> int:
    """Print detected contradictions."""
    memory = IHMT(args.path)
    conflicts = memory.conflicts(subject=args.subject)
    if emit([c.to_dict() for c in conflicts], args.json):
        return 0
    if not conflicts:
        print("no contradictions detected")
        return 0
    print(f"{len(conflicts)} contradiction(s) detected:\n")
    for conflict in conflicts:
        print(f"  ⚠ {conflict.render_notice()}")
        print(
            f"     history: {conflict.old.value} [{conflict.old.timestamp[:10]}, HISTORICAL] → "
            f"{conflict.new.value} [{conflict.new.timestamp[:10]}, ACTIVE]"
        )
    return 0


def cmd_rebuild(args: argparse.Namespace) -> int:
    """Rebuild catalog, timeline and trunk from the files on disk."""
    memory = IHMT(args.path)
    result = memory.rebuild()
    print(f"rebuilt: {result['catalog_entries']} catalog entries, {result['facts']} facts")
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    """End-to-end walkthrough of every capability."""
    workspace = Path(args.path)
    heading("1 · initialize a store")
    memory = IHMT.initialize(workspace, force=True, branch_factor=4, target_tokens=400, max_tokens=900)
    print(f"store      : {memory.config.memory_dir}")
    print(f"branch_factor={memory.config.branch_factor}  target_tokens={memory.config.target_tokens}")
    print("(a small branch factor is used so a modest corpus still builds a multi-layer tree)")

    heading("2 · ingest a Java class, Python module, narrative, journal, clinical history and a recipe")
    sources = [
        EXAMPLES_DIR / "InventoryService.java",
        EXAMPLES_DIR / "pricing_engine.py",
        EXAMPLES_DIR / "narrative_es.txt",
        EXAMPLES_DIR / "journal_personal.txt",
        EXAMPLES_DIR / "clinical_history.txt",
        EXAMPLES_DIR / "recipe_paella.md",
    ]
    reports = []
    for source in sources:
        report = memory.ingest_file(source)
        reports.append(report)
        print(
            f"  {source.name:<26} → {report.data_type.value:<9} {report.domain:<20} "
            f"{report.chunks} leaves  ~{report.tokens} tokens"
        )

    heading("3 · verify no logical block was ever cut")
    java_report = reports[0]
    java_source = EXAMPLES_DIR / "InventoryService.java"
    original = java_source.read_text(encoding="utf-8")
    leaves = [memory.get_leaf(leaf_id) for leaf_id in java_report.leaf_ids]

    rebuilt = "".join(leaf.content for leaf in leaves)
    print(f"byte-exact reconstruction of InventoryService.java : {rebuilt == original}")

    # A boundary at brace depth 0 sits between top-level declarations; depth 1
    # sits between the members of a class. Depth 2+ would mean a method body was
    # cut in half.
    depths = BraceScanner(original).depth_at_line_ends()
    boundaries = [(leaf.span.end_line, depths.get(leaf.span.end_line, 0)) for leaf in leaves[:-1]]
    print(f"deepest brace level at any leaf boundary        : {max((d for _, d in boundaries), default=0)}")
    print(f"no leaf boundary falls inside a method body     : {all(d <= 1 for _, d in boundaries)}")

    print("\nleaf boundaries chosen by the code-aware splitter:")
    for leaf in leaves:
        print(f"  lines {leaf.span.start_line:>3}-{leaf.span.end_line:<3} · {leaf.title.split('·', 1)[-1].strip()}")

    heading("4 · consolidate: propagate summaries layer by layer up to the root")
    report = memory.flush()
    for event in report.events:
        print(
            f"  summarization event · layer {event.layer} · {event.domain:<20} "
            f"{len(event.child_ids)} children ({event.trigger})"
        )
    stats = memory.stats()
    print(f"\n  {stats['leaves']} leaves · {stats['nodes']} branch nodes · depth {stats['depth']}")
    print(f"  nodes per layer: {stats['nodes_per_layer']}")

    heading("5 · the tree")
    for line in memory.outline(max_depth=2, max_children=4):
        print(line)

    heading("6 · traverse the tree to retrieve a memory")
    response = memory.search("reserveStock soft hold concurrency", top_k=2)
    print_response(response, limit=2)
    total_leaves = stats["leaves"]
    print(
        f"\n  opened {response.node_reads} branch files out of {stats['nodes'] + 1} "
        f"to find it among {total_leaves} leaves — the descent is logarithmic, not a scan."
    )

    heading("7 · a query in another domain, in Spanish")
    print_response(memory.search("cómo se hace el socarrat de la paella", top_k=1), limit=1)

    heading("8 · interactive clue loop on an ambiguous query")
    ambiguous = memory.search("Luis", top_k=4)
    print(f'"Luis" alone → confidence {ambiguous.confidence:.2f}, ambiguous={ambiguous.ambiguous}')
    resolved = memory.ask("Luis", scripted_clue("vacaciones en Benidorm"), top_k=3)
    print()
    print_response(resolved, limit=2)

    heading("9 · conflicting information resolved by recency")
    for conflict in memory.conflicts():
        print(f"  ⚠ {conflict.render_notice()}")
        print(
            f"     {conflict.old.value} [{conflict.old.timestamp[:10]}] is kept as HISTORICAL; "
            f"{conflict.new.value} [{conflict.new.timestamp[:10]}] is ACTIVE"
        )
    print("\n  active state:")
    for key, fact in sorted(memory.resolver.active_state().items()):
        print(f"    {key:<28} = {fact.value}  ({fact.timestamp[:10]})")
    print("\n  timeline query — what was true on 2024-12-31?")
    for key, fact in sorted(memory.resolver.state_at("2024-12-31").items()):
        print(f"    {key:<28} = {fact.value}  ({fact.timestamp[:10]})")

    heading("demo complete")
    print(f"explore the store with:  python main.py tree --path {workspace}")
    print(f"                         python main.py search 'migración a Go' --path {workspace}")
    return 0


# --------------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    """Assemble the argument parser."""
    parser = argparse.ArgumentParser(prog="main.py", description="IHMT — Infinite Hierarchical Memory Tree")
    parser.add_argument(
        "--path",
        default=None,
        help="directory containing ihmt_memory/ (default: '.', or ./demo_workspace for 'demo')",
    )
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")

    # The same two flags are accepted after the subcommand as well, which is
    # where people naturally type them. SUPPRESS keeps an unset flag from
    # overwriting the value already parsed by the main parser.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--path", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    subparsers = parser.add_subparsers(dest="command", required=True, parser_class=argparse.ArgumentParser)

    def add(name: str, help_text: str) -> argparse.ArgumentParser:
        return subparsers.add_parser(name, help=help_text, parents=[common])

    p_init = add("init", "create the store")
    p_init.add_argument("--branch-factor", type=int)
    p_init.add_argument("--target-tokens", type=int)
    p_init.add_argument("--force", action="store_true")
    p_init.set_defaults(func=cmd_init)

    p_ingest = add("ingest", "ingest files, directories or '-' for stdin")
    p_ingest.add_argument("sources", nargs="+")
    p_ingest.add_argument("--domain", help="override the detected domain")
    p_ingest.add_argument("--type", choices=[t.value for t in DataType], help="override the detected type")
    p_ingest.add_argument("--tag", action="append", default=[], help="extra tag (repeatable)")
    p_ingest.add_argument("--no-consolidate", action="store_true", help="do not summarize after ingesting")
    p_ingest.set_defaults(func=cmd_ingest)

    p_consolidate = add("consolidate", "run pending summarization events")
    p_consolidate.add_argument("--force", action="store_true", help="also promote partial groups")
    p_consolidate.set_defaults(func=cmd_consolidate)

    p_search = add("search", "walk the tree for a query")
    p_search.add_argument("query")
    p_search.add_argument("--top-k", type=int, default=3)
    p_search.add_argument("--full", action="store_true", help="print leaf content")
    p_search.set_defaults(func=cmd_search)

    p_ask = add("ask", "search with the interactive clue loop")
    p_ask.add_argument("query")
    p_ask.add_argument("--clue", help="answer the clue prompt non-interactively")
    p_ask.add_argument("--top-k", type=int, default=3)
    p_ask.add_argument("--full", action="store_true")
    p_ask.set_defaults(func=cmd_ask)

    p_tree = add("tree", "print an outline of the hierarchy")
    p_tree.add_argument("--depth", type=int, default=2)
    p_tree.set_defaults(func=cmd_tree)

    p_stats = add("stats", "print store counters")
    p_stats.set_defaults(func=cmd_stats)

    p_facts = add("facts", "print the active state of the timeline")
    p_facts.add_argument("--subject")
    p_facts.set_defaults(func=cmd_facts)

    p_conflicts = add("conflicts", "print detected contradictions")
    p_conflicts.add_argument("--subject")
    p_conflicts.set_defaults(func=cmd_conflicts)

    p_rebuild = add("rebuild", "rebuild catalog, timeline and trunk from disk")
    p_rebuild.set_defaults(func=cmd_rebuild)

    p_demo = add("demo", "end-to-end walkthrough")
    p_demo.set_defaults(func=cmd_demo)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Entry point. Returns a process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    # The demo writes to its own workspace so it never disturbs a real store.
    if args.path is None:
        args.path = "./demo_workspace" if args.command == "demo" else "."
    try:
        return int(args.func(args))
    except IHMTError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:  # pragma: no cover
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
