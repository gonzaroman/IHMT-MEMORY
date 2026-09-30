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
