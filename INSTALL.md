# Installing IHMT — instructions for AI agents

> **Human?** You do not need to read this. Paste this into the AI agent you want to give a memory to
> (Claude Code, Codex, opencode, GitHub Copilot, Antigravity, Cursor…) and let it do the work:
>
> ```
> Install the IHMT memory MCP server for me from https://github.com/gonzaroman/IHMT-MEMORY — follow the instructions in its INSTALL.md.
> ```
>
> It connects **that** agent. To use the same memory from another agent later, paste the same prompt
> there: it reuses the existing installation and memory. Prefer doing it by hand? See
> [GUIDE.md](GUIDE.md), sections 4 and 5.

---

**Agent: this file is for you.** Follow the steps in order. Run the commands yourself; do not ask the
user to run them unless a step says so. Keep the user informed with short progress lines, and finish
with the summary in step 8.

IHMT is a long-term memory MCP server (Python, stdio). Installing it means: find or get the code,
make sure its virtual environment has the MCP SDK, register the server with **the agent you are**,
and add usage instructions to your instruction file.

## 0. Rules

1. **Configure only the agent you are.** Never register IHMT with, or edit the files of, any other
   agent unless the user explicitly asks for it — and do not offer to either: the user adds another
   agent by pasting this same prompt there.
2. **Always use absolute paths** in every configuration you write. Expand `~` and `$HOME` yourself.
3. **Reuse an existing installation.** If IHMT is already installed (step 2), use its code folder
   and, above all, **its memory**. Pointing a user who already has memories at a new, empty folder
   would make them "forget" everything.
4. **Never delete, move or overwrite a memory.** A memory is any folder that contains
   `ihmt_memory/root.json`.
5. **Back up every configuration file before editing it**: copy it next to itself with the suffix
   `.bak-ihmt`.
6. **Idempotent:** if something is already done correctly, leave it as it is and say so.
7. If a required step fails and the fix is not obvious, **stop and explain** what failed, with the
   exact error. Do not improvise workarounds, and do not write your own test scripts against
   `mcp_server.py` — step 7 covers testing.

## 1. Requirements

```bash
python3 --version   # must be 3.10 or newer
git --version
```

If Python is older than 3.10 or missing, stop and tell the user to install Python 3.10+ (on macOS:
`brew install python`). If `git` is missing, stop and tell them to install it.

## 2. Look for an existing installation

IHMT may already be installed for another agent. Run this **read-only** check. It looks at the MCP
configuration of every supported agent and prints each IHMT registration it finds:

```bash
python3 - <<'PY'
import pathlib, re
home = pathlib.Path.home()
configs = [
    ".claude.json",                                                   # Claude Code
    ".codex/config.toml",                                             # Codex
    ".config/opencode/opencode.json", ".config/opencode/opencode.jsonc",
    "Library/Application Support/Code/User/mcp.json",                 # VS Code (macOS)
    ".config/Code/User/mcp.json", "AppData/Roaming/Code/User/mcp.json",
    ".copilot/mcp-config.json",                                       # Copilot
    "Library/Application Support/Claude/claude_desktop_config.json",  # Claude Desktop
    "AppData/Roaming/Claude/claude_desktop_config.json",
    ".cursor/mcp.json",                                               # Cursor
    ".codeium/windsurf/mcp_config.json",                              # Windsurf
    ".gemini/settings.json",                                          # Gemini CLI
    ".gemini/config/mcp_config.json",                                 # Antigravity
]
found = False
for rel in configs:
    path = home / rel
    if not path.is_file():
        continue
    text = path.read_text(errors="ignore")
    servers = {s.replace("\\\\", "\\") for s in re.findall(r"""["']([^"']*mcp_server\.py)["']""", text)}
    servers = {s for s in servers if (pathlib.Path(s).parent / "ihmt" / "api.py").is_file()}
    if not servers:
        continue
    homes = set(re.findall(r"""IHMT_HOME["']?\s*[:=]\s*["']([^"']+)["']""", text))
    found = True
    print(f"{path}\n  REPO:      {sorted(str(pathlib.Path(s).parent) for s in servers)}\n  IHMT_HOME: {sorted(homes) or ['(not set: defaults to REPO)']}")
if not found:
    print("No existing IHMT installation found.")
PY
```

Decide:

- **It found an installation** (one `REPO` and one `IHMT_HOME`, possibly listed by several agents):
  that is the installation to reuse. `<REPO>` is that folder and `<HOME_DIR>` is that `IHMT_HOME`.
  Go to step 3, "Existing installation".
- **It found several different `REPO`s or `IHMT_HOME`s:** list them for the user and ask which one
  to use. Do not pick one yourself.
- **It found nothing,** but `~/IHMT-MEMORY/mcp_server.py` exists: reuse that folder as `<REPO>`. If
  `~/.ihmt/ihmt_memory/root.json` exists, that is `<HOME_DIR>`.
- **Nothing at all:** it is a new installation. Go to step 3, "New installation".

## 3. Get or update the code

### Existing installation

