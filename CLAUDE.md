# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

**IHMT (Infinite Hierarchical Memory Tree)** — a dependency-free Python framework giving an LLM
structured long-term memory as a recursive tree of files, plus an MCP server exposing it as tools.
Retrieval walks the tree (`≈ beam_width × log_B(n)` file reads) instead of scanning it.

## Commands

`python main.py --help` lists the subcommands. `--path DIR` selects the store and `--json` gives machine-readable output; both work before or after
the subcommand.

### Tests

```bash
python -m unittest discover                      # the MCP tests skip without the SDK
.venv/bin/python -m unittest discover            # includes the MCP tests
```

Run from the repository root — `tests/` is a package using relative imports, so
`discover -s tests` needs `-t .`.

### Optional dependencies

The core (`ihmt/`, `main.py`, `init_ihmt.py`) imports **only the standard library**. The MCP server
needs the SDK, isolated in a venv:

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-mcp.txt
```

`ihmt/summarizers.py` (`anthropic`) and `mcp_server.py` (`mcp`) are the only files touching
third-party packages, and both are import-guarded with working fallbacks. Keep it that way —
`tests/test_gui.py` enforces it for `ihmt_gui/`, which is stdlib-only including its frontend.

## Architecture

Three node kinds on disk under `ihmt_memory/`: `.txt` **leaves** (layer 0, raw text + a strict JSON
header), JSON **branch nodes** (layers 1..N, summaries of summaries), and `root.json`, the **trunk**.
`state/catalog.json` indexes everything; `state/facts.json` holds the fact timeline.

**Ingest** (`universal_ingestor.py`): `detectors.py` classifies type + domain → `chunkers/` splits →
leaf written with metadata → facts extracted → consolidation triggered. **Retrieve**
(`semantic_navigator.py`): `root.json` → rank domains → beam-descend the branches → open the winning
leaves. `api.py` wires all five components into the `IHMT` facade that `main.py` and `mcp_server.py`
both use — construct components through it rather than by hand.

Five things that are load-bearing and not obvious from any single file:

**Chunking is two-phase.** Each splitter detects `Block`s (a syntactic unit, a paragraph, a dated
entry) and `Chunker.pack()` merges consecutive blocks up to `target_tokens`. Cuts only ever happen
*between* blocks. A block larger than `max_tokens` is stored whole and flagged `oversized` — block
integrity outranks the token budget. `tests/test_chunkers.py` asserts byte-exact reconstruction; do
not weaken those assertions.

**A parent must be rankable without opening its children.** `ChildRef` embeds each child's title,
excerpt, keywords and tags in the parent node. This is the entire basis of logarithmic retrieval —
anything you add to a node must also be summarized into its parent, or the descent goes blind.

**`parent_id is None` is the work queue.** `MemoryStore.pending(layer)` returns unparented entries;
when `branch_factor` of them share a domain, `RecursiveSummarizer` fires a Summarization Event. The
parent is written to disk *before* children are stamped with `parent_id`, so an interrupted run
re-processes a group instead of orphaning it. Preserve that ordering. Anything still unparented is
referenced directly by the trunk, so nothing is ever unreachable from the root.

**The files are the source of truth; the catalog is a rebuildable index.** `rebuild_catalog()`
reconstructs it by scanning leaf headers and node JSON. Consequently `initialize(force=True)`
discards *derived* state only — branches, catalog, timeline — and detaches surviving leaves so the
next consolidation rebuilds the hierarchy. Leaf content is never deleted.

**Retrieval refuses to guess.** `confidence = 0.6 × coverage + 0.4 × margin` — coverage asks "does
this leaf contain what was asked?", margin asks "is it distinguishable from its rivals?". A bare
first name scores high on the first, near zero on the second, which triggers a `ClueRequest` instead
of an answer. Zero-scoring leaves are filtered out entirely, so "nothing is stored about this" never
masquerades as a weak match. Matching is lexical BM25 over accent-folded, CamelCase-split tokens with
document frequencies computed across the siblings of the current level — no model, any language, but
no synonyms either.

**Timeline is separate from content.** Facts carry `ACTIVE`/`HISTORICAL`; leaves stay `ACTIVE` even
when superseded, because what a leaf says was true *on its date* — it just gains a notice so it can
never read as current. Search results surface those notices via `SearchResult.notices`.

Small corpora need a small `branch_factor` to build a real multi-layer tree — the demo and the tests
use `branch_factor=4, target_tokens=400` against the default `8/2000`.

**Project indexes are caches, memories are not.** `ProjectIndex` (`project_index.py`) keeps one store
per source tree under `$IHMT_PROJECTS_DIR` with `code_chunk_mode="symbol"`, and may *delete* leaves of
changed or removed files (`MemoryStore.delete_leaf`) before rebuilding the branches with
`IHMT.initialize(force=True)` + `flush()`. Long-term memory never deletes: re-ingesting a changed file
through `ingest_file` demotes the leaves of its previous version to `HISTORICAL`, and the navigator skips
`HISTORICAL` leaves unless `include_historical=True`. Never pass `replace_previous=True` for a source
shared by unrelated documents (`mcp://claude-code`), or every save would demote the previous ones.

Project leaves are stamped with their file's rank in path order (not the ingest time), because branches
group pending leaves by timestamp: without it, a branch mixes methods of unrelated files and the descent
goes blind. Bulk writes go through `MemoryStore.batch()`, which writes the catalog once on exit instead
of once per leaf.

In `mcp_server.py`, **stdout is the JSON-RPC channel**: log to stderr, never `print()`.

---

## Memory protocol

The `search_memory` / `save_memory` protocol belongs in the user's `~/.claude/CLAUDE.md`, which loads
in every session, so it is not repeated here — two copies would drift apart. `GUIDE.md` (section 5.4)
ships a template for it. This repo *builds* that memory; it follows the same rules as any other
project when using it.

One thing specific to working here: this project's own memory store is the default `IHMT_HOME`, so
notes saved while developing IHMT land in `./ihmt_memory`. Keep test data out of it — use
`--path` or a temporary directory instead.
