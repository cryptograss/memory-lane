# The memory tools (magenta-memory)

How a thinking entity reads the record: the MCP server in this repository,
what each tool does, what it leaves out, and how to find your way from a
fragment to the conversation around it.

Written for an agent that has just woken with none of this in mind. Checked
against memory-lane `acd2553` at Ethereum block 26,121,841 (2026-10-04).
If you change a tool, change this file in the same pull request.

## Where it runs

| | |
|---|---|
| Server | `python manage.py run_mcp_server_v2 --port 8000`, in the memory-lane container on **maybelle** (maybelle-config `maybelle/ansible/maybelle.yml`) |
| Address | `https://mcp.maybelle.cryptograss.live` (HTTP transport). Inside the private network, `10.0.0.2:8000` |
| How a container gets it | `hunter/container_startup.py` runs `claude mcp add --scope user --transport http magenta-memory-v2 <url>`; `MCP_MEMORY_URL` overrides the address |
| Database | the same Postgres as the web app (`magenta_memory` on maybelle) |
| Code | tool schemas: `conversations/mcp/server.py` · handlers: `conversations/mcp/tools.py` · queries: `conversations/services/memory.py`, `conversations/services/bootstrap.py` |
| Shipping a change | merge, then deploy **maybelle**. The server must restart to offer a new tool; a client may need `/mcp` to reconnect |

`run_mcp_server` (without `_v2`) is an older, standalone server. Nothing
deployed runs it as of this writing.

## What the record is

Every message is one row: something a person said, something an agent said,
an agent's private reasoning (a *thought*), a tool call, or a tool's result.
Three groupings sit over the rows, and a message can be in all three:

- **Era**: a phase of the relationship.
- **Context heap**: where one context window filled up and was compacted.
- **Mood** (a *Motion* in the code): what a conversation is about. Most of the
  record predates Moods and belongs to none.

Two kinds of time are stored, and they differ: `timestamp` is when a thing was
said (absent for the earliest era), and `created_at` is when the row was
imported. For anything backfilled, `created_at` can be months later.

The whole record is public. Tool output is not stored by default
(`TOOL_RESULT_CONTENT_CHARS=0`), so a tool result usually reads "(output not
kept)". Known secret shapes are redacted on import (`[REDACTED]`).

## The tools

### Finding something

| Tool | What it gives | Notes |
|---|---|---|
| `search_messages` | Matching messages. Each hit: full id, sender, kind (`message`, `thought`, `tool_use`), Mood or session, and **the text around the match** | Default: ranked word search (Postgres full-text, stemmed). `exact: true`: the phrase as written, case-insensitive, **including inside tool calls**, newest first; use it for commands, code, identifiers. `sender` narrows to one participant. `limit` up to 100. Tool results are never searched |
| `get_message_by_id` | One message, raw | Takes a full id, its first 6+ characters, or a `#m-<id>` link. An ambiguous prefix lists the candidates |
| `get_message_context` | A message with the messages before and after it | Its **session**; for older messages with no session, its **context heap** (it says which). Thinking and tool calls are shown. `before`/`after` up to 100. The usual next step after a search |

### Reading a Mood

| Tool | What it gives | Notes |
|---|---|---|
| `list_moods` | Every Mood, most recently active first, with its description, size and who's in it | |
| `read_mood` | What people and agents said in one Mood, oldest first, each line with its `#m-` link | Newest `limit` turns (default 60, max 300), or `from` a message id or an ISO time. A time without an offset is UTC. Tool calls aren't shown. About 60,000 characters at most; the oldest are dropped first, and it says so |

### Catching up after waking

| Tool | What it gives | Notes |
|---|---|---|
| `bootstrap_memory` | Recent messages (about 10,000 characters, tool results left out), the latest compaction summary if it isn't among them, the Era 1 summary, and the most recent "reawaken and breathe" reflection | The first call after waking cold |
| `list_threads` | Distinct sessions, most recently active first, with who took part, working directory, branch and a title hint | Read before `get_recent_work` to see separate threads rather than one interleaved column |
| `get_recent_work` | The most recent messages, or one thread's (`thread_id` from `list_threads`) | Newest first by `created_at` |
| `get_latest_continuation` | The most recent compaction summary | |
| `get_era_summary` | The first 100 messages of an era | Default era: "Compacting Meta-Conversation (Era 1)" |

### Older tools, with limits worth knowing

| Tool | Limit |
|---|---|
| `get_messages_before` | Fetches up to `limit` (default 300) but **prints only the first 10**, each cut to 200 characters, with no ids |
| `get_context_heap` | Prints the first 20 of the heap's messages, cut to 150 characters, with no ids |
| `random_messages` | Ordered by `created_at` (import time), so "following" messages may not be what came next in conversation |

Prefer `get_message_context` to all three when you can.

## Recipes

**From a fragment to the conversation around it.**
`search_messages` (with `exact: true` for anything literal) → take the id
from the hit → `get_message_context` with that id, or its first 8
characters.

**From a short id someone gave you** (a redaction report, a log line, a link):
`get_message_context` with it directly.

**Catching up on a Mood you weren't woken in:** `read_mood` with its slug; to
start at a particular message, pass its id as `from`.

**What was I doing?** `list_threads`, then `get_recent_work` with one
`thread_id`.

## Known gaps

- Recall is not scoped by agent: every agent sees everyone's history, and
  `bootstrap_memory`'s reflection is looked up by `sender_id='magent'`.
  (memory-lane #75.)
- `get_recent_work` and `random_messages` order by import time, not by when
  things were said.
- The older tools in the last table print no ids, so their output can't be
  followed up.
