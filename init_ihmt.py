#!/usr/bin/env python3
"""Initialize an IHMT store.

Creates the directory skeleton, writes an empty trunk and persists the
configuration::

    ihmt_memory/
      root.json          # the trunk
      layer_0/           # raw .txt leaves, one directory per domain
      layers/1..N/       # JSON branch nodes
      state/             # catalog + fact timeline
      ihmt.config.json   # branch factor, token budgets, backend

Running it twice is safe: existing data is left untouched unless ``--force`` is
given, and ``--force`` still never deletes leaves.

Usage:
    python init_ihmt.py
    python init_ihmt.py --path ./workspace --branch-factor 4 --target-tokens 1500
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ihmt import IHMT, IHMTConfig
from ihmt.models import IHMTError


def build_parser() -> argparse.ArgumentParser:
    """CLI definition for the initializer."""
    parser = argparse.ArgumentParser(
        prog="init_ihmt",
        description="Create the directory structure of an IHMT memory store.",
    )
    parser.add_argument("--path", default=".", help="directory that will contain ihmt_memory/ (default: .)")
    parser.add_argument(
        "--branch-factor",
        type=int,
        default=None,
        help="children per branch node; also the log base of retrieval cost (default: 8)",
    )
    parser.add_argument("--target-tokens", type=int, default=None, help="target leaf size in tokens (default: 2000)")
    parser.add_argument("--max-tokens", type=int, default=None, help="hard ceiling before a leaf is flagged oversized")
    parser.add_argument(
        "--backend",
        choices=["heuristic", "anthropic"],
        default=None,
        help="summarization backend (default: heuristic, fully offline)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="reset trunk, catalog and fact timeline (leaf files are preserved)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns a process exit code."""
    args = build_parser().parse_args(argv)

    overrides = {}
    if args.branch_factor is not None:
        overrides["branch_factor"] = args.branch_factor
    if args.target_tokens is not None:
        overrides["target_tokens"] = args.target_tokens
    if args.max_tokens is not None:
        overrides["max_tokens"] = args.max_tokens
    if args.backend is not None:
        overrides["summarizer_backend"] = args.backend

    try:
        memory = IHMT.initialize(args.path, force=args.force, **overrides)
    except (IHMTError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    config: IHMTConfig = memory.config
    stats = memory.stats()
    print(f"IHMT store ready at {config.memory_dir}")
    print(f"  trunk           : {Path(config.root_path).name}")
    print(f"  leaves (layer 0): {config.layer0_dir}")
    print(f"  branches        : {config.layers_dir}/1..N")
    print(f"  state           : {config.state_dir}")
    print(
        f"  config          : branch_factor={config.branch_factor}, "
        f"target_tokens={config.target_tokens}, max_tokens={config.max_tokens}, "
        f"backend={config.summarizer_backend}"
    )
    print(f"  contents        : {stats['leaves']} leaves, {stats['nodes']} nodes, depth {stats['depth']}")
    print("\nNext: python main.py demo")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