Check that `<REPO>/mcp_server.py` exists. If it does not (the folder was moved or deleted), tell the
user, and continue with "New installation", but **keep `<HOME_DIR>`** if
`<HOME_DIR>/ihmt_memory/root.json` still exists.

Otherwise, update it **only if it is safe**:

```bash
git -C <REPO> remote get-url origin     # must be https://github.com/gonzaroman/IHMT-MEMORY(.git)
git -C <REPO> status --porcelain        # must print nothing (no local changes)
```

- **Both conditions hold:** `git -C <REPO> pull --ff-only`, then reinstall the requirements (below).
- **Otherwise** (not a git clone, another origin, or local changes): **do not touch the code.**
  Tell the user it was not updated and why.

Then make sure the virtual environment works:

```bash
<REPO>/.venv/bin/python -c "import mcp; print('mcp SDK OK')"
```

If that fails, (re)create it as in "New installation". Then go to step 4.

### New installation

Clone the code **straight into `~/IHMT-MEMORY`** — give `git clone` the target folder, do not clone
into the current directory:

```bash
git clone https://github.com/gonzaroman/IHMT-MEMORY.git ~/IHMT-MEMORY
```

`<REPO>` is its absolute path. Then:

```bash
cd <REPO>
python3 -m venv .venv
.venv/bin/pip install -r requirements-mcp.txt
.venv/bin/python -c "import mcp; print('mcp SDK OK')"
```

*Windows (untested):* `py -m venv .venv`, `.venv\Scripts\pip install -r requirements-mcp.txt`, and
the interpreter is `<REPO>\.venv\Scripts\python.exe`.

From here on:

- `<PYTHON>` = `<REPO>/.venv/bin/python` (absolute)
- `<SERVER>` = `<REPO>/mcp_server.py` (absolute)

## 4. Where the memory lives (`IHMT_HOME`)

- **Existing installation:** `<HOME_DIR>` is the one found in step 2. Do not change it.
- **New installation:** use **`~/.ihmt`** (absolute, e.g. `/Users/alice/.ihmt`), unless the user
  asked for another folder. It keeps the memory apart from the code, so updating or reinstalling IHMT
  never touches it. Create it with `mkdir -p`; the server creates `ihmt_memory/` inside on first use.

## 5. Register the server with the agent you are

Use the section for **your** agent only. If your configuration already has an `ihmt-memory` entry with
exactly these values, leave it as it is.

The three values are always the same: run `<PYTHON>` with the argument `<SERVER>`, with the
environment variable `IHMT_HOME=<HOME_DIR>`. Name the server `ihmt-memory`.

### Claude Code (tested)

```bash
claude mcp get ihmt-memory >/dev/null 2>&1 && claude mcp remove ihmt-memory -s user
claude mcp add ihmt-memory -s user -e IHMT_HOME="<HOME_DIR>" -- "<PYTHON>" "<SERVER>"
claude mcp list
```

- Only remove an existing entry if it points somewhere else.
- The server name **must come before `-e`**: `-e` accepts several values and would swallow the name.
- Expected in `claude mcp list`: `ihmt-memory: … - ✔ Connected`.

### Codex (tested)

The `codex` command may not be on the PATH. On macOS it ships inside the ChatGPT app at
`/Applications/ChatGPT.app/Contents/Resources/codex`; use that full path if needed.

```bash
codex mcp add ihmt-memory --env IHMT_HOME="<HOME_DIR>" -- "<PYTHON>" "<SERVER>"
```

Then back up `~/.codex/config.toml` and add `default_tools_approval_mode = "approve"` to the
server's **main** table: directly under `[mcp_servers.ihmt-memory]`, **above** the
`[mcp_servers.ihmt-memory.env]` table. It must not end up inside the `env` table:

```toml
[mcp_servers.ihmt-memory]
command = "<PYTHON>"
args = ["<SERVER>"]
default_tools_approval_mode = "approve"

[mcp_servers.ihmt-memory.env]
IHMT_HOME = "<HOME_DIR>"
```

Without this line, Codex cancels every memory call in non-interactive runs
(`user cancelled MCP tool call`). Verify with `codex mcp get ihmt-memory`, which must show
`default_tools_approval_mode: approve`.

### opencode (tested)

The global config is `~/.config/opencode/opencode.json` or `~/.config/opencode/opencode.jsonc`
(JSON with comments). Back it up, then **merge** this entry into its `"mcp"` object, creating
`"mcp"` if needed and **keeping everything else in the file exactly as it was**, comments included:

```json
"ihmt-memory": {
  "type": "local",
  "command": ["<PYTHON>", "<SERVER>"],
  "environment": { "IHMT_HOME": "<HOME_DIR>" },
  "enabled": true
}
```

If neither file exists, create `~/.config/opencode/opencode.json` containing
`{"$schema": "https://opencode.ai/config.json", "mcp": { …the entry above… }}`.

`command` is a list, and the variable goes under `environment` (not `env`). No approval setting is
needed. Verify with `opencode mcp list`: `✓ ihmt-memory connected`.

