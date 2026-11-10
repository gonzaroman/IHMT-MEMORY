# Memory: use IHMT

**IHMT** (MCP tools `search_memory`, `save_memory`, `mark_outdated`) is my long-term memory, in every
project.

**Search before answering** whenever the answer depends on something not in this conversation: my
setup, past decisions and why, people and projects I name, how I like to work, "like last time". Use
several distinctive words, and pass `also=[...]` with the same question in another language or
wording. Not for general knowledge or what the code already says. If the notes have nothing, try
`scope="conversations"` (my past sessions) before saying you do not know — and never guess.

- `AMBIGUOUS`: do not pick one — ask me, or call again with `clue`.
- `OUTDATED:`: always tell me what changed; never treat the old version as current.
- `NOT FOUND`: say you do not have it; the entries it lists are stray matches.

**Save what lasts** with `save_memory`, once it is decided: decisions and why, my setup, how I want
you to work, people, projects, procedures, and above all corrections — pass `replaces` with a few
words describing the old memory ("RainDesktop version 1.3") so it is flagged OUTDATED; if several
candidates are listed, pick with `mark_outdated`. "Remember this" means `save_memory`.

Write entries that stand alone six months from now, and **start with the date** when the fact belongs
to one: "2026-09-10: prefers stdlib unittest to avoid adding dependencies".

**Never save** passwords, keys or tokens, passing chatter, what the repository already records, or
sensitive personal data I did not ask you to keep. When in doubt, ask me.
