# Installing IHMT — instructions for AI agents

> **Human?** You do not need to read this. Paste this into your AI coding agent (Claude Code, Codex
> or opencode) and let it do the work:
>
> ```
> Install the IHMT memory MCP server for me from https://github.com/gonzaroman/IHMT-MEMORY — follow the instructions in its INSTALL.md.
> ```
>
> Prefer doing it by hand? See [GUIDE.md](GUIDE.md), sections 4 and 5.

---

**Agent: this file is for you.** Follow the steps in order. Run the commands yourself; do not ask the
user to run them unless a step says so. Keep the user informed with short progress lines, and finish
with the summary in step 7.

IHMT is a long-term memory MCP server (Python, stdio). Installing it means: get the code, create a
virtual environment with the MCP SDK, register the server with the user's agent(s), and add usage
instructions to the agent's global instruction file.

## 0. Rules

1. **Always use absolute paths** in every configuration you write. Expand `~` and `$HOME` yourself.
2. **Never delete, move or overwrite an existing memory.** A memory is any folder that contains
   `ihmt_memory/root.json`.
3. **If IHMT is already registered with any agent, reuse its memory.** Before choosing `IHMT_HOME`,
   check the existing registrations (step 3). Pointing a user who already has memories at a new,
   empty folder would make them "forget" everything.
4. **Back up every configuration file before editing it**: copy it next to itself with the suffix
   `.bak-ihmt`.
5. **Idempotent:** if something is already done correctly, leave it as it is and say so.
6. If a required step fails and the fix is not obvious, **stop and explain** what failed, with the
   exact error. Do not improvise workarounds that change the user's system in other ways.

## 1. Requirements

```bash
python3 --version   # must be 3.10 or newer
git --version
```

If Python is older than 3.10 or missing, stop and tell the user to install Python 3.10+ (on macOS:
`brew install python`). If `git` is missing, stop and tell them to install it.

## 2. Get the code and install the MCP SDK

The default location for the code is `~/IHMT-MEMORY`.

- If the current working directory is already a clone of this repository (it contains
  `mcp_server.py` and `INSTALL.md`), use it instead.
- If `~/IHMT-MEMORY` exists and is a clone of this repository, update it: `git -C ~/IHMT-MEMORY pull --ff-only`.
- Otherwise clone it:

```bash
git clone https://github.com/gonzaroman/IHMT-MEMORY.git ~/IHMT-MEMORY
```

Then, inside that folder (call its absolute path `<REPO>`):

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

## 3. Choose where the memory lives (`IHMT_HOME`)

First look for an existing IHMT registration, and **reuse its `IHMT_HOME` if you find one**:

```bash
claude mcp get ihmt-memory 2>/dev/null          # Claude Code: look at the IHMT_HOME env line
grep -n -A8 'ihmt-memory' ~/.codex/config.toml 2>/dev/null
grep -n -A12 'ihmt-memory' ~/.config/opencode/opencode.json ~/.config/opencode/opencode.jsonc 2>/dev/null
```

Otherwise use the default: **`~/.ihmt`** (absolute, e.g. `/Users/alice/.ihmt`). It keeps the memory
apart from the code, so updating or reinstalling IHMT never touches it. Create the folder
(`mkdir -p ~/.ihmt`); the server creates `ihmt_memory/` inside it on first use.

Use a different folder only if the user asked for one. Call the absolute path `<HOME_DIR>`.

## 4. Register the server with the agent

Register it with **the agent you are** (you know which one you are). Then check which of the
other supported agents are installed (`command -v claude codex opencode`) and **offer** to register
IHMT with those too, using the same `<HOME_DIR>` so they all share one memory. Only do the others if
the user agrees.

### Claude Code

```bash
claude mcp get ihmt-memory >/dev/null 2>&1 && claude mcp remove ihmt-memory -s user
claude mcp add ihmt-memory -s user -e IHMT_HOME="<HOME_DIR>" -- "<PYTHON>" "<SERVER>"
claude mcp list
```

