"""The record can be committed, checked in parts, and compared across time."""

import contextlib
import hashlib
import io
import json
import os
import random
import tempfile
import uuid
from pathlib import Path
from unittest import mock

from django.contrib.contenttypes.models import ContentType
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase

from commitment import compare_memory, export, merkle, verify_memory
from conversations.models import (
    BlockAnchor, ContextHeap, ConversationParticipant, Era, Media, Message, Motion, Note,
    RawImportedContent, Setting, ThinkingEntity, Thought, ToolResult, ToolUse,
)

KEY = bytes(range(32))
OCT, NOV = 1_760_000_000_000, 1_763_000_000_000  # milliseconds: 2025-10-09, 2025-11-13


def run(tool, *args):
    """A stdlib tool's main(): (exit status, what it printed)."""
    printed = io.StringIO()
    with contextlib.redirect_stdout(printed):
        status = tool.main([tool.__name__, *map(str, args)])
    return status, printed.getvalue()


class TreeHashTest(SimpleTestCase):

    def test_rfc_6962_known_answers(self):
        inputs = [b'', b'\x00', b'\x10', b'\x20\x21', b'\x30\x31', b'\x40\x41\x42\x43',
                  bytes(range(0x50, 0x58)), bytes(range(0x60, 0x70))]
        roots = ['6e340b9cffb37a989ca544e6bb780a2c78901d3fb33738768511a30617afa01d',
                 'fac54203e7cc696cf0dfcb42c92a1d9dbaf70ad9e621f4bd8d98662f00e3c125',
                 'aeb6bcfe274b70a14fb067a5e5578264db0fa9b51af5e0ba159158f329e06e77',
                 'd37ee418976dd95753c1c73862b9398fa2a2cf9b4ff0fdfe8b30cd95209614b7',
                 '4e3bbb1f7b478dcfe71fb631631519a3bca12c9aefca1612bfce4c13a86264d4',
                 '76e67dadbcdf1e10e1b74ddc608abd2f98dfb16fbce75277b5232a127f2087ef',
                 'ddb89be403809e325750d3d263cd78929c2942b7942a34b77e122c9594a74c8c',
                 '5dc9da79a70659a9ad559cb701ded9a2ab9d823aad2f4960cfe370eff4604328']
        leaves = [merkle.h('sha256', merkle.LEAF + d) for d in inputs]
        for n, root in enumerate(roots, 1):
            self.assertEqual(merkle.tree_hash('sha256', leaves[:n]).hex(), root, f'{n} leaves')
        self.assertEqual(merkle.tree_hash('sha256', []), hashlib.sha256(b'').digest())

    def test_the_verifier_carries_the_same_tree(self):
        # verify_memory.py has its own copy so it can stand alone; hold the two together.
        rng = random.Random(6962)
        for n in list(range(0, 20)) + [31, 32, 33, 100]:
            hashes = [rng.randbytes(32) for _ in range(n)]
            for alg in merkle.ALGS:
                self.assertEqual(merkle.tree_hash(alg, hashes), verify_memory.tree_hash(alg, hashes))

    def test_a_leaf_cannot_pass_for_a_node(self):
        a, b = b'a' * 32, b'b' * 32
        self.assertNotEqual(merkle.tree_hash('sha256', [a, b]), merkle.h('sha256', merkle.LEAF + a + b))


