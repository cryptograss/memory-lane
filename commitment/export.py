"""Export the record as a committed bundle (see SPEC.md).

Reads only. The whole export runs inside one REPEATABLE READ, READ ONLY
transaction, so it sees the record as it stood at a single instant even
while the watcher keeps writing to it.
"""
import datetime as dt
import hashlib
import json
import shutil
import time
import uuid
from collections import Counter, defaultdict
from pathlib import Path

from django.db import connection, transaction

from . import merkle

SPEC_ID = 'cryptograss-memory-commitment/1'
HERE = Path(__file__).parent
JSON_OIDS = (114, 3802)  # json, jsonb: Django's connection hands jsonb back as text

MESSAGE_SQL = """
SELECT m.*,
       CASE WHEN th.message_ptr_id IS NOT NULL THEN 'thought'
            WHEN tu.message_ptr_id IS NOT NULL THEN 'tool_use'
            WHEN tr.message_ptr_id IS NOT NULL THEN 'tool_result'
            ELSE 'message' END AS kind,
       th.signature, tu.tool_name, tu.tool_id, tr.tool_use_id, tr.is_error,
       r.recipients
FROM conversations_message m
LEFT JOIN conversations_thought th ON th.message_ptr_id = m.id
LEFT JOIN conversations_tooluse tu ON tu.message_ptr_id = m.id
LEFT JOIN conversations_toolresult tr ON tr.message_ptr_id = m.id
LEFT JOIN (SELECT message_id,
                  array_agg(conversationparticipant_id ORDER BY conversationparticipant_id) AS recipients
           FROM conversations_message_recipients GROUP BY message_id) r ON r.message_id = m.id
ORDER BY m.id
"""

PARTICIPANT_SQL = """
SELECT p.name, p.participant_type,
       (t.conversationparticipant_ptr_id IS NOT NULL) AS is_thinking_entity,
       t.is_biological_human
FROM conversation_participants p
LEFT JOIN thinking_entities t ON t.conversationparticipant_ptr_id = p.name
"""


def by_month(prefix, column):
    """Chunk name from a row: <prefix>/YYYY-MM by a time column, else <prefix>/undated."""
    def chunk(row):
        value = row.get(column)
        if value is None:
            return f'{prefix}/undated'
        if isinstance(value, int):  # milliseconds since the epoch
            value = dt.datetime.fromtimestamp(value / 1000, dt.timezone.utc)
        return f'{prefix}/{value.astimezone(dt.timezone.utc):%Y-%m}'
    return chunk


class Table:
    def __init__(self, name, sql, key, chunk, needs, stream=False):
        self.name, self.sql, self.key, self.needs, self.stream = name, sql, key, needs, stream
        self.chunk = chunk if callable(chunk) else (lambda row: chunk)


def whole(name, table, chunk, key='id'):
    return Table(name, f'SELECT * FROM {table}', key, chunk, (table,))


# Every table of the record, in the order they are written. Sign-in devices,
# login codes, sessions and admin accounts are deliberately not here.
TABLES = (
    Table('message', MESSAGE_SQL, 'id', by_month('messages', 'timestamp'),
          ('conversations_message', 'conversations_thought', 'conversations_tooluse',
           'conversations_toolresult', 'conversations_message_recipients'), stream=True),
    Table('raw', 'SELECT * FROM raw_imported_content ORDER BY id', 'id', by_month('raw', 'imported_at'),
          ('raw_imported_content',), stream=True),
    whole('heap', 'context_heaps', 'heaps'),
    whole('era', 'eras', 'eras'),
    whole('compacting_action', 'compacting_actions', 'compacting_actions'),
    whole('summary', 'summaries', 'summaries'),
    whole('note', 'notes', 'notes'),
    whole('motion', 'motions', 'motions', key='slug'),
    whole('motion_session', 'motion_sessions', 'motion_sessions', key='session_id'),
    Table('participant', PARTICIPANT_SQL, 'name', 'participants',
          ('conversation_participants', 'thinking_entities')),
    whole('topic', 'topics', 'topics'),
    whole('message_topic', 'message_topics', 'message_topics'),
    whole('conversation_file', 'conversation_files', 'conversation_files'),
    whole('block_anchor', 'block_anchors', 'block_anchors', key='number'),
    whole('setting', 'settings', 'settings'),
    whole('content_type', 'django_content_type', 'content_types'),
    Table('media', 'SELECT sha256, mime, size, added_by_id, created_at FROM media', 'sha256', 'media', ('media',)),
)