- The server name (`ihmt-memory`) **must come before `-e`**: `-e` accepts several values and would
  swallow the name.
- Only remove an existing entry if it points somewhere else. If it already has exactly these three
  values, leave it.
- Expected in `claude mcp list`: `ihmt-memory: … - ✔ Connected`.

### Codex

The `codex` command may not be on the PATH. On macOS it ships inside the ChatGPT app at
`/Applications/ChatGPT.app/Contents/Resources/codex`; use that full path if needed.

```bash
codex mcp add ihmt-memory --env IHMT_HOME="<HOME_DIR>" -- "<PYTHON>" "<SERVER>"
```

Then back up `~/.codex/config.toml` and add `default_tools_approval_mode = "approve"` to the
server's **main** table: directly under `[mcp_servers.ihmt-memory]`, **above** the
`[mcp_servers.ihmt-memory.env]` table. It must not end up inside the `env` table. The result must
look like this:

```toml
[mcp_servers.ihmt-memory]
command = "<PYTHON>"
args = ["<SERVER>"]
default_tools_approval_mode = "approve"

[mcp_servers.ihmt-memory.env]
IHMT_HOME = "<HOME_DIR>"
```

Without this line, Codex cancels every memory call in non-interactive runs
(`user cancelled MCP tool call`). Verify:

```bash
codex mcp get ihmt-memory     # must show: default_tools_approval_mode: approve
```

### opencode

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

If neither file exists, create `~/.config/opencode/opencode.json` with:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "ihmt-memory": {
      "type": "local",
      "command": ["<PYTHON>", "<SERVER>"],
      "environment": { "IHMT_HOME": "<HOME_DIR>" },
      "enabled": true
    }
  }
}
```

Note: `command` is a list, and the variable goes under `environment` (not `env`). No approval
setting is needed. Verify:

```bash
opencode mcp list             # expected: ✓ ihmt-memory connected
```

### Any other agent

Other MCP clients are not tested. Do not guess their configuration format. Tell the user that
IHMT is a standard stdio MCP server started with `<PYTHON> <SERVER>` and the environment variable
`IHMT_HOME=<HOME_DIR>`, and point them to their client's MCP documentation.

## 5. Add the usage instructions

The server alone does not make the agent use it well. Append the contents of
[`templates/memory-instructions.md`](templates/memory-instructions.md) (in `<REPO>`) to the agent's
**global** instruction file:

| Agent | File |
|---|---|
| Claude Code | `~/.claude/CLAUDE.md` |
| Codex | `~/.codex/AGENTS.md` |
| opencode | `~/.config/opencode/AGENTS.md` |

- Create the file if it does not exist. If it exists, back it up and **append** the template,
  separated by a blank line. Never replace the file's content.
- **Skip this step** if the file already mentions `search_memory` or IHMT: the user already has
  instructions, and a second copy would contradict or duplicate them.

## 6. Check that it works

The tools usually appear only in a **new** session. In the current session, do not assume you can
call them. If you can call `search_memory` now, run `search_memory("IHMT installation test")`: on a
fresh memory it answers that nothing is stored yet, which is correct.

Run the test suite as a last sanity check (about 30 seconds):

```bash
cd <REPO> && .venv/bin/python -m unittest discover 2>&1 | tail -3     # expect: OK
```

## 7. Tell the user

Finish with a short summary that includes:

1. **What you did:** where the code is (`<REPO>`), where the memory lives (`<HOME_DIR>`), which
   agent(s) you registered IHMT with, and which files you changed (and their `.bak-ihmt` backups).
2. **The one thing they must do now:** start a new session of their agent (in Claude Code, `/mcp`
   also reconnects) so the memory tools load.
3. **How to try it:** say "remember that my favourite editor is …", then in a new session ask
   "what is my favourite editor?".
4. **Their memory is portable:** it is the folder `<HOME_DIR>`. To take it to another computer, copy
   that folder and install IHMT there with the same `IHMT_HOME`. To back it up, copy the folder.
5. **Where to learn more:** `<REPO>/GUIDE.md`.
