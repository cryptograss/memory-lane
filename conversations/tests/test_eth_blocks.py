"""Block heights are interpolated between real anchors, never fetched at ingest."""

import uuid
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.test import TestCase

from conversations.models import BlockAnchor, Message, Motion, ThinkingEntity
from conversations.services import eth_blocks
from conversations.services.eth_blocks import ANCHOR_SPACING, BlockClock

# A post-Merge block on the anchor grid, and its timestamp.
B = 25_999_872
T0 = 1_790_000_000

# Two real-shaped intervals: the first has no missed slot (256 blocks in
# 256 slots), the second has three (256 blocks in 259 slots).
ANCHORS = [(B, T0), (B + 256, T0 + 256 * 12), (B + 512, T0 + 256 * 12 + 259 * 12)]


class BlockClockTest(TestCase):

    def setUp(self):
        self.clock = BlockClock(ANCHORS)

    def test_exact_at_anchors(self):
        for number, ts in ANCHORS:
            self.assertEqual(self.clock.block_at(ts), number)

    def test_exact_across_an_interval_with_no_missed_slot(self):
        self.assertEqual(self.clock.block_at(T0 + 12 * 7), B + 7)
        self.assertEqual(self.clock.block_at(T0 + 12 * 7 + 11), B + 7)
        self.assertEqual(self.clock.block_at(T0 + 12 * 8), B + 8)

    def test_missed_slots_keep_the_estimate_inside_the_interval(self):
        start = ANCHORS[1][1]
        estimates = [self.clock.block_at(t) for t in range(start, ANCHORS[2][1])]
        self.assertEqual(estimates, sorted(estimates))
        self.assertTrue(all(B + 256 <= b <= B + 511 for b in estimates))
        self.assertEqual(self.clock.block_at(ANCHORS[2][1] - 1), B + 511)

    def test_outside_the_anchors_is_unknown_not_guessed(self):
        self.assertIsNone(self.clock.block_at(T0 - 1))
        self.assertIsNone(self.clock.block_at(ANCHORS[-1][1] + 1))
        self.assertIsNone(BlockClock([]).block_at(T0))

    def test_a_hole_between_anchors_is_unknown_not_guessed(self):
        clock = BlockClock([ANCHORS[0], (B + 5 * ANCHOR_SPACING, T0 + 5 * ANCHOR_SPACING * 12)])
        self.assertIsNone(clock.block_at(T0 + 12 * 100))


def fake_node(head=(B + 600, T0 + 600 * 12), missing=()):
    """A node where block n has timestamp T0 + 12 * (n - B), and some blocks come back null."""
    requested = []

    def post(url, payload):
        if isinstance(payload, dict):
            n, t = head
            return {'result': {'number': hex(n), 'timestamp': hex(t)}}
        requested.extend(int(p['params'][0], 16) for p in payload)
        return [{'id': p['id'], 'result': None if int(p['params'][0], 16) in missing else
                 {'number': p['params'][0], 'timestamp': hex(T0 + 12 * (int(p['params'][0], 16) - B))}}
                for p in payload]
    return post, requested


class SyncAnchorsTest(TestCase):

    def sync(self, since, **kwargs):
        post, requested = fake_node(**{k: kwargs.pop(k) for k in ('head', 'missing') if k in kwargs})
        with mock.patch.object(eth_blocks, '_post', side_effect=post):
            added = eth_blocks.sync_anchors(since, url='http://node.test', **kwargs)
        return added, requested

    def test_covers_since_through_head_and_is_idempotent(self):
        added, _ = self.sync(T0 + 12 * 100)
        again, _ = self.sync(T0 + 12 * 100)

        numbers = list(BlockAnchor.objects.values_list('number', flat=True))
        self.assertEqual(numbers, [B, B + 256, B + 512, B + 600])
        self.assertEqual(added, 4)
        self.assertEqual(again, 0)
        self.assertEqual(BlockClock().block_at(T0 + 12 * 100 + 5), B + 100)

    def test_max_batches_bounds_requests(self):
        _, requested = self.sync(T0, head=(B + 256 * 250, T0 + 12 * 256 * 250), max_batches=1)
        self.assertEqual(len(requested), eth_blocks.BATCH_SIZE)

    def test_null_blocks_are_skipped_and_asked_for_again(self):
        added, _ = self.sync(T0, missing={B + 256})
        self.assertEqual(added, 3)
        added, requested = self.sync(T0)
        self.assertEqual((added, requested), (1, [B + 256]))

    def test_never_reaches_back_before_the_merge(self):
        _, requested = self.sync(0, head=(eth_blocks.MERGE_BLOCK + 300, eth_blocks.MERGE_TIMESTAMP + 3600))
        self.assertTrue(requested)
        self.assertGreaterEqual(min(requested), eth_blocks.MERGE_BLOCK - ANCHOR_SPACING)


class StampCommandTest(TestCase):

    def setUp(self):
        BlockAnchor.objects.bulk_create(BlockAnchor(number=n, timestamp=t) for n, t in ANCHORS)
        self.justin = ThinkingEntity.objects.create(name='justin', is_biological_human=True)
        self.inside = self.message((T0 + 12 * 7) * 1000 + 500)
        self.later = self.message((ANCHORS[-1][1] + 60) * 1000)
        self.untimed = Message.objects.create(id=uuid.uuid4(), content='x', sender=self.justin)

    def message(self, ts):
        return Message.objects.create(id=uuid.uuid4(), content='x', sender=self.justin, timestamp=ts)

    def run_command(self, *args):
        out, err = StringIO(), StringIO()
        call_command('stamp_blockheights', *args, stdout=out, stderr=err)
        return out.getvalue(), err.getvalue()

    def test_stamps_what_the_anchors_cover_and_nothing_else(self):
        out, _ = self.run_command('--no-sync')

        for m in (self.inside, self.later, self.untimed):
            m.refresh_from_db()
        self.assertEqual(self.inside.eth_blockheight, B + 7)
        self.assertIsNone(self.later.eth_blockheight)
        self.assertIsNone(self.untimed.eth_blockheight)
        self.assertIn('messages=1', out)

    def test_does_not_overwrite_a_motion_opened_at_a_known_block(self):
        Motion.objects.create(slug='known', eth_blockheight=26_071_421)
        self.run_command('--no-sync')
        self.assertEqual(Motion.objects.get(slug='known').eth_blockheight, 26_071_421)

    def test_rerun_is_a_no_op(self):
        self.run_command('--no-sync')
        out, _ = self.run_command('--no-sync')
        self.assertIn('messages=0', out)

    def test_a_failed_sync_still_stamps_what_it_can(self):
        with mock.patch.object(eth_blocks, '_post', side_effect=RuntimeError('node down')):
            out, err = self.run_command()
        self.assertIn('node down', err)
        self.assertIn('messages=1', out)

    def test_a_pre_merge_message_neither_blocks_nor_drives_the_sync(self):
        ancient = self.message(1_500_000_000_000)
        post, requested = fake_node(head=(B + 600, T0 + 600 * 12))
        with mock.patch.object(eth_blocks, '_post', side_effect=post):
            out, err = self.run_command()
        ancient.refresh_from_db()
        self.assertIsNone(ancient.eth_blockheight)
        self.assertEqual(err, '')
        self.assertIn('messages=2', out)  # inside, and later once the head anchor covers it
