# Record events: how an agent's turn reaches the record

One format, memory-lane's own, for whatever harness an agent runs in. A
runner translates its harness's output into these events at the edge and
posts them; memory-lane has one importer, for this format. No harness's file
layout or wire dialect reaches the record's code.

Status: **proposed**; the importer is memory-lane#166. Written by magent at Ethereum block 26,170,449
(2026-10-11), against memory-lane `1a93f19`, after Justin in the magent-self
Mood: *"read the files for metadata only, but ensure that the interface
itself is what we're logging."* Nothing here is built yet.

## Why

Today every agent turn is Claude Code's, all the way in:

| Path | What happens | Share of rows, 7 days to 2026-10-11 |
|---|---|---|
| Woken turns | `claude -p --output-format stream-json`; the runner posts its events; the server rewrites each as a Claude Code transcript line (`stream_line` in `conversations/views_runner.py`) and imports it with `import_line_from_claude_code_v2` | 12,119 |
| Terminal sessions | the watcher reads `~/.claude/projects/*.jsonl` and posts lines to `/api/ingest/` | 512 |
| People in a Mood | the web composer writes the row itself | 475 |

The stream endpoint refuses any harness but `claude-code`. A second harness
would mean a second server-side importer, and a third a third. With this
format, a harness costs one adapter in the runner, and an agent that isn't on
hunter at all can be invited by giving it a runner key: it posts events, and
no files are involved.

## Transport

`POST /api/moods/<slug>/stream/`, authenticated by a runner key
(`MOOD_RUNNER_KEYS`), which names the agent. The agent is the sender of
everything the turn says; the body cannot change that.

```json
{
  "format": "events/1",
  "harness": "claude-code 2.1.289",
  "session_id": "<uuid: the session this turn continues or starts>",
  "forked_from": "<uuid, optional: the session this one was forked from>",
  "events": [ ... ]
}
```

- `format` selects this importer. A body without it is today's Claude Code
  stream, until that path is retired.
- `harness` is what produced the events, kept as metadata (today's
  `client_version`). It changes nothing about how they are read.
- `forked_from`: a session forked from another goes on from the history it
  copied, so its rows continue that session's context heap.
- At most 500 events per post, in the order they happened. Post as they come.

## Events

Every event has:

| Field | |
|---|---|
| `type` | one of the types below |
| `id` | a UUID, stable for this event. Posting the same event twice changes nothing. Where the harness has its own ids, derive it from them (uuid5), so a later transcript of the same turn finds the same rows |
| `at` | ISO 8601 time it happened |
| `turn` | the turn's id, the same for every event of one turn |
| `helper` | optional: the id of the helper (subagent) whose event this is. Absent for the agent's own line of thought |

Types:

| Type | Fields | Stored as |
|---|---|---|
| `turn_started` | `model`, `effort`, `cwd`, `git_branch` (each optional) | not a row: stamps the turn's rows. This is what the transcript files are read for today |
| `user_message` | `text`, `from` (`mood-poller`, the default) | a message from the mood-poller; with `helper`, the agent's own brief to its helper. Any other `from` is refused: people's words reach the record through the Mood, where they typed them, and a runner can't put words in anyone's mouth |
| `assistant_message` | `text`, optional `model`, `usage` | a message from the agent |
| `reasoning` | `text`, optional `signature` | a thought. `text` may be empty: some models return only an opaque, vendor-signed block, and the signature is kept as it came |
| `tool_call` | `call_id`, `name`, `input` (object) | a tool use |
| `tool_result` | `call_id`, `output` (string or object), `is_error`, optional `images` (base64 with a MIME type) | a tool result. Images become stored media, as today |
| `compaction` | `trigger` (`manual` or `auto`), `tokens_before`, optional `tokens_after`, `summary` | a compacting action, and the summary as the continuation |
| `task_notice` | `task_id`, `status`, `summary`, optional `call_id` | the notice a background task leaves when it ends |
| `turn_ended` | `outcome` (`completed`, `failed`, `canceled`), `stop_reason`, `usage`, `cost_usd` (this turn's, not the session's running total), `duration_ms`, optional `error` | the turn-result row; stamps the turn's last message with its stop reason, so the Mood stops showing the agent at work |

`usage` is `{"input": n, "output": n, "cache_read": n, "cache_write": n}`,
any of them absent if the harness doesn't say.

Every string is redacted before anything else reads it, as every line is
today. An event of a type this list doesn't name is counted in the response
and otherwise ignored, so an adapter can send more than memory-lane reads yet.

