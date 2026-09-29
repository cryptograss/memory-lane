"""Block heights are interpolated between real anchors, never fetched at ingest."""

import uuid
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.test import TestCase

from conversations.models import BlockAnchor, Message, Motion, ThinkingEntity
from conversations.services import eth_blocks
from conversations.services.eth_blocks import BlockClock

# Two real-shaped intervals: the first has no missed slot (256 blocks in
# 256 slots), the second has three (256 blocks in 259 slots).
T0 = 1_790_000_000
ANCHORS = [(1000, T0), (1256, T0 + 256 * 12), (1512, T0 + 256 * 12 + 259 * 12)]


class BlockClockTest(TestCase):

    def setUp(self):
        self.clock = BlockClock(ANCHORS)

    def test_exact_at_anchors(self):
        for number, ts in ANCHORS:
            self.assertEqual(self.clock.block_at(ts), number)

    def test_exact_across_an_interval_with_no_missed_slot(self):
        self.assertEqual(self.clock.block_at(T0 + 12 * 7), 1007)
        self.assertEqual(self.clock.block_at(T0 + 12 * 7 + 11), 1007)
        self.assertEqual(self.clock.block_at(T0 + 12 * 8), 1008)

    def test_missed_slots_keep_the_estimate_inside_the_interval(self):
        start = ANCHORS[1][1]
        estimates = [self.clock.block_at(t) for t in range(start, ANCHORS[2][1])]
        self.assertEqual(estimates, sorted(estimates))
        self.assertTrue(all(1256 <= b <= 1511 for b in estimates))
        self.assertEqual(self.clock.block_at(ANCHORS[2][1] - 1), 1511)

    def test_outside_the_anchors_is_unknown_not_guessed(self):
        self.assertIsNone(self.clock.block_at(T0 - 1))
        self.assertIsNone(self.clock.block_at(ANCHORS[-1][1] + 1))
        self.assertIsNone(BlockClock([]).block_at(T0))


def fake_node(head=(1600, T0 + 600 * 12)):
    """A node where block n has timestamp T0 + 12 * (n - 1000)."""
    requested = []

    def post(url, payload):
        if isinstance(payload, dict):
            n, t = head
            return {'result': {'number': hex(n), 'timestamp': hex(t)}}
        requested.extend(int(p['params'][0], 16) for p in payload)
        return [{'id': p['id'], 'result': {'number': p['params'][0],
                                           'timestamp': hex(T0 + 12 * (int(p['params'][0], 16) - 1000))}}
                for p in payload]
    return post, requested


class SyncAnchorsTest(TestCase):

    def test_covers_since_through_head_and_is_idempotent(self):
        post, requested = fake_node()
        with mock.patch.object(eth_blocks, '_post', side_effect=post):
            added = eth_blocks.sync_anchors(T0 + 12 * 100, url='http://node.test')
            again = eth_blocks.sync_anchors(T0 + 12 * 100, url='http://node.test')

        numbers = list(BlockAnchor.objects.values_list('number', flat=True))
        self.assertEqual(numbers, [1024, 1280, 1536, 1600])
        self.assertEqual(added, 4)
        self.assertEqual(again, 0)
        self.assertEqual(BlockClock().block_at(T0 + 12 * 100 + 5), 1100)

    def test_max_batches_bounds_requests(self):
        post, requested = fake_node(head=(1000 + 256 * 250, T0 + 12 * 256 * 250))
        with mock.patch.object(eth_blocks, '_post', side_effect=post):
            eth_blocks.sync_anchors(T0, url='http://node.test', max_batches=1)
        self.assertEqual(len(requested), eth_blocks.BATCH_SIZE)


class StampCommandTest(TestCase):

    def setUp(self):
        BlockAnchor.objects.bulk_create(BlockAnchor(number=n, timestamp=t) for n, t in ANCHORS)
        justin = ThinkingEntity.objects.create(name='justin', is_biological_human=True)

        def message(ts):
            return Message.objects.create(id=uuid.uuid4(), content='x', sender=justin, timestamp=ts)
        self.inside = message((T0 + 12 * 7) * 1000 + 500)
        self.later = message((ANCHORS[-1][1] + 60) * 1000)
        self.untimed = Message.objects.create(id=uuid.uuid4(), content='x', sender=justin)

    def test_stamps_what_the_anchors_cover_and_nothing_else(self):
        out = StringIO()
        call_command('stamp_blockheights', '--no-sync', stdout=out)

        for m in (self.inside, self.later, self.untimed):
            m.refresh_from_db()
        self.assertEqual(self.inside.eth_blockheight, 1007)
        self.assertIsNone(self.later.eth_blockheight)
        self.assertIsNone(self.untimed.eth_blockheight)
        self.assertIn('messages=1', out.getvalue())

    def test_does_not_overwrite_a_motion_opened_at_a_known_block(self):
        Motion.objects.create(slug='known', eth_blockheight=26_071_421)
        call_command('stamp_blockheights', '--no-sync', stdout=StringIO())
        self.assertEqual(Motion.objects.get(slug='known').eth_blockheight, 26_071_421)

    def test_rerun_is_a_no_op(self):
        call_command('stamp_blockheights', '--no-sync', stdout=StringIO())
        out = StringIO()
        call_command('stamp_blockheights', '--no-sync', stdout=out)
        self.assertIn('messages=0', out.getvalue())
