# IHMT — Infinite Hierarchical Memory Tree

A universal, domain-agnostic long-term memory for LLMs, stored as a recursive tree of plain files on
the local disk. No vector database, no server, **no third-party dependencies** — Python 3.10+ and the
standard library.

Instead of embedding everything into one flat index and scanning it, IHMT organizes knowledge into a
tree: raw text leaves at the bottom, recursive JSON summaries above them, and a single `root.json`
trunk at the top. A query walks that tree — root → branch → branch → leaf — so the number of files
opened grows with the *depth* of the tree (`≈ beam × log_B(n)`), not with the amount stored.

| | Flat RAG | IHMT |
|---|---|---|
| Retrieval cost | scan / ANN over all *n* chunks | `beam × log_B(n)` file reads |
| Structure | none — a bag of vectors | explicit hierarchy, inspectable |
| Chunking | fixed character windows | syntax-, scene- and date-aware |
| Stale facts | served silently | superseded, dated, and flagged |
| Ambiguity | returns a plausible guess | asks you for a clue |
| Storage | binary index | UTF-8 `.txt` + JSON you can read |

> **Compatibility.** Officially supported: **Claude Code**. Also tested: **Codex** (CLI and the
> ChatGPT desktop app — [setup](GUIDE.md#56-using-it-with-codex-tested)) and **opencode**
> ([setup](GUIDE.md#57-using-it-with-opencode-tested)). IHMT is a standard stdio MCP server, so other
> MCP clients may work, but they are not tested or documented yet. All of them can share one memory.

Your memory is **local and private**: a folder on your disk that IHMT never uploads or shares. Every
person who installs IHMT starts with their own, empty memory.

**New here? Read [`GUIDE.md`](GUIDE.md)** — installation and everyday use, step by step, with real
outputs. This README is the technical reference.

---

## Install

Requirements: Python 3.10+, git, and the [Claude Code](https://claude.com/claude-code) CLI to use it
as memory for Claude.

```bash
git clone https://github.com/gonzaroman/IHMT-MEMORY.git
cd IHMT-MEMORY
python3 -m venv .venv
.venv/bin/pip install -r requirements-mcp.txt
claude mcp add ihmt-memory -s user -e IHMT_HOME="$PWD" -- "$PWD/.venv/bin/python" "$PWD/mcp_server.py"
claude mcp list                     # ihmt-memory … ✔ Connected
```

`IHMT_HOME` is where the memory will live (created on first use); the server name must come before
`-e`. Then add the usage instructions to your `~/.claude/CLAUDE.md` —
[GUIDE.md §5.4](GUIDE.md#54-tell-claude-when-to-use-it-recommended) has a ready-to-paste template.
Windows, the project scope, the graphical setup and troubleshooting are all covered in the
[guide](GUIDE.md#5-connect-it-to-claude-code).

**Using Codex?** Register it with
`codex mcp add ihmt-memory --env IHMT_HOME="$PWD" -- "$PWD/.venv/bin/python" "$PWD/mcp_server.py"`,
then add `default_tools_approval_mode = "approve"` to the `[mcp_servers.ihmt-memory]` table in
`~/.codex/config.toml` and put the instructions in `AGENTS.md` — details in
[GUIDE.md §5.6](GUIDE.md#56-using-it-with-codex-tested).

**Using opencode?** Add an `"mcp"` entry to `~/.config/opencode/opencode.json` —
`"ihmt-memory": {"type": "local", "command": ["<abs>/.venv/bin/python", "<abs>/mcp_server.py"],
"environment": {"IHMT_HOME": "<abs>"}}` — and put the instructions in `~/.config/opencode/AGENTS.md`;
details in [GUIDE.md §5.7](GUIDE.md#57-using-it-with-opencode-tested).

## Quick start

```bash
python3 gui.py                       # graphical interface: set up, browse, inspect
python3 init_ihmt.py                 # or from the terminal: create ./ihmt_memory
python3 main.py demo                 # full walkthrough in ./demo_workspace
python3 -m unittest discover -v      # stdlib only; MCP tests skip without the SDK
```

Then use it on your own material:

```bash
python3 main.py ingest ~/notes ~/project/src/Main.java
python3 main.py consolidate --force
python3 main.py search "how did we handle stock reservations"
python3 main.py ask "Luis"                    # interactive clue loop
python3 main.py conflicts                     # what changed over time
```

As a library:

```python
from ihmt import IHMT

memory = IHMT.initialize("./workspace")
memory.ingest_file("examples/InventoryService.java")
memory.ingest_file("examples/journal_personal.txt")
memory.flush()                                # close the tree up to the root

answer = memory.search("reserveStock soft hold")
print(answer.best.content)                    # the leaf
print(answer.best.path)                       # ['root', 'N1-software.java-…', 'L-software.java-…']
print(answer.node_reads)                      # how many branch files were opened

for notice in memory.notices():
    print(notice)   # "In 2024 you said user location = 'Madrid', but in 2026 you updated to 'Valencia'."
```

---

## Graphical interface

```bash
python3 gui.py                       # opens a browser at 127.0.0.1
python3 gui.py --path ~/my-memory --port 8765 --no-browser
```

Still zero dependencies — the server is `http.server` from the standard library, it listens only on
the loopback interface, and every `/api/*` call needs the random token carried in the URL it opens.

Four screens: **Set up** (pick the memory folder with the system dialog, create the store, choose
between per-project and global registration, preview the exact command or JSON before anything is
written), **Explore** (collapsible tree down to the stored text, with supersession notices),
**Diagnose** (a search that reports confidence, files opened vs. total, and the descent path), and
**Timeline** (active vs. historical values and the detected contradictions). The interface is
bilingual (ES/EN) and read-only over the memory: it never deletes or edits a leaf.

---

## Use it from Claude Code (MCP)

`mcp_server.py` exposes the tree to Claude Code as eight tools in three families: long-term memory,
project indexes and session scratch memory. The core stays dependency-free; the
SDK is an optional extra:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-mcp.txt        # mcp[cli]>=2.0
```

For a single project, copy `.mcp.json.example` to that project's `.mcp.json` and fill in the absolute
paths; Claude Code asks you to approve it on the next session there (`claude mcp list` shows it as
*Pending approval* until then):

```json
{
  "mcpServers": {
    "ihmt-memory": {
      "command": "/absolute/path/to/IHMT-MEMORY/.venv/bin/python",
      "args": ["/absolute/path/to/IHMT-MEMORY/mcp_server.py"],
      "env": { "IHMT_HOME": "/absolute/path/to/IHMT-MEMORY" }
    }
  }
}
```

or in one command:

```bash
claude mcp add ihmt-memory --scope user \
  -e IHMT_HOME=/absolute/path/to/IHMT-MEMORY \
  -- /absolute/path/to/IHMT-MEMORY/.venv/bin/python /absolute/path/to/IHMT-MEMORY/mcp_server.py
```

`IHMT_HOME` selects the store (`$IHMT_HOME/ihmt_memory`), created on first use. Point every project at
one shared directory for a single cross-project memory, or give each project its own.

| Tool | Behaviour |
|---|---|
| `search_memory(query, clue=None, detail="compact")` | Walks the tree. Compact output: the best memory with its date and `OUTDATED` notices, one line per other match; `detail="full"` adds ids, tree paths and excerpts. An ambiguous query returns an `AMBIGUOUS` block listing the candidates instead of guessing — call again with `clue`. A query that matches nothing says so. |
| `save_memory(content, domain="general", content_type="auto")` | Classifies, splits and stores the text, extracts dated facts, and keeps the tree consolidated. Reports how it was filed and — if the save contradicts something remembered earlier — the notice to relay to the user. |
| `project_map(path, detail="files", subpath="")` | Compact map of a codebase: files with their size in tokens and, with `detail="symbols"`, each method with its line range. Built from the sync manifest, without opening leaves. |
| `find_code(query, path, scope="main", subpath="", clue=None, max_tokens=1500)` | Returns only the symbol that answers the query, as `file:first-last` + code. `scope` is `main` (skip tests), `test` or `all`. |
| `read_file(path, force=False)` | Reads a file and remembers what it handed out this session: a repeated read answers `UNCHANGED` or only a unified diff. |
| `note(text)` / `recall(query, clue=None)` | Session scratch memory: survives a context compaction, disappears when the session ends. |
| `digest_output(text, label="output")` | Condenses a long log to its first lines, errors, failures, test totals and last lines; the full text stays recallable. |

### Saving tokens inside a session

Long-term memory saves tokens *between* sessions. The project tools save them *within* one, where the
cost is reading the same files again and again. `ProjectIndex` (`ihmt/project_index.py`) keeps a
private store per project under `$IHMT_PROJECTS_DIR` (default `$IHMT_HOME/ihmt_projects`):

* **one leaf per symbol** — `code_chunk_mode="symbol"`, so a lookup returns a method, not a file;
* **checksum sync on every call** — size and mtime first, SHA-256 only for what moved; changed files
  are re-ingested, removed ones deleted, and the branches rebuilt. Code is never served stale;
* **path-ordered branches** — each file is stamped with its rank in path order, so every branch covers
  neighbouring files and its summary stays meaningful for the descent;
* **code-aware ranking** — the navigator filters by `scope`/`path_prefix` from the catalog, prefers
  the file a query names, demotes tests unless asked and demotes import lines.

Measured on a 55-file Spring Boot project (8 typical questions): reading the files that hold the
answers costs 3,613 tokens; `find_code` returns the exact method for all 8 in 1,071. The map of the
project costs 660 tokens against 13,157 to read it whole.

Those savings are against an agent that **reads whole files**. In an A/B test with 22 real headless
Claude Code sessions, Claude preferred batched `grep`/`sed -n` and never called the project tools on
its own; *forcing* them made sessions 42–71 % more expensive. Keeping the server enabled costs about
460 tokens per conversation, since Claude Code loads MCP tools on demand. IHMT's main value is memory
**between** sessions; treat the project tools as optional, and do not mandate them in `CLAUDE.md`.

The server transparently supports MCP SDK 2.x (`MCPServer`), 1.x (`FastMCP`) and the standalone
`fastmcp` package.

The usage instructions in your `~/.claude/CLAUDE.md` ([template](GUIDE.md#54-tell-claude-when-to-use-it-recommended)) tell Claude Code *when* to reach for each tool: search before answering anything that
depends on earlier sessions, save durable facts with their date, never guess on `AMBIGUOUS`, always
relay `OUTDATED`, and never store secrets.

---

## Storage layout

Everything lives in one relocatable directory:

```
ihmt_memory/
  root.json                 # the trunk: domains, topics, top branches, counters
  layer_0/<domain>/*.txt    # the leaves: raw UTF-8 text + a strict JSON header
  layers/1/*.json           # branches: summaries of leaves
  layers/2..N/*.json        # branches: summaries of summaries
  state/catalog.json        # index: id -> path, domain, parent, timestamp
  state/facts.json          # the fact timeline
  ihmt.config.json          # branch factor, token budgets, backend
```

`root.json` lives *inside* `ihmt_memory/` so the store is self-contained: copy the directory and the
memory travels with it.

**A leaf** is a normal text file that describes itself, so it stays meaningful even if the catalog is
lost:

```
<<<IHMT-META
{
  "leaf_id": "L-software.java-0001-84130ee123",
  "timestamp": "2026-09-09T17:45:00Z",
  "data_type": "CODE",
  "domain": "software.java",
  "tags": ["method:reserveStock", "class:InventoryService", "lang:java", "type:code"],
  "parent_id": "N1-software.java-5571a7baac",
  "span": {"start_line": 43, "end_line": 68},
  "checksum": "sha256:…",
  "extra": {"context": "public final class InventoryService {"}
}
IHMT-META>>>
    public Optional<String> reserveStock(Sku sku, int quantity) {
        …
```

**A branch node** embeds each child's title, excerpt and keywords. That is the detail that makes the
descent cheap: a branch can be *ranked without opening any of its children*.

---

## The five components

### 1. `UniversalIngestor` — detect, split, enrich, store

`DomainDetector` classifies each document by **type** (`CODE`, `NARRATIVE`, `CLINICAL`, `PERSONAL`,
`PROCESS`, `GENERIC`) and **domain** (`software.java`, `medicine.clinical`, `process.cooking`,
`personal`, …) from its extension plus lexical signatures in English and Spanish. Both can be
overridden with `--domain` / `--type`.

The type selects the splitter, and every splitter obeys one invariant:

> **A logical block is never cut.** If a single block exceeds `max_tokens` it is stored whole and
> flagged `oversized`. Correctness of the block beats hitting the token budget.

* **Code** (`chunkers/code.py`) — Python via the stdlib `ast`; Java/JS/TS/C/C++/C#/Go/Rust/Kotlin/
  Swift/PHP via `BraceScanner`, a character-level scanner that tracks brace depth while skipping
  comments, string literals, char literals, template literals and preprocessor lines. Imports
  coalesce; each class/function is a block; an oversized class splits **per member**, and the
  enclosing class header travels in the leaf's `extra.context` rather than being spliced into the
  text. Concatenating a file's leaves reproduces the file **byte for byte** — asserted in the tests.
* **Narrative** — paragraph-atomic, with `***`, `---`, `Chapter`/`Capítulo` as hard boundaries. Only
  a paragraph larger than `max_tokens` is split, and then at sentence boundaries.
* **Temporal** (journals, chats, clinical records) — one dated entry is atomic and a change of date
  is a hard boundary, so a leaf never mixes two encounters. **The date found in the text becomes the
  leaf's timestamp**, which is what makes recency weighting mean *when something was true* rather
  than when it was ingested.
* **Process** (recipes, protocols, runbooks) — steps and ingredient lists stay attached to their
  heading.

Leaf ids are derived from `(domain, source, position, content)`, so re-ingesting an unchanged
document rewrites the same leaves instead of duplicating them.

### 2. `RecursiveSummarizer` — the Summarization Event

It watches layer 0. When `branch_factor` leaves of one domain have no parent, it fires a
**Summarization Event**: they are condensed into a layer-1 node, the node is written, and *only then*
are the children stamped with their `parent_id` — so an interrupted run re-processes a group instead
of orphaning it. The same rule applies from layer 1 to layer 2, and so on, until the tree converges;
then `root.json` is rewritten.

`consolidate()` is idempotent. `flush()` (`--force`) also promotes partial groups so the tree closes
completely. Anything still unconsolidated is referenced directly by the trunk, so **nothing in the
store is ever unreachable from the root**.

### 3. `SemanticNavigator` — descent and the Interactive Clue Loop

Ranking uses Okapi BM25 over each candidate's title, keywords, tags and excerpt, with field weights
and document frequencies computed **across the siblings of the current level** — exactly the
discrimination the descent needs, at no extra I/O cost. The walk keeps a beam of `beam_width`
branches per level.

Confidence blends two independent signals:

```
confidence = 0.6 × coverage + 0.4 × margin
```

*Coverage* asks "does this leaf actually contain what was asked?"; *margin* asks "is it
distinguishable from its rivals?". A common first name scores high on the first and near zero on the
second — which is precisely when the system must not guess:

```
$ python main.py ask "Luis"

"Luis" is ambiguous (3 memories match this query equally well, confidence 0.62).
It could belong to any of these branches:
  1. [personal] journal_personal.txt · 2024-07-22 — Vacaciones en Benidorm con Luis, mi primo…
  2. [personal] journal_personal.txt · 2026-08-30 — Fin de semana en la playa de El Saler con Luis…
  3. [personal] journal_personal.txt · 2024-11-30 — Cierre de trimestre… Luis Marín revisó el pull request…
Give me a clue to narrow it down (e.g. a place, a date, a project):
> vacaciones en Benidorm

query: "Luis + vacaciones en Benidorm" · confidence 0.74 · 5 node reads, 3 leaf reads, depth 2
  1. [personal] journal_personal.txt · 2024-07-22
     path  root → N2-personal-ea34db33ae → N1-personal-59bf8bc291 → L-personal-0002-a17b3c8f35
```

The clue triggers a **joint cross-reference**: candidates matching *both* term groups are boosted
(×1.6), candidates matching only one are demoted (×0.7). The loop runs up to `max_clue_rounds`
times, stops early if the user declines, and never silently converts an ambiguous query into a
confident answer.

`clue_provider` is any callable, so the loop works for a human at a terminal (`input`) or for an
agent that lets the LLM supply its own follow-up.

### 4. `ConflictResolver` — recency weighting and the timeline

Facts are `(subject, attribute, value, timestamp, source_leaf)`, recorded programmatically via
`record_fact()` or extracted at ingest time by pattern rules (`vivo en X` / `I live in X`,
`mi stack es Y`, `trabajo en Z`, `Diagnóstico:`, `Tratamiento:`, `Medicación:` …; extend with
`add_pattern`).

Each `(subject, attribute)` keeps a dated timeline. The newest value is `ACTIVE`; every earlier one
becomes `HISTORICAL` with `superseded_by` and a `valid_from`/`valid_to` interval. **Nothing is
deleted**, so both questions stay answerable:

```python
memory.resolver.active_state()["user::location"].value      # 'Valencia'  (now)
memory.resolver.state_at("2024-12-31")["user::location"].value  # 'Madrid'  (back then)
```

Repeating a value at a later date is a confirmation, not a contradiction. A genuine change produces a
transparent notice — *"In 2024 you said user location = 'Madrid', but in 2026 you updated to
'Valencia'."* — and the superseded leaf is annotated, so retrieving outdated material always arrives
with its correction attached (`SearchResult.notices`).

A leaf itself stays `ACTIVE`: what it says was true *on its own date*, and that remains the right
answer to a historical question. What changes is that it can no longer be read as current.

### 5. Summarization backends

```python
class SummarizerBackend(Protocol):
    name: str
    def summarize(self, children, *, domain: str, layer: int) -> NodeSummary: ...
```

* `HeuristicSummarizer` (default) — stdlib extractive summarization: TF term ranking with EN/ES stop
  words plus representative-sentence selection. Offline, deterministic, which is what lets the test
  suite assert on tree shape.
* `AnthropicSummarizer` (optional) — used only when selected *and* the `anthropic` package and
  `ANTHROPIC_API_KEY` are both present. Every failure path (missing SDK, missing key, network error,
  unparseable reply) falls back to the heuristic backend, so a consolidation is never lost because a
  model was unreachable.

```bash
python3 init_ihmt.py --backend anthropic     # model set by summarizer_model in ihmt.config.json
```

Any other model or local runtime plugs in by implementing the same protocol and passing it as
`IHMT(..., backend=MyBackend())`.

---

## CLI reference

| Command | Purpose |
|---|---|
| `init [--branch-factor N] [--target-tokens N] [--force]` | create the store |
| `ingest <paths…\|-> [--domain D] [--type T] [--tag X] [--no-consolidate]` | ingest files, directories or stdin |
| `consolidate [--force]` | run pending Summarization Events |
| `search <query> [--top-k N] [--full]` | walk the tree |
| `ask <query> [--clue TEXT] [--top-k N]` | search with the clue loop |
| `tree [--depth N]` | outline of the hierarchy |
| `stats`, `facts [--subject S]`, `conflicts [--subject S]` | inspection |
| `rebuild` | rebuild catalog, timeline and trunk from the files |
| `demo` | end-to-end walkthrough |

`--path` selects the store directory and `--json` emits machine-readable output; both work before or
after the subcommand.

## Configuration

`ihmt_memory/ihmt.config.json`:

| Key | Default | Meaning |
|---|---|---|
| `branch_factor` | 8 | children per branch; the log base of retrieval cost |
| `target_tokens` / `max_tokens` | 2000 / 3000 | leaf size target and oversize threshold |
| `beam_width` | 3 | branches kept alive per level |
| `confidence_threshold` | 0.45 | below this, ask for a clue |
| `ambiguity_margin` | 0.18 | score gap under which candidates count as tied |
| `max_clue_rounds` | 3 | clue-loop iterations |
| `summarizer_backend` / `summarizer_model` | `heuristic` / `claude-sonnet-5` | summarization |
| `code_chunk_mode` | `pack` | `symbol` stores one leaf per class member (used by project indexes) |

Small corpora deserve a small branch factor — the demo uses `branch_factor=4, target_tokens=400` so a
handful of documents still builds a genuine multi-layer tree.

## Tests

```bash
python3 -m unittest discover -v          # from the project root
```

187 tests — 25 of them for the MCP server, skipped without the SDK — covering: byte-exact
reconstruction and boundary-depth invariants for Java and Python, scene/date/section atomicity,
oversized-block handling, leaf header round-trips, catalog recovery, summarization thresholds, upward
propagation, idempotence, full reachability from the root, descent cost bounds, the clue loop,
recency weighting, historical preservation, the project indexes, the MCP tools, the graphical
interface and the CLI.

## Design notes and limits

* **Retrieval is a descent, not a scan.** That is the whole point, and it means a branch pruned at
  the trunk is not revisited. Domain-level pruning only happens when a query has actual signal at the
  trunk; if it has none, every domain stays in play and the beam applies one level down. The clue
  loop is the recovery mechanism when the descent goes wide.
* **Lexical, not semantic.** Matching is BM25 over accent-folded, CamelCase-split tokens: it works in
  any language and needs no model, but it will not match a synonym. Plugging an embedding re-ranker
  into `BM25Ranker` is the natural upgrade; the tree structure does not change.
* **Token counts are estimated** at ~4 characters per token. Budgets only need to be consistent, not
  exact.
* **Fact extraction is pattern-based.** The bundled rules cover common English/Spanish phrasings and
  clinical headers; `record_fact()` is the reliable path, and `add_pattern()` extends the rules.
* **Single-writer.** Writes are atomic (`tmp` + `os.replace`) and catalog rebuilds take a lock file,
  but the store assumes one writer at a time.
* `initialize(force=True)` discards *derived* state only — branches, catalog, timeline — and detaches
  the surviving leaves so the next consolidation rebuilds the hierarchy. Leaf content is never
  deleted.

## Layout

```
ihmt/
  api.py                    IHMT facade wiring everything together
  config.py                 IHMTConfig
  models.py                 MemoryLeaf, BranchNode, ChildRef, RootIndex, Fact, Contradiction
  storage.py                MemoryStore: atomic I/O, catalog, recovery
  textutils.py              tokenizing, keywords, entities, extractive summary, timestamps
  detectors.py              DomainDetector
  chunkers/                 base · code · narrative · temporal · process · generic
  summarizers.py            SummarizerBackend · Heuristic · Anthropic
  universal_ingestor.py     UniversalIngestor
  recursive_summarizer.py   RecursiveSummarizer
  semantic_navigator.py     SemanticNavigator, BM25Ranker, ClueRequest, scope/path filters
  project_index.py          ProjectIndex: per-project code cache with checksum sync
  conflict_resolver.py      ConflictResolver + timeline manager
ihmt_gui/                   local graphical interface (stdlib only)
mcp_server.py               MCP server: the eight tools
init_ihmt.py · main.py · gui.py · examples/ · tests/
GUIDE.md                    installation and usage guide
```

## License

[MIT](LICENSE) © 2026 Gonzalo Román Márquez (gonzaroman)
