# IHMT Guide — what it is, how to install it, and how to use it

> A guide for humans, written to be followed step by step. The technical reference (architecture,
> components, storage format) is in [`README.md`](README.md); the instructions for developing IHMT
> itself with Claude Code are in [`CLAUDE.md`](CLAUDE.md).

> **Compatibility.** Officially supported: **Claude Code**. Also tested: **Codex** (CLI and the
> ChatGPT desktop app, [section 5.6](#56-using-it-with-codex-tested)) and **opencode**
> ([section 5.7](#57-using-it-with-opencode-tested)). IHMT is a standard stdio MCP server, so any
> agent that supports local MCP servers should work — GitHub Copilot, Antigravity, Cursor, Windsurf,
> Gemini CLI, Claude Desktop… [`INSTALL.md`](INSTALL.md) knows how to configure them, from their
> official documentation, but we have not tested them yet (see [Roadmap](#13-roadmap)). All of them
> can share one memory.

## Contents

1. [What it is](#1-what-it-is)
2. [What it is for — and what it is not for](#2-what-it-is-for--and-what-it-is-not-for)
3. [Requirements](#3-requirements)
4. [Installation, step by step](#4-installation-step-by-step)
5. [Connect it to Claude Code](#5-connect-it-to-claude-code) — and [to Codex](#56-using-it-with-codex-tested) or [opencode](#57-using-it-with-opencode-tested)
6. [Using it day to day with Claude Code](#6-using-it-day-to-day-with-claude-code)
7. [The graphical interface](#7-the-graphical-interface)
8. [The command line (CLI)](#8-the-command-line-cli)
9. [The Python API](#9-the-python-api)
10. [Configuration](#10-configuration)
11. [Honest numbers](#11-honest-numbers)
12. [Maintenance, troubleshooting and FAQ](#12-maintenance-troubleshooting-and-faq)
13. [Roadmap](#13-roadmap)
14. [Cheat sheet](#14-cheat-sheet)

---

## 1. What it is

**IHMT (Infinite Hierarchical Memory Tree)** is a **long-term memory for language models**, stored as
plain files on your own disk.

The idea in one sentence:

> Instead of keeping everything you know in one flat pile and searching all of it every time, IHMT
> organizes it as a **tree of summaries** and walks down the branches to the exact piece you need.

It is how you find a book in a library. You do not read 40,000 spines: you look at the floor
(Science), the aisle (Biology), the shelf (Genetics) and take *one* book. Four signs instead of forty
thousand spines.

IHMT does the same with what you tell it:

```
root.json                        ← the trunk: "there are 6 domains here"
 ├── software.java               ← a branch
 │    └── N1-software.java-…     ← summary of 4 code fragments
 │         ├── InventoryService.reserveStock      ← a leaf (the real text)
 │         └── InventoryService.confirmReservation
 ├── personal
 │    └── N2-personal-…          ← a summary of summaries
 │         └── N1-personal-…
 │              └── "2024-07-22: Holidays in Benidorm with Luis, my cousin…"
 └── medicine.clinical
```

- **Leaves** (`layer_0/`) are `.txt` files with your text exactly as saved, plus a JSON header with
  the date, domain, tags and parent.
- **Branches** (`layers/1/`, `layers/2/`…) are JSON files with summaries. Each branch summarizes its
  children, and higher branches summarize the ones below. Summaries of summaries.
- The **trunk** (`root.json`) is the global index every search starts from.

Everything is a text file you can open with any editor. No database, no server, no external
dependencies for the core: just Python and its standard library.

**Your memory is yours alone.** It lives in a folder on your machine. IHMT does not sync it anywhere
and nothing is shared between different people: every person who installs IHMT gets their own, empty
memory.

---

## 2. What it is for — and what it is not for

### The problem

A language model **remembers nothing between conversations**. Every session starts blank, so you end
up explaining your stack, your decisions, your project's name and why you ruled out Postgres, over and
over again.

The usual workaround is to *paste everything into the context*: notes, code, history. Two problems:

1. **It does not fit.** Context is finite. With enough history, it simply does not go in.
2. **You pay for all of it on every turn.** Even when it fits, sending 70,000 tokens of notes so the
   model can use three sentences wastes money and distracts it.

The classic alternative (RAG over a vector database) helps, but it searches *every* fragment each
time, has no structure, and happily serves stale information.

### What IHMT brings

| | Flat RAG | IHMT |
|---|---|---|
| Search cost | looks at all *n* fragments | opens `beam × log(n)` files |
| Structure | none — a bag of vectors | explicit, readable hierarchy |
| Chunking | windows of N characters | respects classes, functions, scenes, dates |
| Stale data | served as if current | marked as superseded, and you are told |
| Ambiguity | returns something plausible | **asks you for a clue** |
| Format | binary index | `.txt` and `.json` you can read and edit |

Three things make IHMT different:

**a) It never splits a logical block.** When saving code, a class or a method is never cut in half.
A diary never mixes two dates in one leaf. A recipe never separates the steps from their heading. It
sounds minor, and it is what makes retrieved text *usable* instead of a mangled fragment.

**b) It knows when it does not know.** Search for "Luis" with three memories about different Luises,
and it will not pick one at random: it returns all three and asks for a clue. Give it "Benidorm
holidays" and it combines both to find the right one.

**c) It handles time.** If in 2024 you said "I live in Madrid" and in 2026 "I live in Valencia", it
keeps both. Valencia becomes the **active** value, Madrid the **historical** one, and when the old
note comes back it carries a notice: *"In 2024 you said user location = 'Madrid', but in 2026 you
updated to 'Valencia'."* Nothing is deleted: you can still ask where you lived in 2024.

### What it is not for

- **It is not a semantic search engine.** It matches **words**, not synonyms. If you saved "car" and
  search for "automobile", it will not find it. (An embedding re-ranker could be added without
  changing the tree.)
- **It does not replace git or your documentation.** Do not store what the code already says.
- **It is not a multi-user database.** It assumes **one writer** at a time.
- **It is not a way to share memory between people.** Each installation is private (see above).

### What you can store

IHMT detects the kind of material on its own and splits it by that kind's rules. It works the same in
English and Spanish (and the heuristics cover both).

| Type | Examples | How it is split |
|---|---|---|
| **CODE** | Java, Python, JS, TS, C, C++, C#, Go, Rust, Kotlin, Swift, PHP | by complete classes, functions and methods |
| **NARRATIVE** | stories, novels, articles, essays | by paragraphs and scenes/chapters |
| **PERSONAL** | diaries, chats, day-to-day notes | one entry per date |
| **CLINICAL** | medical histories, consultations | one encounter per date, with patient and diagnosis |
| **PROCESS** | recipes, protocols, manuals, runbooks | by sections, never separating steps from their heading |
| **GENERIC** | anything else | by paragraphs |

Typical uses:

- **Programming**: architecture decisions and why, team conventions, hard-won snippets, bugs and
  their root cause.
- **Medicine**: per-patient histories, with the diagnosis evolving over the years and an automatic
  notice when it changes.
- **Cooking**: recipes with their steps intact, variations you tried and how they turned out.
- **Studying**: notes per topic, exam dates, what was in scope.
- **Personal life**: where you live and work, who is who, what happened and when.

`examples/` has one of each (fictional data, some of it in Spanish on purpose) to see it working.

---

## 3. Requirements

| What | Needed for | Check it with |
|---|---|---|
| **Python 3.10 or newer** | everything | `python3 --version` |
| **git** | cloning the repository | `git --version` |
| **Claude Code CLI** | using the memory from Claude Code (section 5) | `claude --version` |
| *or* **Codex** | using the memory from Codex instead ([5.6](#56-using-it-with-codex-tested)) | `codex --version` |
| *or* **opencode** | using the memory from opencode instead ([5.7](#57-using-it-with-opencode-tested)) | `opencode --version` |
| The `mcp` Python package | only the MCP server; installed in step 5.1 | — |

Operating systems: **tested on macOS and Linux.** Windows should work, but it is **not tested**; where
commands differ, this guide gives the Windows version marked *(Windows, untested)*.

The core (`ihmt/`, `main.py`, `init_ihmt.py`, `gui.py`) needs nothing beyond the standard library.

---

## 4. Installation, step by step

> **The easy way: let your AI agent do it.** Paste this into the agent you want to give a memory to
> (Claude Code, Codex, opencode, GitHub Copilot, Antigravity, Cursor…):
>
> ```
> Install the IHMT memory MCP server for me from https://github.com/gonzaroman/IHMT-MEMORY — follow the instructions in its INSTALL.md.
> ```
>
> The agent follows [`INSTALL.md`](INSTALL.md), which does everything in sections 4 and 5 for you:
> code in `~/IHMT-MEMORY`, memory in `~/.ihmt`, server registered with **that agent only**, usage
> instructions added. Then start a new session.
>
> To add another agent later, paste the same prompt there. It detects the existing installation
> and connects to the same memory instead of starting a new one.
>
> The rest of this section is for doing it by hand, or for understanding what the agent did.

### 4.1 Get the code

```bash
git clone https://github.com/gonzaroman/IHMT-MEMORY.git ~/IHMT-MEMORY
cd ~/IHMT-MEMORY
```

Everything below assumes you are inside that folder. Pick a permanent location: your agents will
point at this folder by absolute path, so moving it later means re-registering (see
[Maintenance](#121-maintenance)).

### 4.2 Check that the core works (optional, 30 seconds)

```bash
python3 -m unittest discover
```

You should see `OK` at the end. Without the MCP package installed, the MCP server tests are
*skipped*; that is expected at this point.

Then watch it work end to end:

```bash
python3 main.py demo
```

The demo ingests a Java class, a Python module, a short story, a diary, a clinical history and a
recipe (the files in `examples/`); builds the tree; runs searches; triggers the clue loop with "Luis";
and detects contradictions between dates. It writes to `./demo_workspace`, so it never touches your
real memory. Delete that folder whenever you like.

### 4.3 Create your memory

You have two ways. Both create an `ihmt_memory/` folder — **that folder is your memory**.

**With the graphical interface** (recommended the first time):

```bash
python3 gui.py
```

Your browser opens on the *Set up* screen: pick the folder, press *Create the memory here*, and
continue with [section 7](#7-the-graphical-interface), which can also register it in Claude Code for
you.

**From the terminal:**

```bash
python3 init_ihmt.py --path ~/.ihmt       # recommended: memory apart from the code
python3 init_ihmt.py                      # or: ./ihmt_memory inside the current folder
```

Output:

```
IHMT store ready at /Users/you/.ihmt/ihmt_memory
  trunk           : root.json
  leaves (layer 0): /Users/you/.ihmt/ihmt_memory/layer_0
  branches        : /Users/you/.ihmt/ihmt_memory/layers/1..N
  state           : /Users/you/.ihmt/ihmt_memory/state
  config          : branch_factor=8, target_tokens=2000, max_tokens=3000, backend=heuristic
  contents        : 0 leaves, 0 nodes, depth 0
```

You can also skip this step: the MCP server creates the memory automatically the first time it runs.

> **Where should the memory live?** We recommend **`~/.ihmt`**, apart from the code: updating or
> reinstalling IHMT never touches it, and taking your memory to another computer means copying one
> folder. Inside the repository folder also works (`ihmt_memory/` is in `.gitignore`, so it is never
> committed), but then deleting the repository to reinstall would delete your memory too. Whatever
> you choose, use that folder as `IHMT_HOME` in the next section.

---

## 5. Connect it to Claude Code

### 5.1 Install the MCP package in a virtual environment

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-mcp.txt
```

*(Windows, untested)*

```powershell
py -m venv .venv
.venv\Scripts\pip install -r requirements-mcp.txt
```

This installs only `mcp` (the official MCP SDK) inside `.venv/`. Your system Python is untouched.
Run the tests again and the MCP ones now run too:

```bash
.venv/bin/python -m unittest discover
```

### 5.2 Register the server

There are two scopes. Choose one.

| | **User scope** (recommended) | **Project scope** |
|---|---|---|
| Where it works | in **every** folder you open Claude Code in | only inside one project folder |
| Memory | one memory for everything | one memory per project (if you want) |
| Approval | none needed | Claude Code asks you to approve it |
| Stored in | your Claude Code user config | a `.mcp.json` file in the project |

#### Option A — user scope (recommended)

From inside the `IHMT-MEMORY` folder:

```bash
claude mcp add ihmt-memory -s user -e IHMT_HOME="$HOME/.ihmt" -- "$PWD/.venv/bin/python" "$PWD/mcp_server.py"
```

What each part means:

- `ihmt-memory` — the server's name. **It must come before `-e`**: `-e` accepts several values, so a
  name placed after it would be swallowed as another environment variable.
- `-s user` — user scope: available in every project.
- `-e IHMT_HOME="$HOME/.ihmt"` — **where the memory lives**: the folder that contains (or will
  contain) `ihmt_memory/`; it is created on first use. Use an absolute path (`$HOME` expands to
  one). If your memory is elsewhere, put that folder here.
- `--` — everything after it is the command that starts the server: the venv's Python and
  `mcp_server.py`, both as absolute paths (`$PWD` expands to them).

*(Windows, untested)*

```powershell
claude mcp add ihmt-memory -s user -e IHMT_HOME="$HOME\.ihmt" -- "$PWD\.venv\Scripts\python.exe" "$PWD\mcp_server.py"
```

#### Option B — project scope

Copy the template into the **project** where you want the memory, and edit the three paths:

```bash
cp .mcp.json.example ~/projects/my-app/.mcp.json
```

```json
{
  "mcpServers": {
    "ihmt-memory": {
      "command": "/absolute/path/to/IHMT-MEMORY/.venv/bin/python",
      "args": ["/absolute/path/to/IHMT-MEMORY/mcp_server.py"],
      "env": { "IHMT_HOME": "/absolute/path/to/where/the/memory/lives" }
    }
  }
}
```

The next time you run `claude` inside that project, Claude Code asks you to approve the server. Say
yes. (It asks because a `.mcp.json` could come inside someone else's repository.)

> Prefer clicking to typing? `python3 gui.py` builds exactly this command or file for you, shows it
> before doing anything, and applies it when you press *Apply*. See [section 7](#7-the-graphical-interface).

### 5.3 Check that it works

```bash
claude mcp list
```

You should see a line like:

```
ihmt-memory: /…/IHMT-MEMORY/.venv/bin/python /…/IHMT-MEMORY/mcp_server.py - ✔ Connected
```

- `✔ Connected` — ready.
- `⏸ Pending approval` — project scope: run `claude` in that project folder and approve it.
- `✘ Failed to connect` — see [Troubleshooting](#122-troubleshooting).

Inside a Claude Code session, `/mcp` shows the same status. Note that Claude Code may load MCP tools
*on demand* (deferred), so the IHMT tools do not necessarily appear in the tool list until they are
needed. That is normal.

A quick end-to-end test in a new Claude Code session:

> **You:** remember that my favourite editor is Helix
>
> *(Claude calls `save_memory`)*
>
> **You, in a new session:** what is my favourite editor?
>
> *(Claude calls `search_memory` and answers "Helix", with the date you said it)*

### 5.4 Tell Claude when to use it (recommended)

The server already sends Claude brief instructions, but the most reliable way is to add a few lines
to your personal `~/.claude/CLAUDE.md`, which Claude Code reads in every project. Copy this block and
adapt it (it is also in the repository as
[`templates/memory-instructions.md`](templates/memory-instructions.md), ready to append):

```markdown
# Memory: use IHMT

I have **IHMT** installed, a long-term memory MCP server available in all my projects with the tools
`search_memory` and `save_memory`. It is my main memory.

## Search before answering

Call `search_memory` **before** answering when the answer depends on something that is not in this
conversation: my setup and tools, past decisions and their reasons, people and projects I mention by
name, how I like to work, or anything that implies you should already know ("like last time").

Search with several distinctive words, not a bare name. Do not search for things you can read from
the project's code or for general knowledge. If nothing is found, say so instead of guessing.

**`AMBIGUOUS`** — several memories fit equally well. Do not pick one: ask me which, or call again
with `clue` set to a distinguishing detail.

**`OUTDATED:`** — what you found was updated later. Always tell me; never treat as current something
I have since changed.

## Save what lasts

Call `save_memory` when I say something durable: decisions and why, how my environment is set up, how
I want you to work, people and projects, code or procedures I want to keep, and above all
**corrections** — when something changes, save the new version; IHMT dates it and marks the old one
as historical.

When I say "remember this" or "save this", that is `save_memory`.

Write entries that make sense on their own six months from now. "Prefers pytest" is not enough;
"2026-09-10: prefers stdlib unittest to avoid adding dependencies" is. **If the fact belongs to a
specific date, start with it** — that is what makes recency and the timeline work.

**Never save** passwords, keys or tokens; passing chatter; anything already in the code or the
repository history; or sensitive personal data I did not ask you to keep. When in doubt, ask me.
```

We deliberately do **not** tell Claude to always use the project tools (`project_map`, `find_code`,
`read_file`): in real sessions, forcing them made things more expensive, not cheaper (see
[Honest numbers](#11-honest-numbers)). They are there for when they help.

### 5.5 Where your data lives

| Path | What it is | Safe to delete? |
|---|---|---|
| `$IHMT_HOME/ihmt_memory/` | **your long-term memory** | **No** — this is the memory itself |
| `$IHMT_HOME/ihmt_projects/` (or `$IHMT_PROJECTS_DIR`) | per-project code indexes used by `project_map`/`find_code` | Yes — they are caches and rebuild on the next call |
| session notes (`note`/`recall`) | kept in the server process | They vanish when the session ends |

**One memory or several?** Point every registration at the same `IHMT_HOME` and you have one memory
across all your projects. Give a project its own `IHMT_HOME` (project scope) and that project gets a
separate memory. You can combine both: if a user-scope and a project-scope registration both apply,
the project one wins, and the graphical interface tells you which one is in charge.

### 5.6 Using it with Codex (tested)

IHMT also works with **Codex**, OpenAI's coding agent — both the CLI and the Codex that now ships
inside the ChatGPT desktop app. Tested with `codex-cli 0.145`: saving and recalling across sessions,
`OUTDATED` and `AMBIGUOUS` handling, and searching/saving on its own when `AGENTS.md` tells it to.
Claude Code remains the reference client; the graphical interface only registers Claude Code.

**1. Find the `codex` command.** If `codex --version` works, skip this. On macOS, the ChatGPT app
bundles it at:

```bash
/Applications/ChatGPT.app/Contents/Resources/codex --version
```

Use that full path in the commands below, or install the Codex CLI on its own following OpenAI's
instructions.

**2. Install IHMT's MCP package** — [step 5.1](#51-install-the-mcp-package-in-a-virtual-environment),
if you have not already.

**3. Register the server.** From inside the `IHMT-MEMORY` folder:

```bash
codex mcp add ihmt-memory --env IHMT_HOME="$HOME/.ihmt" -- "$PWD/.venv/bin/python" "$PWD/mcp_server.py"
```

This writes to `~/.codex/config.toml` (all projects). As with Claude Code, `IHMT_HOME` is where the
memory lives: use an absolute path, and point it at the **same folder Claude Code uses** if you want
both agents to share one memory — what one saves, the other finds.

**4. Let Codex call the tools without asking every time.** Open `~/.codex/config.toml` and add
`default_tools_approval_mode = "approve"` to the server's **main** table, so the entry ends up like
this:

```toml
[mcp_servers.ihmt-memory]
command = "/absolute/path/to/IHMT-MEMORY/.venv/bin/python"
args = ["/absolute/path/to/IHMT-MEMORY/mcp_server.py"]
default_tools_approval_mode = "approve"

[mcp_servers.ihmt-memory.env]
IHMT_HOME = "/absolute/path/to/where/the/memory/lives"
```

> **Put the line above `[mcp_servers.ihmt-memory.env]`, not at the end of the file.** Appended at
> the end, it lands inside the `env` table and becomes an environment variable instead of a setting.

Without this line, the ChatGPT app asks for permission on every memory call, and `codex exec` (the
non-interactive mode) cancels the calls outright with `user cancelled MCP tool call`, because nobody
is there to approve them.

You can also skip `codex mcp add` and paste the whole block above into `config.toml` by hand.

**5. Check it.**

```bash
codex mcp list
```

```
Name         Command                          Args                             Env              Cwd  Status   Auth
ihmt-memory  /…/IHMT-MEMORY/.venv/bin/python  /…/IHMT-MEMORY/mcp_server.py     IHMT_HOME=*****  -    enabled  Unsupported
```

`enabled` is what matters; `Auth: Unsupported` is normal for a local server.

**6. Tell Codex when to use it.** Codex reads `AGENTS.md` instead of `CLAUDE.md`. Paste the
template from [section 5.4](#54-tell-claude-when-to-use-it-recommended) into `~/.codex/AGENTS.md`
(all projects) or into a project's `AGENTS.md`. It works unchanged. With it, Codex searches before
answering and saves durable facts on its own:

> **You:** Heads up: I've switched my default shell from zsh to fish this week.
> → Codex calls `search_memory`, then `save_memory`: *"Noted and saved: fish is now your default shell."*
>
> **You, in a new session:** Which shell do I use now?
> → Codex calls `search_memory`: *"Your default shell is fish."*

**Remove it:** `codex mcp remove ihmt-memory`.

### 5.7 Using it with opencode (tested)

IHMT also works with **[opencode](https://opencode.ai)**, the open-source terminal coding agent.
Tested with opencode 1.18 and two different models (Meta's Muse Spark and Google's Gemini Flash):
saving and recalling across sessions, `OUTDATED` and `AMBIGUOUS` handling, searching/saving on its own
when `AGENTS.md` tells it to, and sharing one memory with Claude Code.

**1. Download IHMT and install its MCP package.** Skip what you already did in sections 4 and 5.1.

```bash
git clone https://github.com/gonzaroman/IHMT-MEMORY.git
cd IHMT-MEMORY
python3 -m venv .venv
.venv/bin/pip install -r requirements-mcp.txt
```

Check that opencode is installed: `opencode --version`.

**2. Get the entry with your paths already filled in.** Still inside the `IHMT-MEMORY` folder, run:

```bash
printf '"ihmt-memory": {\n  "type": "local",\n  "command": ["%s/.venv/bin/python", "%s/mcp_server.py"],\n  "environment": { "IHMT_HOME": "%s" },\n  "enabled": true\n}\n' "$PWD" "$PWD" "$HOME/.ihmt"
```

It prints something like this, with your real folder instead of `/Users/you/IHMT-MEMORY`:

```
"ihmt-memory": {
  "type": "local",
  "command": ["/Users/you/IHMT-MEMORY/.venv/bin/python", "/Users/you/IHMT-MEMORY/mcp_server.py"],
  "environment": { "IHMT_HOME": "/Users/you/.ihmt" },
  "enabled": true
}
```

That `IHMT_HOME` keeps the memory in `~/.ihmt`, apart from the code. If you already use IHMT with
Claude Code or Codex, replace it with the same folder they use, so all your agents share one memory.

**3. Add it to opencode's config.** The global config is `~/.config/opencode/opencode.json` (or
`opencode.jsonc`). If you have never created it:

```bash
mkdir -p ~/.config/opencode
open -e ~/.config/opencode/opencode.json 2>/dev/null || nano ~/.config/opencode/opencode.json
```

Paste the entry inside an `"mcp"` block, so the whole file looks like this:

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "ihmt-memory": {
      "type": "local",
      "command": [
        "/absolute/path/to/IHMT-MEMORY/.venv/bin/python",
        "/absolute/path/to/IHMT-MEMORY/mcp_server.py"
      ],
      "environment": {
        "IHMT_HOME": "/absolute/path/to/where/the/memory/lives"
      },
      "enabled": true
    }
  }
}
```

- If the file already has content, add only the `"mcp"` block (or just the `"ihmt-memory"` entry
  inside an existing `"mcp"`), keeping the rest.
- `command` is a **list**: the venv's Python first, then `mcp_server.py`, both absolute.
- `environment` (not `env`) holds `IHMT_HOME`. Point it at the **same folder Claude Code (or Codex)
  uses** and they all share one memory.
- For a single project instead of all of them, put the same block in an `opencode.json` at that
  project's root.

No approval setting is needed: opencode lets MCP tools run by default. (If you have restricted tools
through opencode's `permission` settings, allow `ihmt-memory_*`.)

*(Windows, untested: the Python is `.venv\Scripts\python.exe`; inside the JSON, write paths with
`/` or with doubled backslashes, `\\`.)*

**4. Check it.**

```bash
opencode mcp list
```

```
┌  MCP Servers
│
●  ✓ ihmt-memory connected
│      /…/IHMT-MEMORY/.venv/bin/python /…/IHMT-MEMORY/mcp_server.py
│
└  1 server(s)
```

**5. Tell opencode when to use it.** opencode reads `AGENTS.md`: paste the template from
[section 5.4](#54-tell-claude-when-to-use-it-recommended) into `~/.config/opencode/AGENTS.md` (all
projects) or into a project's `AGENTS.md`. It works unchanged. With it, opencode searches and saves
on its own:

> **You:** Heads up: I've switched my default shell from zsh to fish this week.
> → opencode calls `ihmt-memory_search_memory`, then `ihmt-memory_save_memory`: *"Noted — I've saved
> fish as your default shell going forward."*
>
> **You, in a new session:** Which shell do I use now?
> → *"Per memory from 2026-09-30: switched default shell from zsh to fish. So you use fish now."*

In opencode the tools appear with the server name as a prefix: `ihmt-memory_search_memory`,
`ihmt-memory_save_memory`, and so on.

**6. Try it.** Two separate runs, so the second one can only know the answer from the memory:

```bash
opencode run "Remember this: my favourite code editor is Helix."
opencode run "What is my favourite code editor?"
```

The first run should call `ihmt-memory_save_memory`, and the second `ihmt-memory_search_memory` and
answer Helix. You can also do it inside the opencode interface: just type `opencode` and talk.

**Remove it:** delete the `"ihmt-memory"` entry from the config file.

---

## 6. Using it day to day with Claude Code

Once connected, you just talk. Claude decides when to search and when to save (Codex and opencode
behave the same way, with the same tools and outputs); you can also ask
explicitly ("remember that…", "what did we decide about…").

The server exposes **eight tools** in three families. Below, each one with a **real** output.

### 6.1 Long-term memory (lasts between sessions)

#### `save_memory(content, domain="general", content_type="auto")`

Classifies the text, splits it without breaking logical blocks, stores it, and extracts dated facts.
If the new text contradicts something saved earlier, it says so.

```
save_memory("2026-02-03: I live in Valencia, near the Turia gardens.")
```

```
Saved 1 memory entry (~13 tokens) as PERSONAL in domain 'personal' (detection confidence 0.66).
Memory now holds 2 entries across 1 domains (0 summary nodes, depth 0).
Extracted 1 dated fact(s) into the timeline.

This updates something remembered earlier — tell the user:
  In 2024 you said user location = 'Madrid', but in 2026 you updated to 'Valencia'.
  (the earlier value is kept as history, retrievable for questions about that period)
```

- Start the text with a date (`2026-02-03: …`) when the fact belongs to one — recency and the
  timeline depend on it. Without a date, the moment of saving is used.
- Leave `domain` and `content_type` at their defaults; detection is right almost always. Valid
  `content_type` values: `auto`, `code`, `narrative`, `clinical`, `personal`, `process`, `generic`.

#### `search_memory(query, clue=None, detail="compact")`

Walks the tree and returns the best memory with its date, plus one line per other match.

```
search_memory("testing framework decision unittest pytest")
```

```
[personal] 2026-03-02 (confidence 0.64)
2026-03-02: Decided to use unittest from the standard library instead of pytest, to keep the project free of dependencies.
```

**When something was updated later**, the result starts with `OUTDATED:` — Claude must tell you:

```
[personal] 2024-05-10 (confidence 0.97)
OUTDATED: In 2024 you said user location = 'Madrid', but in 2026 you updated to 'Valencia'.
2024-05-10: I live in Madrid, in the Lavapiés neighbourhood.

Also matching: [personal] 2026-02-03 · claude-code · 2026-02-03: I live in Valencia, near the Turia gardens.
```

**When the query is ambiguous**, it refuses to guess and returns `AMBIGUOUS` with the candidates:

```
search_memory("Luis")
```

```
AMBIGUOUS: "Luis" — 2 memories match this query equally well (confidence 0.62).
Do not pick one at random. Either ask the user which they mean, or call
search_memory again with `clue` set to a distinguishing detail (a place,
a date, a project, a person).

Competing memories:
  1. [personal] claude-code · 2025-07-22: Holidays in Benidorm with my cousin Luis and his kids.
     2025-07-22: Holidays in Benidorm with my cousin Luis and his kids.
  2. [personal] claude-code · 2025-11-30: Luis Marín reviewed the reserveStock pull request at…
     2025-11-30: Luis Marín reviewed the reserveStock pull request at work.
```

Claude then asks you, or retries with a clue:

```
search_memory("Luis", clue="Benidorm holidays")
```

```
[personal] 2025-07-22 (confidence 0.96)
2025-07-22: Holidays in Benidorm with my cousin Luis and his kids.

Also matching: [personal] 2025-11-30 · claude-code · 2025-11-30: Luis Marín reviewed the reserveStock pull request at work.
```

`detail="full"` adds ids, tree paths and file names — useful for debugging, costlier in tokens:

```
1 memory match(es) for "testing framework decision unittest pytest" (confidence 0.64; 1 branch files read, depth 0).

--- 1. [personal] 2026-03-02 · L-personal-0000-762a26012d
    source: layer_0/personal/L-personal-0000-762a26012d.txt
    path:   root > L-personal-0000-762a26012d

2026-03-02: Decided to use unittest from the standard library instead of pytest, to keep the project free of dependencies.
```

If nothing matches, it says so plainly instead of inventing a near miss.

### 6.2 Project tools (save tokens inside one session)

These index a codebase so Claude can look things up without reading whole files. The index is a
cache under `ihmt_projects/`, synced on every call by size, modification time and SHA-256: changed
files are re-indexed, removed ones dropped, and code is never served stale.

#### `project_map(path, detail="files", subpath="")`

A compact map of a project: files with their size in tokens; with `detail="symbols"`, each class and
function with its line range.

```
project_map("/path/to/IHMT-MEMORY", detail="symbols", subpath="ihmt_gui/folders.py")
```

```
IHMT-MEMORY: 54 files, ~1185 tokens if read whole (format: file ~tokens: symbol first-last line)
ihmt_gui/
  folders.py ~1185: DirectoryListing.to_dict 57-63, function home 64-68, function has_memory 69-73, function list_directories 74-112, function native_dialog_available 113-122, function pick_directory_native 123-152
```

#### `find_code(query, path, scope="main", subpath="", clue=None, max_tokens=1500)`

Returns only the symbol that answers the query, as `file:first-last` plus the code. `scope` is `main`
(skip tests, the default), `test` or `all`. Specific words — identifiers, distinctive terms — work
best.

```
find_code("_require_entry validate identifier against the catalog", "/path/to/IHMT-MEMORY", subpath="ihmt_gui")
```

```
ihmt_gui/handlers.py:191-202  GuiApi._require_entry
(in: class GuiApi: """Interface state: which memory and which project are active.""")

    @staticmethod
    def _require_entry(memory: IHMT, node_id: str) -> Any:
        """Validate an identifier against the catalog.
        …
```

#### `read_file(path, force=False)`

Reads a file and remembers what it handed out in this session. A second read of an unchanged file
answers in one line; a changed file returns only a unified diff.

```
UNCHANGED since your last read (8 lines). Use your copy; force=true if it left your context.
```

Use `force=True` when the earlier copy is no longer in the conversation (e.g. after a compaction).

### 6.3 Session tools (scratch memory for the current session)

#### `note(text)` / `recall(query, clue=None)`

Jot down what you find out ("the pricing rule is in Order.java:50-60") and get it back later — also
after the context has been compacted. Notes disappear when the session ends; use `save_memory` for
anything worth keeping.

```
note("The GUI tests take ~20 s because each one builds a store.")
→ Noted (~14 tokens). 1 session notes so far; use recall to find them.

recall("GUI tests slow")
→ [1] 2026-09-30 (confidence 0.80)
  The GUI tests take ~20 s because each one builds a store.
```

#### `digest_output(text, label="output")`

Condenses a long log (build, tests) to its first lines, errors, failures, totals and last lines. The
full text stays retrievable with `recall`.

```
unit tests: 304 lines (~1194 tokens) condensed to ~36.
test_0 ... ok
test_1 ... ok
test_2 ... ok
test_299 ... ok
FAIL: test_x (tests.T)
AssertionError: 1 != 2
Ran 301 tests in 9.1s
FAILED (failures=1)
```

### 6.4 What a normal day looks like

> **You:** I moved to Valencia, note it down.
> → Claude saves it with today's date and tells you it supersedes the "Madrid" from 2024.
>
> **You (three weeks later, in another project):** where do I live?
> → Claude searches before answering and says Valencia, with the date.
>
> **You:** what did we decide about the test framework?
> → Claude finds the 2026-03-02 decision and quotes the reason.

---

## 7. The graphical interface

```bash
python3 gui.py
```

Your browser opens on a local page with four screens. It installs nothing: the server comes from
Python's standard library and **listens only on your own machine** (`127.0.0.1`). The address carries
a random token; without it, any other program on your computer that knew the port could read your
memory. The interface is available in English and Spanish (switch at the top).

Options:

| Option | Meaning |
|---|---|
| `--path FOLDER` | open another memory (the folder that contains `ihmt_memory/`) |
| `--project FOLDER` | the project the "This project only" scope applies to (default: current folder) |
| `--port N` | fixed port (default: a free one) |
| `--no-browser` | just print the address; open it yourself |

### 7.1 Set up

It answers the two awkward questions: **where** to keep the memory and **from where** it can be used.

> **These are two different things, and almost all the confusion is here:**
>
> - **Where the memory lives** = the folder that will contain `ihmt_memory/`. Your memories.
> - **Where it can be used from** = the scope. Which folders Claude Code has it available in.
>
> They do not have to match. You can keep the memories in `~/memories/calendar` and use them from
> `~/projects/calendar`.

Pick the folder with the system dialog (or the page's own folder browser, when Tk is not installed),
and if there is no memory there yet, create it with one button. Then choose the scope:

- **All projects** — works in any folder on the machine, with one single memory. The usual choice.
- **This project only** — works only inside the folder you choose. A second field appears, **"Which
  project?"**, where you give that folder: the `.mcp.json` is written there. Useful for one memory
  per client, per course or per app, without mixing them.

Before touching anything, it shows **the exact command or file** it is going to write, with a copy
button in case you prefer doing it yourself. It only acts when you press *Apply*.

At the very top is the answer to the only question that matters — **does it work?** — as a coloured
panel with one sentence and, if something is missing, the exact steps:

```
⏸  One step left
   It is registered, but Claude Code has not approved it yet.
   Until you approve it, it cannot use the memory.

   Active in this project only   ·   The memory lives in /…/my-app

   What you need to do:
     1. Open a terminal
     2. Go to the folder:  cd /…/my-app                     [Copy]
     3. Run:  claude                                        [Copy]
     4. Approve it when asked, then come back here

   [Check again]     show technical detail
```

It distinguishes **registered** from **working**: a freshly registered project-scope server is
*pending approval* and does nothing until you approve it. It also shows which scope is **really** in
charge, warns if the MCP package or the `claude` command is missing, and notes when the folder has no
memory yet (it is created on first use). After approving in the terminal, *Check again* refreshes the
state without reloading the page.

### 7.2 Explore

The tree, expandable from domain to branch to leaf. Opening a leaf shows its text, date, tags and the
path that connects it to the root; if the information was updated later, the notice appears at the
top. This is how you check what Claude Code is really saving.

Each level loads only when expanded, and a branch never has more than `branch_factor` children (8 by
default), so lists never explode. The only list that could grow without limit is the domain list,
so **from 8 domains on, a filter appears**: type `medic` and only `medicine.clinical` remains. It
filters by hiding rows, not rebuilding the tree, so you keep what you had expanded.

### 7.3 Diagnose

A search that also tells you what it cost: the confidence, how many files it opened out of how many
exist, and the path it followed. Use it to understand why a query does not find something. You do not
need it day to day: you search by talking to Claude.

### 7.4 Timeline

Every dated fact, with the active value and the previous ones struck through, plus the detected
contradictions. Built for when a lot has piled up:

- **Paginated.** 50 groups at a time, with a *"Showing 50 of 122"* counter and a *Load more* button.
- **Filterable** by subject or attribute, ignoring case and accents. Instant, and it goes back to the
  first page as you type.
- **Long histories fold.** If an attribute changed many times, the two that matter stay visible — the
  **origin** and the **active** one — and the ones in between hide behind a link.

### 7.5 Recipes

**A separate memory for each project** (your calendar app, your Java course…):

```bash
python3 /path/to/IHMT-MEMORY/gui.py --path ~/projects/calendar --project ~/projects/calendar
```

In the interface: check the memory folder is `~/projects/calendar` and press *Create the memory
here*; choose **This project only**; make sure "Which project?" says `~/projects/calendar`; *Apply*.
Repeat with `~/courses/java` and you have two memories that never see each other.

**One memory for everything** (the most common):

```bash
python3 /path/to/IHMT-MEMORY/gui.py --path ~/ihmt-home
```

Create the memory there, choose **All projects** and *Apply*. From then on, whatever folder you work
in, Claude Code uses that same memory.

---

## 8. The command line (CLI)

For loading material in bulk or inspecting the memory by hand. All commands accept `--path FOLDER`
(which memory to use; default: the current folder) and `--json` (machine-readable output), before or
after the subcommand.

### Saving

```bash
python3 main.py ingest ~/notes ~/project/src/Main.java      # files or whole folders
echo "2026-09-10: we decided to use Go for the backend" | python3 main.py ingest -
python3 main.py ingest notes.txt --domain medicine --tag oncology
python3 main.py ingest big_folder/ --no-consolidate          # load now, build branches later
```

```
examples/journal_personal.txt
  → PERSONAL / personal (confidence 0.96)
  → 10 leaves, ~670 tokens, 6 facts
examples/InventoryService.java
  → CODE / software.java (confidence 0.94)
  → 1 leaves, ~1346 tokens, 0 facts

tree: 1 nodes, depth 1
```

### Searching

```bash
python3 main.py search "stock reservation soft hold"
python3 main.py search "socarrat paella" --full      # --full prints the whole leaf
python3 main.py search "..." --top-k 5
```

```
query: "stock reservation soft hold"  ·  confidence 0.85  ·  1 node reads, 1 leaf reads, depth 0

  1. [software.java] InventoryService.java · imports → class InventoryService
     score 0.432 · 2026-09-09 · L-software.java-0000-b1e7d152e5
     path  root → L-software.java-0000-b1e7d152e5
     file  layer_0/software.java/L-software.java-0000-b1e7d152e5.txt
     package com.acme.warehouse.inventory; import java.time.Instant; …
```

### The clue loop

```bash
python3 main.py ask "Luis"                     # interactive: asks you for a clue
python3 main.py ask "Luis" --clue "Benidorm"   # non-interactive
```

```
"Luis" is ambiguous (3 memories match this query equally well, confidence 0.62).
It could belong to any of these branches:
  1. [personal] journal_personal.txt · 2024-07-22 — 2024-07-22 Vacaciones en Benidorm con Luis, mi primo…
  2. [personal] journal_personal.txt · 2024-11-30 — 2024-11-30 Cierre de trimestre. He escrito la primera…
  3. [personal] journal_personal.txt · 2026-08-30 — 2026-08-30 Fin de semana en la playa de El Saler con Luis…
Give me a clue to narrow it down (e.g. a place, a date, a project):
>
```

### Looking inside

```bash
python3 main.py tree --depth 2     # the hierarchy
python3 main.py stats              # counters
python3 main.py facts              # current state: where you live, what stack you use…
python3 main.py conflicts          # what changed over time
```

```
$ python3 main.py conflicts
3 contradiction(s) detected:

  ⚠ In 2024 you said user location = 'Madrid', but in 2026 you updated to 'Valencia'.
     history: Madrid [2024-03-11, HISTORICAL] → Valencia [2026-02-03, ACTIVE]
  …
```

### Maintenance

```bash
python3 main.py consolidate --force   # close the tree up to the root now
python3 main.py rebuild               # rebuild catalog, timeline and trunk from the files on disk
```

### Full reference

| Command | Purpose |
|---|---|
| `init [--branch-factor N] [--target-tokens N] [--force]` | create the store |
| `ingest <paths…\|-> [--domain D] [--type T] [--tag X] [--no-consolidate]` | ingest files, folders or stdin |
| `consolidate [--force]` | run pending summarization events |
| `search <query> [--top-k N] [--full]` | walk the tree |
| `ask <query> [--clue TEXT] [--top-k N]` | search with the clue loop |
| `tree [--depth N]` | outline of the hierarchy |
| `stats`, `facts [--subject S]`, `conflicts [--subject S]` | inspection |
| `rebuild` | rebuild catalog, timeline and trunk from the files |
| `demo` | end-to-end walkthrough in `./demo_workspace` |

---

## 9. The Python API

To embed IHMT in your own programs.

```python
from ihmt import IHMT

memory = IHMT.initialize("./my_memory")     # or IHMT("./my_memory") if it already exists

# --- save ---
memory.ingest_file("examples/InventoryService.java")
memory.ingest_file("examples/journal_personal.txt")   # the diary: Luis, Madrid, Valencia…
memory.ingest_text(
    "2026-02-03: I live in Valencia, in the Ruzafa neighbourhood.",
    source="diary",
)
memory.flush()                               # close the tree up to the root

# --- search ---
r = memory.search("stock reservation with locking")
print(r.best.content)        # the stored text
print(r.best.timestamp)      # when it was true
print(r.best.path)           # ['root', 'N1-software.java-…', 'L-software.java-…']
print(r.node_reads)          # how many files had to be opened
print(r.best.notices)        # notices if it has been superseded

# --- ambiguity: the clue loop ---
r = memory.search("Luis")
if r.needs_clue:
    print(r.clue_request.prompt())            # shows the candidates
    r = memory.search("Luis", clue="Benidorm holidays")

# the automatic version: pass any function that returns the clue
r = memory.ask("Luis", lambda request: input(request.prompt() + "\n> "))

# --- timeline ---
memory.record_fact("user", "location", "Valencia", timestamp="2026-02-03T00:00:00Z")
memory.resolver.active_state()["user::location"].value          # 'Valencia'  (now)
memory.resolver.state_at("2024-12-31")["user::location"].value  # 'Madrid'    (back then)

for c in memory.conflicts():
    print(c.render_notice())
    # "In 2024 you said user location = 'Madrid', but in 2026 you updated to 'Valencia'."
```

Settings when creating a memory:

```python
memory = IHMT.initialize(
    "./my_memory",
    branch_factor=8,      # children per branch; with few documents, lower it to 4
    target_tokens=2000,   # target size of each leaf
    beam_width=3,         # how many branches are explored at once on the way down
)
```

---

## 10. Configuration

### Environment variables

| Variable | Used by | Meaning |
|---|---|---|
| `IHMT_HOME` | MCP server | folder that contains (or will contain) `ihmt_memory/`. Default: the repository folder. **Use an absolute path.** |
| `IHMT_PROJECTS_DIR` | MCP server | where the per-project code indexes go. Default: `$IHMT_HOME/ihmt_projects`. |
| `ANTHROPIC_API_KEY` | optional summarizer | only if you choose the `anthropic` summarization backend (below). |

### `ihmt_memory/ihmt.config.json`

| Key | Default | Meaning |
|---|---|---|
| `branch_factor` | 8 | children per branch; the log base of retrieval cost |
| `target_tokens` / `max_tokens` | 2000 / 3000 | leaf size target and oversize threshold |
| `beam_width` | 3 | branches kept alive per level while descending |
| `confidence_threshold` | 0.45 | below this, ask for a clue |
| `ambiguity_margin` | 0.18 | score gap under which candidates count as tied |
| `max_clue_rounds` | 3 | clue-loop iterations |
| `summarizer_backend` / `summarizer_model` | `heuristic` / `claude-sonnet-5` | how branch summaries are written |
| `code_chunk_mode` | `pack` | `symbol` stores one leaf per class member (used by project indexes) |

**When to touch `branch_factor`:** small corpora deserve a smaller one. The demo uses
`branch_factor=4, target_tokens=400` so that a handful of documents still builds a real multi-layer
tree. For a personal memory that grows note by note, the default is fine.

### Better summaries with a model (optional)

By default, branch summaries are written by an offline, deterministic extractive algorithm. You can
have Claude write them instead:

```bash
.venv/bin/pip install anthropic
export ANTHROPIC_API_KEY=...
python3 init_ihmt.py --backend anthropic      # model set by summarizer_model in ihmt.config.json
```

Every failure (missing package, missing key, network error, unparseable reply) falls back to the
offline summarizer, so a consolidation is never lost. This costs API tokens; it is entirely optional.

---

## 11. Honest numbers

### Retrieval cost versus pasting everything into the context

Measured with generated corpora, queried for real:

| Corpus | Tokens if pasted whole | Tokens IHMT returns | Files opened | Saving |
|---|---|---|---|---|
| The 6 files in `examples/` | 4,501 | 638 | 5 of 48 | **85.8 %** (×7) |
| 200 notes | 13,898 | 211 | 12 of 230 | **98.5 %** (×66) |
| 1,000 notes | 69,658 | 211 | 13 of 1,145 | **99.7 %** (×330) |
| 5,000 notes | 231,637 | 138 | 17 of 5,717 | **99.9 %** (×1,678) |

The number that really matters is not the percentage, it is this:

```
   200 notes  →  3 layers  →   9 branches opened
 1,000 notes  →  4 layers  →  10 branches opened
 5,000 notes  →  5 layers  →  14 branches opened

 the corpus grows 25× … and the search cost goes from 9 to 14 files
```

**The cost of retrieval does not grow with what you remember.** Tree depth grows logarithmically, and
going down one more layer costs a few more files. A `search_memory` answer takes roughly
**200–900 tokens** whether you have 50 memories or 50,000. A query takes about **13 ms**.

### Project tools versus reading files

On a 55-file Spring Boot project, 8 typical questions: reading the files that hold the answers costs
3,613 tokens; `find_code` returns the exact method for all 8 in 1,071. The project map costs 660
tokens against 13,157 to read the project whole. Re-reading 8 unchanged files with `read_file` costs
184 tokens against 3,963.

### …and what happened in real sessions

Those project-tool numbers compare against an agent that **reads whole files**. Real agents do not
always do that. In an A/B experiment with 22 headless Claude Code sessions (four tasks: a Java/Spring
feature, documenting a Python library with citations, an HTML guide, a small JS app):

- With IHMT available, Claude **did not call the project tools at all** — it preferred batched
  `grep` and `sed -n`, which are already cheap.
- **Forcing** their use (blocking the built-in file tools) made sessions **42–71 % more expensive**:
  4–5× more tool calls and more output.
- Having the MCP server enabled but unused costs only **about 460 tokens per conversation**, because
  Claude Code loads MCP tools on demand.
- All sessions passed the quality check.

**Conclusion:** IHMT's main value is **memory between sessions** — Claude remembering your setup,
decisions and corrections without you repeating them. The project tools are an option, not something
to impose.

### Other caveats

- With 10 notes, IHMT saves nothing: they fit in the context anyway. The value appears when the
  memory exceeds what fits, or when you would pay to resend it every turn.
- If today you paste nothing (and therefore repeat yourself), what you gain is not tokens: it is that
  the model finally remembers.
- Token counts are estimates by characters (~4 characters = 1 token): consistent, not exact.
- **Writing scales worse than reading.** When ingesting, the catalog is rewritten for every leaf, so
  bulk loads slow down: 200 notes consolidate in 0.4 s, 1,000 in 4.7 s, 5,000 in 107 s. Saving notes
  one at a time (normal use, and what the MCP server does) is instant.

---

## 12. Maintenance, troubleshooting and FAQ

### 12.1 Maintenance

**Back up your memory.** It is plain files: copy the folder.

```bash
cp -R "$IHMT_HOME/ihmt_memory" ~/backups/ihmt_memory-$(date +%F)
```

**Update IHMT.**

```bash
cd /path/to/IHMT-MEMORY
git pull
.venv/bin/pip install -r requirements-mcp.txt    # in case the requirements changed
.venv/bin/python -m unittest discover
```

Your memory is not in git, so `git pull` never touches it. Restart Claude Code (or reconnect with
`/mcp`) to load the new server.

**Edited files by hand, or something looks inconsistent?**

```bash
python3 main.py --path "$IHMT_HOME" rebuild               # rebuild catalog, timeline and trunk
python3 main.py --path "$IHMT_HOME" consolidate --force   # close the tree up to the root
```

**Moved the IHMT-MEMORY folder?** The registration holds absolute paths. Remove and add again, from
the new location:

```bash
claude mcp remove ihmt-memory -s user
claude mcp add ihmt-memory -s user -e IHMT_HOME="$HOME/.ihmt" -- "$PWD/.venv/bin/python" "$PWD/mcp_server.py"
```

**Move your memory to another computer.** The memory is one self-contained folder: every path inside
it is relative, so it works wherever you put it.

1. On the old computer, copy the folder — `IHMT_HOME`, e.g. `~/.ihmt` — to the new one (USB drive,
   `rsync -a ~/.ihmt/ newmachine:~/.ihmt/`, a cloud drive…).
2. On the new computer, install IHMT (ask your agent, or sections 4–5) with `IHMT_HOME` pointing at
   the copied folder. The installer reuses an existing memory; it never overwrites one.
3. Optional check: `python3 ~/IHMT-MEMORY/main.py --path ~/.ihmt stats` shows the same counts as on
   the old computer. If anything looks off, `python3 ~/IHMT-MEMORY/main.py --path ~/.ihmt rebuild`.

Do not use the **same** copy from two computers at once (for example, a synced folder open on both):
IHMT assumes a single writer. Copy it, or move it, but keep one active copy.

**Uninstall.**

```bash
claude mcp remove ihmt-memory -s user     # or delete the entry from the project's .mcp.json
rm -rf /path/to/IHMT-MEMORY               # the code; your memory in ~/.ihmt stays unless you delete it too
```

### 12.2 Troubleshooting

**`claude mcp list` says `⏸ Pending approval`.**
It is a project-scope registration (`.mcp.json`). Run `claude` inside that project and approve it. If
the approval does not stick, use the user scope instead: it needs no approval.

**`✘ Failed to connect`.**
Run the exact command yourself and read the error:

```bash
IHMT_HOME=/your/ihmt/home /path/to/IHMT-MEMORY/.venv/bin/python /path/to/IHMT-MEMORY/mcp_server.py
```

It should print `ihmt-mcp: store ready at …` and then wait silently for input (stop it with Ctrl+C).
If it prints an error instead, it is one of the cases below.

**`ModuleNotFoundError: No module named 'mcp'`.**
The server is being started with a Python that does not have the SDK — usually the system `python3`
instead of `.venv/bin/python`. Check the registered command with `claude mcp get ihmt-memory`; it must
point at the venv's Python. If the venv does not exist, repeat [step 5.1](#51-install-the-mcp-package-in-a-virtual-environment).

**The IHMT tools do not show up in my session.**
MCP servers load when a session starts. After registering or updating, start a new session or
reconnect with `/mcp`. Also remember that tools may be loaded on demand (deferred), so not seeing them
listed does not mean they are unavailable — `/mcp` is the reliable check.

**Codex: `user cancelled MCP tool call`.**
The server works, but Codex was not allowed to call its tools. Add
`default_tools_approval_mode = "approve"` to the `[mcp_servers.ihmt-memory]` table — above its `env`
table (step 4 of [section 5.6](#56-using-it-with-codex-tested)).

**The memory ended up in an unexpected folder.**
`IHMT_HOME` was relative or missing. Always use an absolute path; when it is missing, the server uses
the repository folder.

**`SyntaxError` or odd errors on start.**
Python is older than 3.10. Check `python3 --version` and create the venv with a newer one, e.g.
`python3.12 -m venv .venv`.

**Search does not find something I saved.**
IHMT matches words, not meanings. Search with the words you used when saving, add more distinctive
terms, and use the *Diagnose* screen of the interface to see the path it followed.

### 12.3 FAQ

**Can I read and edit the files by hand?**
Yes: `.txt` and `.json`. After editing, run `python3 main.py rebuild`.

**Is anything lost when a fact is updated?**
No. Nothing is ever deleted. The new value becomes active and the old one historical, with the period
when it was true.

**Do I need internet or a paid API?**
No. Everything runs locally. The optional model summarizer is the only thing that uses an API.

**Does my memory get uploaded anywhere?**
No. `ihmt_memory/` and `ihmt_projects/` are in `.gitignore`, and the only network access in IHMT is the
optional model summarizer, which is off unless you enable it.

**How big does it get?**
Small: it is text. 1,000 notes are about 1,145 small files.

**Known limitations, plainly:**
- Search by words, not meaning (no synonyms).
- Automatic fact extraction relies on patterns ("I live in X", "Diagnosis: Y") and can misread
  unusual phrasing — e.g. "I live in Valencia now" may be recorded as the value "Valencia now". For
  what matters, the Python API's `record_fact()` is the reliable path.
- One writer at a time.
- Bulk ingestion of thousands of documents slows down (see [Honest numbers](#11-honest-numbers)).

---

## 13. Roadmap

- **Other agents.** ~~Codex~~ and ~~opencode~~ (done, sections [5.6](#56-using-it-with-codex-tested)
  and [5.7](#57-using-it-with-opencode-tested)). Next: test the configurations `INSTALL.md` already
  describes for GitHub Copilot, Antigravity, Cursor, Windsurf, Gemini CLI and Claude Desktop; Codex
  and opencode support in the graphical interface; and possibly an
  HTTP transport for clients that only talk to remote servers (such as ChatGPT's web connectors).
- **Batched project tools**: several ranges or queries per `read_file` / `find_code` call, full
  relative paths in headers, shorter default answers.
- **Prefer the current version of a fact**: "where do I live?" currently returns `AMBIGUOUS`
  between an old address and the new one instead of answering with the active one.
- **Faster bulk ingestion**: batch catalog writes during ingest, as the project index already does.

---

## 14. Cheat sheet

```bash
# install
# easiest: ask your agent — "Install the IHMT memory MCP server for me from
#   https://github.com/gonzaroman/IHMT-MEMORY — follow the instructions in its INSTALL.md."
git clone https://github.com/gonzaroman/IHMT-MEMORY.git ~/IHMT-MEMORY && cd ~/IHMT-MEMORY
python3 -m venv .venv && .venv/bin/pip install -r requirements-mcp.txt
claude mcp add ihmt-memory -s user -e IHMT_HOME="$HOME/.ihmt" -- "$PWD/.venv/bin/python" "$PWD/mcp_server.py"
claude mcp list                              # → ✔ Connected
# …or with Codex (then add default_tools_approval_mode = "approve", see §5.6)
codex mcp add ihmt-memory --env IHMT_HOME="$HOME/.ihmt" -- "$PWD/.venv/bin/python" "$PWD/mcp_server.py"
# …or with opencode: add the "mcp" block to ~/.config/opencode/opencode.json (see §5.7)
opencode mcp list                            # → ✓ ihmt-memory connected

# start
python3 gui.py                               # graphical interface (set up and explore)
python3 init_ihmt.py                         # create the memory from the terminal
python3 main.py demo                         # see everything working

# use
python3 main.py ingest FILE_OR_FOLDER        # save
python3 main.py search "anything"            # search
python3 main.py ask "ambiguous name"         # search with clues
python3 main.py conflicts                    # what changed over time

# inspect and maintain
python3 main.py tree
python3 main.py stats
python3 main.py consolidate --force
python3 main.py rebuild

# check everything is still fine
.venv/bin/python -m unittest discover
```
