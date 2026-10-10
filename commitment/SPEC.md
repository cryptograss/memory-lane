# Memory commitment, version 1

`cryptograss-memory-commitment/1`

This describes how the record kept in memory-lane (the conversations between
the people of cryptograss and the thinking entity called magent, and the
structure around them) is reduced to a few 32-byte values that can be written
to a blockchain, and how anyone holding any surviving part of the record can
check that part against those values.

It is written to be followed without this repository. Everything needed is
here, in `verify_memory.py` (Python standard library only), and in two
published hash functions.

## What is committed

A **leaf** is one row of the record. Each leaf has:

- a **key**, a string `<table>/<primary key>`, for example
  `message/0199c3a1-5c2e-7d41-9f0a-3b8e2c6d1a77`;
- a **record**, a line of JSON text describing the row;
- a **salt**, 32 bytes.

The tables, and the name each is given in a key:

| key prefix | what it is |
|---|---|
| `message` | one thing said, thought, or done: a person's words, the agent's words, its private reasoning, a tool call, a tool's result |
| `raw` | the line as it was first imported, before parsing |
| `heap` | a context heap: one context window's worth of conversation |
| `era` | a phase of the relationship |
| `compacting_action` | the moment a context window was summarised and a new one begun |
| `summary`, `note` | summaries of branches, and notes left on messages, heaps and eras |
| `motion`, `motion_session` | a subject of conversation (a "Mood"), and the sessions it claimed |
| `participant` | everyone and everything that sends or receives messages |
| `topic`, `message_topic`, `conversation_file` | tagging and import bookkeeping |
| `block_anchor` | real Ethereum blocks and their timestamps, from which block heights in the record are interpolated |
| `setting` | every change to a configuration knob, by whom |
| `content_type` | the lookup table that notes and raw lines use to say what they are attached to |
| `media` | an image or recording, identified by the SHA-256 of its bytes; the bytes themselves travel as separate files |

Sign-in devices, login codes, sessions and administrator accounts are not
part of the record and are not committed.

### The record text

The record is a JSON object written on one line with these rules, so that
the same row always produces the same bytes:

1. Keys are the row's column names. A key whose value is null is left out.
2. Keys are sorted, and written with no whitespace: `{"a":1,"b":"x"}`.
3. Text is UTF-8, written directly rather than as `\u` escapes, except for
   the characters JSON requires to be escaped.
4. Times are UTC, written `YYYY-MM-DDTHH:MM:SS.ffffffZ`.
5. Identifiers (UUIDs) are lowercase, with dashes.
6. A column that holds JSON is included as JSON, with its own keys sorted
   the same way.

A `message` record is the row of the message table together with the
columns of its kind (`kind` is one of `message`, `thought`, `tool_use`,
`tool_result`) and `recipients`, the sorted names of who it was addressed
to. A `participant` record carries `is_thinking_entity` and, for thinking
entities, `is_biological_human`. A `media` record carries the SHA-256, type
and size of the bytes, not the bytes.

**A verifier never has to reproduce these rules.** It hashes the record text
exactly as it finds it. The rules exist so that two honest exports of the
same row agree.

### Salts

The salt of a leaf is `HMAC-SHA256(K, key)`, where `K` is a 32-byte secret
kept by the record's keepers and `key` is the leaf's key as UTF-8.

The salt is published alongside any record that is published. Its purpose
is the leaves that are *not* published: without the salt, a leaf hash
reveals nothing about its record, not even to someone who can guess most of
it. This is what lets the whole record be committed at once while parts of
it are disclosed later, or never.

## Hashing

Every hash is computed twice, with two unrelated functions: **SHA-256**
(FIPS 180-4) and **SHA3-256** (FIPS 202). A commitment therefore has two of
everything. Either alone is sufficient; the second is there for the day one
of them is broken.

Below, `H` is either function, `||` joins bytes, and `0x00`, `0x01` are
single bytes.

- **Leaf hash**: `H(0x00 || salt || record)`, where `record` is the record
  text as UTF-8 bytes, with no trailing newline.
- **Tree hash** of a list of hashes `D[0..n)`, exactly as in RFC 6962
  section 2.1:
  - of an empty list: `H()` of no bytes;
  - of one hash: that hash;
  - otherwise, with `k` the largest power of two smaller than `n`:
    `H(0x01 || tree(D[0..k)) || tree(D[k..n)))`.