MEDIA_EXTENSIONS = {'image/png': 'png', 'image/jpeg': 'jpg', 'image/gif': 'gif', 'image/webp': 'webp',
                    'audio/webm': 'webm', 'audio/ogg': 'ogg', 'audio/mp4': 'm4a', 'audio/mpeg': 'mp3',
                    'audio/wav': 'wav'}


class ExportError(Exception):
    pass


def plain(value):
    """A database value as something JSON can carry, by SPEC.md's rules."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, dt.datetime):
        if value.tzinfo is None:
            raise ExportError(f'a time without a timezone: {value!r}')
        return value.astimezone(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    if isinstance(value, dict):
        return {str(k): plain(v) for k, v in value.items()}
    raise ExportError(f'no rule for a {type(value).__name__}: {value!r}')


def record_text(row):
    """The one line of JSON that is hashed for a row."""
    return json.dumps({k: plain(v) for k, v in row.items() if v is not None},
                      sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')


def rows(sql, stream):
    """Rows of a query as dicts, JSON columns parsed. Streamed from the server when asked."""
    cursor = connection.chunked_cursor() if stream else connection.cursor()
    try:
        cursor.execute(sql)
        batch = cursor.fetchmany(2000)
        if not batch:
            return
        columns = [(d[0], d[1] in JSON_OIDS) for d in cursor.description]
        while batch:
            for values in batch:
                yield {name: (json.loads(v) if is_json and isinstance(v, str) else v)
                       for (name, is_json), v in zip(columns, values)}
            batch = cursor.fetchmany(2000)
    finally:
        cursor.close()


def existing_tables():
    with connection.cursor() as c:
        c.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        return {r[0] for r in c.fetchall()}


def parse_withhold(specs):
    """--withhold values -> a function (table, row) -> the reason a leaf is withheld, or None.

    table:<name>   every leaf of a table (table:raw, table:media)
    kind:<kind>    messages of a kind (kind:tool_result, kind:thought)
    sender:<name>  messages a participant sent
    """
    rules = []
    for spec in specs or ():
        what, _, value = spec.partition(':')
        if what not in ('table', 'kind', 'sender') or not value:
            raise ExportError(f'--withhold {spec!r}: expected table:<name>, kind:<kind> or sender:<name>')
        rules.append((what, value, spec))

    def reason(table, row):
        for what, value, spec in rules:
            if what == 'table' and table == value:
                return spec
            if table == 'message' and what == 'kind' and row.get('kind') == value:
                return spec
            if table == 'message' and what == 'sender' and row.get('sender_id') == value:
                return spec
        return None
    return reason


class Writer:
    """Chunk files on disk, and the leaf hashes that become their roots."""

    def __init__(self, out):
        self.out = out
        self.files, self.hashers, self.last_key = {}, {}, {}
        self.leaves = defaultdict(lambda: {alg: [] for alg in merkle.ALGS})
        self.disclosed, self.withheld = Counter(), Counter()

    def write(self, chunk, key, salt, record, withheld_for=None):
        key_bytes = key.encode('utf-8')
        if chunk in self.last_key and key_bytes <= self.last_key[chunk]:
            raise ExportError(f'{chunk}: {key} is not after the key before it')
        self.last_key[chunk] = key_bytes
        hashes = {alg: merkle.leaf_hash(alg, salt, record) for alg in merkle.ALGS}
        for alg in merkle.ALGS:
            self.leaves[chunk][alg].append(hashes[alg])
        if withheld_for:
            line = b'W\t' + key_bytes + b'\t' + '\t'.join(hashes[alg].hex() for alg in merkle.ALGS).encode() + b'\n'
            self.withheld[withheld_for] += 1
        else:
            line = b'D\t' + key_bytes + b'\t' + salt.hex().encode() + b'\t' + record + b'\n'
            self.disclosed[chunk] += 1
        if chunk not in self.files:
            path = self.out / 'chunks' / f'{chunk}.tsv'
            path.parent.mkdir(parents=True, exist_ok=True)
            self.files[chunk], self.hashers[chunk] = open(path, 'wb'), hashlib.sha256()
        self.files[chunk].write(line)
        self.hashers[chunk].update(line)
        return hashes

    def close(self):
        for f in self.files.values():
            f.close()


def export(out, secret, epoch, eth_block, sealed, withhold=(), allow_missing=False, log=lambda s: None):
    """Write a bundle to `out` and return its manifest (a dict)."""
    out = Path(out)
    if out.exists() and any(out.iterdir()):
        raise ExportError(f'{out} is not empty')
    out.mkdir(parents=True, exist_ok=True)
    withheld_reason = parse_withhold(withhold)
    started = time.time()
    writer = Writer(out)
    views = defaultdict(lambda: {alg: [] for alg in merkle.ALGS})
    per_table, skipped, media_files = Counter(), [], {}

    # One snapshot for the whole export. A caller already inside a transaction
    # (the test suite) has fixed its own view, and Postgres won't change it now.
    own_transaction = not connection.in_atomic_block
    with transaction.atomic():
        with connection.cursor() as c:
            if own_transaction:
                c.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
            c.execute('SELECT now(), (SELECT max(created_at) FROM conversations_message)')
            taken_at, newest = c.fetchone()
        present = existing_tables()

        for table in TABLES:
            missing = [t for t in table.needs if t not in present]
            if missing:
                if not allow_missing:
                    raise ExportError(f'table {missing[0]} does not exist (use allow_missing for an older copy)')
                skipped.append(table.name)
                log(f'  {table.name}: skipped, {missing[0]} does not exist here')
                continue
            source = rows(table.sql, table.stream)
            if not table.stream:  # small: sort by key bytes, which is not the database's order for numbers
                source = sorted(source, key=lambda row: f'{table.name}/{plain(row[table.key])}'.encode('utf-8'))
            for row in source:
                key = f'{table.name}/{plain(row[table.key])}'
                record = record_text(row).encode('utf-8')
                hashes = writer.write(table.chunk(row), key, merkle.salt_for(secret, key), record,
                                      withheld_reason(table.name, row))
                per_table[table.name] += 1
                if table.name == 'message':
                    for view in (f"mood/{row['motion_id']}" if row.get('motion_id') else None,
                                 f"sender/{row['sender_id']}" if row.get('sender_id') else None):
                        if view:
                            for alg in merkle.ALGS:
                                views[view][alg].append(hashes[alg])
            log(f'  {table.name}: {per_table[table.name]} leaves')

        # Media bytes travel beside the chunks, named by their own hash.
        if 'media' in present and not withheld_reason('media', {}):
            for row in rows('SELECT sha256, mime, data FROM media ORDER BY sha256', stream=True):
                data = bytes(row['data'])
                if hashlib.sha256(data).hexdigest() != row['sha256']:
                    raise ExportError(f"media {row['sha256']}: the stored bytes do not have that hash")
                name = f"media/{row['sha256']}.{MEDIA_EXTENSIONS.get(row['mime'], 'bin')}"
                (out / 'media').mkdir(exist_ok=True)
                (out / name).write_bytes(data)
                media_files[name] = len(data)
    writer.close()

    chunks = [{'name': name, 'leaves': len(writer.leaves[name]['sha256']),
               'roots': {alg: merkle.tree_hash(alg, writer.leaves[name][alg]).hex() for alg in merkle.ALGS}}
              for name in sorted(writer.leaves, key=lambda n: n.encode('utf-8'))]
    manifest = {
        'spec': SPEC_ID,
        'subject': 'magent',
        'epoch': epoch,
        'sealed': sealed,
        'hash_algs': list(merkle.ALGS),
        'salt_key_id': hashlib.sha256(secret).hexdigest()[:16],
        'snapshot': {
            'taken_at': plain(taken_at),
            'newest_message_at': plain(newest),
            'eth_block': eth_block,
            'source': 'memory-lane',
        },
        'totals': {'chunks': len(chunks), 'leaves': sum(c['leaves'] for c in chunks)},
        'chunks': chunks,
        'root': {alg: merkle.root_of_chunks(alg, [(c['name'], bytes.fromhex(c['roots'][alg])) for c in chunks]).hex()
                 for alg in merkle.ALGS},
        'views': {name: {'leaves': len(v['sha256']),
                         'roots': {alg: merkle.tree_hash(alg, v[alg]).hex() for alg in merkle.ALGS}}
                  for name, v in sorted(views.items())},
    }
    manifest_bytes = canonical(manifest)
    (out / 'manifest.json').write_bytes(manifest_bytes)

    # What this particular publication discloses. Not part of the commitment.
    edition = {
        'manifest_sha256': hashlib.sha256(manifest_bytes).hexdigest(),
        'leaves_by_table': dict(per_table),
        'tables_skipped': skipped,
        'withheld': dict(writer.withheld),
        'disclosed': sum(writer.disclosed.values()),
        'chunk_files': {f'chunks/{name}.tsv': writer.hashers[name].hexdigest() for name in sorted(writer.files)},
        'media_files': len(media_files),
        'media_bytes': sum(media_files.values()),
        'seconds': round(time.time() - started, 1),
    }
    (out / 'edition.json').write_text(json.dumps(edition, indent=1, sort_keys=True) + '\n')
    for name in ('SPEC.md', 'verify_memory.py', 'compare_memory.py', 'README.txt'):
        if (HERE / name).exists():
            shutil.copy(HERE / name, out / name)
    return manifest, edition