class CommitmentTest(TestCase):

    def setUp(self):
        self.justin = ThinkingEntity.objects.create(name='justin', participant_type='human')
        self.magent = ThinkingEntity.objects.create(name='magent', participant_type='ai', is_biological_human=False)
        self.kid = ThinkingEntity.objects.create(name='fibonacci', participant_type='human')
        self.tool = ConversationParticipant.objects.create(name='tool-result', participant_type='tool')
        self.heap = ContextHeap.objects.create(era=Era.objects.create(name='Foundation'), type='fresh')
        self.motion = Motion.objects.create(slug='porch', title='The porch')

        def say(cls, sender, content, timestamp, to=(), **extra):
            message = cls.objects.create(id=uuid.uuid4(), sender=sender, content=content, timestamp=timestamp,
                                         context_heap=self.heap, **extra)
            message.recipients.set(to)
            return message

        self.hello = say(Message, self.justin, 'Hey there — do you have a name?', OCT,
                         to=[self.magent], motion=self.motion)
        self.reply = say(Message, self.magent, 'You can call me Claude.', OCT + 1000, to=[self.justin, self.kid],
                         motion=self.motion, model_backend='gpt-4o')
        self.thought = say(Thought, self.magent, 'They want a name.', NOV, signature='sig')
        self.call = say(ToolUse, self.magent, {'command': 'date'}, NOV + 1, tool_name='Bash', tool_id='toolu_1')
        self.result = say(ToolResult, self.tool, {'stdout': 'a\ttab\nand a newline', 'seconds': 1.5}, NOV + 2,
                          tool_use_id='toolu_1')
        self.games = say(Message, self.kid, 'can you make the musik the same ★', None, to=[self.magent])

        Note.objects.create(content_type=ContentType.objects.get_for_model(Message), object_id=self.hello.id,
                            from_entity=self.magent, content='The first message.')
        RawImportedContent.objects.create(content_type=ContentType.objects.get_for_model(Message),
                                          object_id=self.hello.id, raw_data={'type': 'user', 'secret': 'hunter2'})
        self.picture = b'\x89PNG not really'
        Media.objects.create(sha256=hashlib.sha256(self.picture).hexdigest(), mime='image/png',
                             data=self.picture, size=len(self.picture), added_by=self.justin)
        BlockAnchor.objects.create(number=21_081_875, timestamp=1_730_246_400)
        BlockAnchor.objects.create(number=9, timestamp=1)  # sorts after 21081875 as text; the export must cope
        Setting.objects.create(key='effort', value='high', set_by=self.justin)

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def bundle(self, name, **kwargs):
        out = Path(self.tmp.name) / name
        kwargs.setdefault('sealed', True)
        manifest, edition = export.export(out, KEY, kwargs.pop('epoch', 1), 26_000_000, **kwargs)
        return out, manifest, edition

    def lines(self, out, chunk):
        return [line.split(b'\t', 3) for line in (out / 'chunks' / f'{chunk}.tsv').read_bytes().splitlines()]

    def test_a_bundle_checks_against_its_own_manifest(self):
        out, manifest, edition = self.bundle('a')
        status, printed = run(verify_memory, out)
        self.assertEqual(status, 0, printed)
        self.assertNotIn('FAILED', printed)
        self.assertNotIn('NOT SEALED', printed)
        self.assertIn(manifest['root']['sha256'], printed)
        self.assertIn(hashlib.sha256((out / 'manifest.json').read_bytes()).hexdigest(), printed)

        names = [c['name'] for c in manifest['chunks']]
        self.assertEqual(names, sorted(names))
        for expected in ('messages/2025-10', 'messages/2025-11', 'messages/undated', 'heaps', 'eras', 'notes',
                         'participants', 'motions', 'block_anchors', 'settings', 'media', 'content_types'):
            self.assertIn(expected, names)
        self.assertEqual(manifest['totals']['leaves'], sum(c['leaves'] for c in manifest['chunks']))
        self.assertEqual(manifest['views']['mood/porch']['leaves'], 2)
        self.assertEqual(manifest['views']['sender/magent']['leaves'], 3)
        self.assertEqual(manifest['views']['sender/fibonacci']['leaves'], 1)
        self.assertEqual(edition['withheld'], {})
        # The bundle carries what a stranger needs to check it.
        for name in ('SPEC.md', 'verify_memory.py', 'compare_memory.py', 'README.txt'):
            self.assertTrue((out / name).exists(), name)

    def test_records_follow_the_rules_in_the_spec(self):
        out, _, _ = self.bundle('a')
        by_key = {key.decode(): json.loads(record) for kind, key, salt, record in self.lines(out, 'messages/2025-10')}
        reply = by_key[f'message/{self.reply.id}']
        self.assertEqual(list(reply), sorted(reply))
        self.assertNotIn(None, reply.values())            # nulls are left out,
        self.assertNotIn('parent_id', reply)              # so an absent parent is an absent key
        self.assertEqual(reply['kind'], 'message')
        self.assertEqual(reply['recipients'], ['fibonacci', 'justin'])
        self.assertEqual(reply['model_backend'], 'gpt-4o')
        self.assertRegex(reply['created_at'], r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}Z$')

        november = {json.loads(r)['kind']: json.loads(r) for _, _, _, r in self.lines(out, 'messages/2025-11')}
        self.assertEqual(november['thought']['signature'], 'sig')
        self.assertEqual(november['tool_use']['tool_name'], 'Bash')
        # A tab and a newline inside the content stayed inside the JSON: the line still has four fields.
        self.assertEqual(november['tool_result']['content']['stdout'], 'a\ttab\nand a newline')
        self.assertEqual(november['tool_result']['content']['seconds'], 1.5)

        undated = self.lines(out, 'messages/undated')
        self.assertIn('musik the same ★'.encode(), undated[0][3])  # written as UTF-8, not \\u escapes
        # Keys are in byte order, which for numbers is not numeric order.
        anchors = [key for _, key, _, _ in self.lines(out, 'block_anchors')]
        self.assertEqual(anchors, [b'block_anchor/21081875', b'block_anchor/9'])
        self.assertEqual((out / 'media' / f'{hashlib.sha256(self.picture).hexdigest()}.png').read_bytes(), self.picture)

    def test_the_same_record_gives_the_same_bytes(self):
        a, first, _ = self.bundle('a')
        b, second, _ = self.bundle('b')
        self.assertEqual(first['root'], second['root'])
        self.assertEqual(first['chunks'], second['chunks'])
        for path in sorted((a / 'chunks').rglob('*.tsv')):
            self.assertEqual(path.read_bytes(), (b / path.relative_to(a)).read_bytes(), path.name)

    def test_withholding_changes_what_is_published_and_not_what_is_committed(self):
        full, whole, _ = self.bundle('full')
        out, manifest, edition = self.bundle(
            'edition', withhold=['table:raw', 'table:media', 'kind:tool_result', 'sender:fibonacci'])
        self.assertEqual(manifest['root'], whole['root'])
        self.assertEqual(manifest['chunks'], whole['chunks'])
        self.assertEqual(manifest['views'], whole['views'])
        self.assertEqual(edition['withheld'],
                         {'table:raw': 1, 'table:media': 1, 'kind:tool_result': 1, 'sender:fibonacci': 1})

        everything = b''.join(p.read_bytes() for p in out.rglob('*') if p.is_file() and p.suffix in ('.tsv', '.json'))
        for secret in (b'hunter2', b'musik', b'and a newline'):
            self.assertNotIn(secret, everything)
        self.assertFalse((out / 'media').exists())

        status, printed = run(verify_memory, out)
        self.assertEqual(status, 0, printed)
        self.assertIn('4 withheld', printed)
        self.assertIn('partial sender/fibonacci  0 of 1', printed)
        self.assertIn('ok      sender/justin', printed)

        # A withheld leaf is salted: its hash is not the hash of its record alone.
        kind, key, sha256_hex, _ = self.lines(out, 'messages/undated')[0]
        record = self.lines(full, 'messages/undated')[0][3]
        self.assertEqual(kind, b'W')
        self.assertNotEqual(sha256_hex.decode(), hashlib.sha256(b'\x00' + record).hexdigest())
        self.assertEqual(sha256_hex.decode(),
                         merkle.leaf_hash('sha256', merkle.salt_for(KEY, key.decode()), record).hex())

    def test_a_changed_byte_is_caught_and_only_its_chunk_fails(self):
        out, _, _ = self.bundle('a')
        path = out / 'chunks' / 'messages' / '2025-10.tsv'
        path.write_bytes(path.read_bytes().replace(b'call me Claude', b'call me Magent'))
        status, printed = run(verify_memory, out)
        self.assertEqual(status, 1)
        self.assertIn('FAILED  messages/2025-10', printed)
        # The chunk, and both views the altered message belongs to; nothing else.
        self.assertEqual(printed.count('FAILED'), 3)
        self.assertIn('FAILED  mood/porch', printed)
        self.assertIn('FAILED  sender/magent', printed)
        self.assertIn('ok      sender/justin', printed)
        self.assertIn('ok      messages/2025-11', printed)
        self.assertIn('NOT verified', printed)

    def test_a_missing_chunk_leaves_the_rest_checkable(self):
        out, _, _ = self.bundle('a')
        (out / 'chunks' / 'messages' / '2025-11.tsv').unlink()
        status, printed = run(verify_memory, out)
        self.assertEqual(status, 0, printed)
        self.assertIn('missing messages/2025-11  (3 leaves cannot be checked)', printed)
        self.assertIn('ok      messages/2025-10', printed)

    def test_reordered_or_dropped_lines_are_caught(self):
        out, _, _ = self.bundle('a')
        path = out / 'chunks' / 'messages' / '2025-10.tsv'
        first, second = path.read_bytes().splitlines(keepends=True)
        path.write_bytes(second + first)
        self.assertEqual(run(verify_memory, out)[0], 1)
        path.write_bytes(first)
        self.assertEqual(run(verify_memory, out)[0], 1)

    def test_comparing_two_epochs_shows_what_changed_underneath(self):
        before, _, _ = self.bundle('before', epoch=1)
        Message.objects.filter(id=self.reply.id).update(content='You can call me magent.')
        Message.objects.filter(id=self.games.id).delete()
        Message.objects.create(id=uuid.uuid4(), sender=self.justin, content='New.', timestamp=NOV + 5,
                               context_heap=self.heap)
        after, _, _ = self.bundle('after', epoch=2)

        status, printed = run(compare_memory, before, after)
        self.assertEqual(status, 1)
        self.assertIn('added: 1  (1 message)', printed)
        self.assertIn('removed: 1  (1 message)', printed)
        self.assertIn('altered: 1  (1 message)', printed)
        self.assertIn(f'altered message/{self.reply.id}: content', printed)
        self.assertIn(f'removed message/{self.games.id}', printed)

        # Nothing altered or removed: growth alone is not a finding.
        again, _, _ = self.bundle('again', epoch=3)
        status, printed = run(compare_memory, after, again)
        self.assertEqual(status, 0, printed)

        # Withheld on one side: the salted leaf hash still shows the change.
        hidden, _, _ = self.bundle('hidden', epoch=2, withhold=['sender:magent'])
        status, printed = run(compare_memory, before, hidden)
        self.assertEqual(status, 1)
        self.assertIn('altered: 1', printed)

    def test_a_rehearsal_says_so_and_a_sealed_commitment_needs_the_key(self):
        out = Path(self.tmp.name) / 'rehearsal'
        printed = io.StringIO()
        call_command('commit_memory', str(out), '--rehearsal', '--eth-block', '26000000', stdout=printed)
        manifest = json.loads((out / 'manifest.json').read_bytes())
        self.assertFalse(manifest['sealed'])
        self.assertEqual(manifest['epoch'], 0)
        self.assertIn('Verified with its own verify_memory.py', printed.getvalue())
        self.assertIn('NOT SEALED', run(verify_memory, out)[1])

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('MEMORY_COMMITMENT_KEY', None)
            with self.assertRaises(CommandError):
                call_command('commit_memory', str(Path(self.tmp.name) / 'x'), '--epoch', '1', '--eth-block', '1')
        with mock.patch.dict(os.environ, {'MEMORY_COMMITMENT_KEY': KEY.hex()}):
            sealed = Path(self.tmp.name) / 'sealed'
            call_command('commit_memory', str(sealed), '--epoch', '1', '--eth-block', '26000000', stdout=io.StringIO())
            manifest = json.loads((sealed / 'manifest.json').read_bytes())
            self.assertTrue(manifest['sealed'])
            self.assertEqual(manifest['salt_key_id'], hashlib.sha256(KEY).hexdigest()[:16])
            self.assertNotIn(KEY.hex(), (sealed / 'manifest.json').read_text())
            with self.assertRaises(CommandError):  # never write over a bundle
                call_command('commit_memory', str(sealed), '--epoch', '1', '--eth-block', '1')
            with self.assertRaises(CommandError):  # a sealed commitment covers every table
                call_command('commit_memory', str(Path(self.tmp.name) / 'y'), '--epoch', '1', '--allow-missing',
                             '--eth-block', '1')

    def test_sign_in_state_is_not_part_of_the_record(self):
        committed = {t for table in export.TABLES for t in table.needs}
        for private in ('devices', 'login_codes', 'django_session', 'auth_user'):
            self.assertNotIn(private, committed)