The response is `{"imported": n, "skipped": n, "ignored": n, "errors": [...], "finished": bool}`.

## Adapters

An adapter lives with the runner (`poller/`), one per harness. It reads what
the harness emits and writes events. The vocabulary above is deliberately
close to [Paseo](https://github.com/getpaseo/paseo)'s normalised timeline
(`user_message`, `assistant_message`, `reasoning`, `tool_call`, `compaction`;
`turn_started`, `turn_completed`, `turn_failed`, `turn_canceled`), which
already maps Claude Code, Codex, OpenCode, Pi and ACP agents onto one shape.
Where they differ, it is because the record keeps something a display
doesn't need: ids stable across sources, per-message usage, signatures.

| From | Into |
|---|---|
| **Claude Code** stream-json | `system/init` → `turn_started`; each `assistant` content block → `assistant_message`, `reasoning` or `tool_call`; `user` `tool_result` blocks → `tool_result`; `system/compact_boundary` (its `compact_metadata`) and the replayed summary after it → `compaction`; `system/task_notification` → `task_notice`; `result` → `turn_ended` |
| **Pi** (`pi --mode json`, one run per wake; checked against a real run of Pi 1.1.0, 2026-10-11) | our turn is Pi's *agent run*, `agent_start` to `agent_settled` (Pi's own `turn_start`/`turn_end` mark each model response inside it); the session header (`type: session`, a UUID) is the session id, and `--session-id`/`--fork` start or fork one, as `claude -p --resume --fork-session` does; `message_end` is authoritative and `message_update` deltas are only for live display; `message_end` with role `user` → `user_message` (unlike `claude -p`, Pi echoes the prompt it was given); role `assistant`: each `text`, `thinking` (`thinkingSignature`, possibly `redacted`) and `toolCall` block → `assistant_message`, `reasoning`, `tool_call`, with the message's `model` and `usage`; role `toolResult` (or `tool_execution_end`) → `tool_result`, its `content` blocks joined, images as `images`; role `system` (the prompt and tools) → not recorded; `compaction_end` with a `result` → `compaction` (`summary`, `tokensBefore`, `estimatedTokensAfter`); `agent_settled` → `turn_ended`, with `cost_usd` the sum of the run's assistant messages' `usage.cost.total` and `stopReason` `stop` → `end_turn`, `length` → `max_tokens`. **Ids:** Pi's stream carries none for messages (its session file's entry ids are 8 hex digits and never on the wire), so the adapter derives each from the session id, the message's millisecond `timestamp` and the block's index |
| **ACP** agents | `session/update` notifications: `agent_message_chunk` → `assistant_message` (joined per message), `agent_thought_chunk` → `reasoning`, `tool_call` and `tool_call_update` (completed) → `tool_call` and `tool_result`; the prompt response's stop reason → `turn_ended` |

## Transcript files

A harness that writes files (Claude Code, Pi) can still be read, but only to
fill in. For a session a runner streamed, a transcript line that finds its
row adds what is missing (as `enrich` does today) and never creates a row or
changes a sender. For a session no runner streamed (someone at a terminal),
the watcher still writes it, filed as from a transcript, so that work isn't
lost. Moving that work behind an interface we own is the other way to close
it.

## Order of work

1. This document.
2. The importer for `events/1`, beside today's path, with tests that post the
   same turn both ways and get the same rows.
3. The Claude Code adapter in the runner; the runner posts `events/1`. Then
   `stream_line` and the `claude-code` stream path go.
4. The watcher fills in only, for streamed sessions.
5. A second harness, with no Claude Code anywhere in the path. Pi is the
   cheapest: one adapter reaches Anthropic, OpenAI and Google models.

## Open questions

- **Human turns typed into a harness.** In a terminal session a person's
  words reach the agent without passing through a Mood. A runner may not
  name a person as `from`, so such a session can't be streamed as it
  stands; the watcher still brings it in from its transcript. If it should
  be streamed, who the person is has to come from the runner's config,
  never from the body.
- **Helpers.** Claude Code's subagents arrive inline today (`isSidechain`).
  Pi and Paseo model them as separate sessions. `helper` covers the first;
  the second may want its own session id and a link back.
- **What "same rows" means across sources.** Stable ids need each adapter to
  derive them from the harness's own ids. A harness with none gets ids from
  the adapter, and its transcript (if any) can't be matched to them.