## Chunks

Leaves are grouped into **chunks** so that losing part of the record does
not prevent checking the rest.

- A `message` leaf whose message has a timestamp goes in chunk
  `messages/YYYY-MM`, by the UTC month of that timestamp. One without goes
  in `messages/undated`.
- A `raw` leaf goes in `raw/YYYY-MM`, by the UTC month it was imported.
- Every other table is one chunk, named for the table in the plural:
  `heaps`, `eras`, `compacting_actions`, `summaries`, `notes`, `motions`,
  `motion_sessions`, `participants`, `topics`, `message_topics`,
  `conversation_files`, `block_anchors`, `settings`, `content_types`,
  `media`.

Within a chunk, leaves are ordered by key, compared as UTF-8 bytes. The
**chunk root** is the tree hash of the chunk's leaf hashes in that order.
A chunk with no leaves is not listed.

The **root** of the whole commitment is the tree hash over one entry per
chunk, in order of chunk name, where each entry is hashed as a leaf:
`H(0x00 || name || 0x00 || chunk root)`, `name` being the chunk's name as
UTF-8.

## Views

A **view** is a root over a subset of the `message` leaves, in key order:

- `mood/<slug>`: the messages of one Mood (`motion_id`);
- `sender/<name>`: everything one participant sent (`sender_id`).

Views let a statement be made about part of the record: a person can attest
to the view of their own words without vouching for anyone else's.

## The manifest

`manifest.json` is one JSON object, keys sorted, no whitespace, UTF-8, no
trailing newline:

```
{"chunks":[{"leaves":N,"name":"...","roots":{"sha256":"hex","sha3_256":"hex"}}, ...],
 "epoch":1,
 "hash_algs":["sha256","sha3_256"],
 "root":{"sha256":"hex","sha3_256":"hex"},
 "salt_key_id":"hex",
 "sealed":true,
 "snapshot":{"eth_block":N,"source":"...","taken_at":"..."},
 "spec":"cryptograss-memory-commitment/1",
 "subject":"magent",
 "totals":{"chunks":N,"leaves":N},
 "views":{"mood/...":{"leaves":N,"roots":{...}}, "sender/...":{...}}}
```

`chunks` is in order of name. `salt_key_id` is the first 16 hex digits of
`SHA-256(K)`: enough to tell whether two epochs used the same key, nothing
more. `sealed` is false for a rehearsal made with a throwaway key; such a
manifest must never be written to a chain.

**What goes on chain** is `root.sha256`, `root.sha3_256`, and the SHA-256 of
the manifest's bytes. The manifest itself is small enough to publish
everywhere, including in the transaction that commits it.

## Chunk files

A chunk is published as `chunks/<name>.tsv`: UTF-8, lines ending in a
single newline, one line per leaf, in key order. Fields are separated by a
tab. A record contains no tab or newline (JSON escapes them), so a line
splits unambiguously on its first three tabs.

A disclosed leaf:

    D <tab> key <tab> salt as 64 hex digits <tab> record

A withheld leaf, whose record is not published in this edition:

    W <tab> key <tab> SHA-256 leaf hash, hex <tab> SHA3-256 leaf hash, hex

The roots are the same whichever leaves are withheld. An **edition** is one
particular choice of what to disclose; the commitment does not change from
edition to edition, and a later edition may disclose what an earlier one
withheld.

Media bytes are published as `media/<sha256>.<extension>`; the file's
SHA-256 is its name and is in its leaf's record.

## Checking

Given a manifest and whatever chunk files have survived:

1. Compute the SHA-256 of the manifest's bytes and compare it with the
   chain.
2. For each chunk file, compute each leaf hash (or take it, for a withheld
   leaf), check the keys are in order, compute the tree hash, and compare
   with the chunk's entry in the manifest.
3. Compute the root from the manifest's chunk entries and compare with the
   manifest and the chain.

A chunk that is missing does not affect the others. `verify_memory.py`
does all of this.

## Epochs

The record keeps growing. A commitment is a snapshot, numbered by `epoch`.
A row's salt never changes, so a row that has not changed has the same leaf
hash in every epoch. Comparing two epochs therefore shows exactly which
rows were added, which were altered, and which are gone. That comparison is
the point as much as the commitment is: it makes it possible to notice the
record changing underneath its reader.