### Other agents (from their official docs, not tested by us yet)

Back up the file, then merge an `ihmt-memory` entry into the root key shown, keeping the rest of the
file intact. The entry is the same in all of them except where noted:

```json
"ihmt-memory": {
  "command": "<PYTHON>",
  "args": ["<SERVER>"],
  "env": { "IHMT_HOME": "<HOME_DIR>" }
}
```

| Agent | Config file (macOS / Linux) | Root key | Entry differences | Check |
|---|---|---|---|---|
| **GitHub Copilot in VS Code** | user `mcp.json`: `~/Library/Application Support/Code/User/mcp.json` (Linux `~/.config/Code/User/mcp.json`), or the command *MCP: Open User Configuration* | `"servers"` | add `"type": "stdio"` | *MCP: List Servers* |
| **Claude Desktop** | `~/Library/Application Support/Claude/claude_desktop_config.json` (Windows `%APPDATA%\Claude\claude_desktop_config.json`) | `"mcpServers"` | — | quit and reopen the app completely; the server appears under *Connectors* |
| **Cursor** | `~/.cursor/mcp.json` | `"mcpServers"` | add `"type": "stdio"` | Output panel → *MCP Logs* |
| **Windsurf** | `~/.codeium/windsurf/mcp_config.json` | `"mcpServers"` | — | Cascade panel → MCPs icon |
| **Gemini CLI** | `~/.gemini/settings.json` | `"mcpServers"` | optional `"trust": true` to skip a confirmation on every call | `gemini mcp list` |
| **Antigravity** | `~/.gemini/config/mcp_config.json` (or *Open MCP Config* in its MCP manager, to confirm the path in your version) | `"mcpServers"` | — | its MCP manager (`/mcp`) |

VS Code can also add it from the terminal:
`code --add-mcp '{"name":"ihmt-memory","type":"stdio","command":"<PYTHON>","args":["<SERVER>"],"env":{"IHMT_HOME":"<HOME_DIR>"}}'`.

### Any other agent

If your agent is not listed, look up **your own client's official MCP documentation** to find where
and how it registers a local (stdio) server, and register it with the three values above. If you
cannot find reliable documentation, **stop** and give the user these three values so they can add it
by hand, instead of guessing a file or a format.

## 6. Add the usage instructions

The server alone does not make an agent use it well. Add the contents of
[`templates/memory-instructions.md`](templates/memory-instructions.md) (in `<REPO>`) to **your**
global instruction file:

| Agent | Where the instructions go |
|---|---|
| Claude Code | `~/.claude/CLAUDE.md` |
| Codex | `~/.codex/AGENTS.md` |
| opencode | `~/.config/opencode/AGENTS.md` |
| GitHub Copilot | `~/.copilot/copilot-instructions.md` (user-level; untested) |
| Gemini CLI | `~/.gemini/GEMINI.md` |
| Antigravity | its global rules — probably `~/.gemini/GEMINI.md`; otherwise its *Rules* settings (untested) |
| Claude Desktop, Cursor, Windsurf | set in the app's settings (Claude Desktop: a project's or your profile's instructions; Cursor: *User Rules*; Windsurf: *Global Rules*). You cannot edit these files: tell the user where to paste the template. |

- If the file exists, back it up and **append** the template, separated by a blank line. Never
  replace the file's content. Create the file if it does not exist.
- **Skip this step** if the file already mentions `search_memory` or IHMT: the user already has
  instructions, and a second copy would duplicate or contradict them.

## 7. Check that it works

The tools usually appear only in a **new** session (or after reloading the MCP servers, or after
restarting a desktop app). In the current session, do not assume you can call them. If you can call
`search_memory` now, try `search_memory("IHMT installation test")`: on a new memory it says that
nothing is stored yet, which is correct; on a reused memory it may find something.

Run the test suite as a last sanity check (about 30 seconds):

```bash
cd <REPO> && .venv/bin/python -m unittest discover 2>&1 | tail -3     # expect: OK
```

## 8. Tell the user

Finish with a short summary that includes:

1. **What you did:** whether you reused an existing installation or installed a new one. If you
   reused one, say explicitly whether you updated its code, and if not, why (for example: "not
   updated: it is not a git clone of the repository"); where the code is (`<REPO>`) and where the memory lives
   (`<HOME_DIR>`); the files you changed and their `.bak-ihmt` backups.
2. **The one thing they must do now:** start a new session, reload the MCP servers, or restart the
   app, so the memory tools load.
3. **How to try it:** say "remember that my favourite editor is …", then in a new session ask
   "what is my favourite editor?". If you reused an existing memory, suggest asking about something
   they told another agent before.
4. **Other agents:** "To use this memory from another agent, paste the same prompt there; it will
   reuse this installation and memory."
5. **Their memory is portable:** it is the folder `<HOME_DIR>`. To take it to another computer, copy
   that folder and install IHMT there with the same `IHMT_HOME`. To back it up, copy the folder.
6. **Where to learn more:** `<REPO>/GUIDE.md`.
