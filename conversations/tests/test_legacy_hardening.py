"""
Hardening of the legacy (pre-Motion) JSON endpoints for public traffic.

Every one of these is unauthenticated, so each must serve a bounded page,
answer a malformed parameter with a 400 rather than a 500, and run a fixed
number of queries no matter how many rows it serializes.

heap_metadata is not covered: it uses Postgres DISTINCT ON, and these tests
run on SQLite.
"""

import uuid

from django.contrib.contenttypes.models import ContentType
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from conversations.models import (
    CompactingAction,
    ContextHeap,
    ConversationParticipant,
    Era,
    Message,
    Note,
    RawImportedContent,
    ThinkingEntity,
    Thought,
    ToolResult,
    ToolUse,
)


class LegacyFixture:
    """Two heaps in one era: a tool chain, a thought, a compact leaf, a note,
    and a message with no message_number (imports leave some of those)."""

    @classmethod
    def setUpTestData(cls):
        cls.justin = ThinkingEntity.objects.create(name='justin', is_biological_human=True)
        cls.magent = ThinkingEntity.objects.create(name='magent', is_biological_human=False)
        cls.tool = ConversationParticipant.objects.create(name='tool-result', participant_type='tool')
        cls.era = Era.objects.create(name='Hardening Era')
        cls.heap1 = ContextHeap.objects.create(era=cls.era)
        cls.heap2 = ContextHeap.objects.create(era=cls.era)

        def msg(model, heap, number, sender, content, **extra):
            m = model.objects.create(
                id=uuid.uuid4(), context_heap=heap, message_number=number,
                sender=sender, content=content, timestamp=1_700_000_000_000 + (number or 99) * 1000,
                eth_blockheight=20_000_000 + (number or 99), **extra,
            )
            return m

        cls.h1_opener = msg(Message, cls.heap1, 1, cls.justin, 'hello')
        cls.h1_thought = msg(Thought, cls.heap1, 2, cls.magent, 'hmm', signature='sig')
        cls.h1_tooluse = msg(ToolUse, cls.heap1, 3, cls.magent, {'tool_name': 'Bash'},
                             tool_name='Bash', tool_id='toolu_1')
        cls.h1_result = msg(ToolResult, cls.heap1, 4, cls.tool, 'ok', tool_use_id='toolu_1',
                            parent=cls.h1_tooluse)
        cls.h1_leaf = msg(Message, cls.heap1, 5, cls.magent, 'done')
        cls.h1_unnumbered = msg(Message, cls.heap1, None, cls.justin, 'imported without a number')
        cls.heap1_ids = [cls.h1_opener.id, cls.h1_thought.id, cls.h1_tooluse.id,
                         cls.h1_result.id, cls.h1_leaf.id, cls.h1_unnumbered.id]
        for m in (cls.h1_opener, cls.h1_leaf):
            m.recipients.add(cls.magent)

        cls.heap2_ids = [msg(Message, cls.heap2, n, cls.justin, f'h2 #{n}').id for n in (1, 2, 3)]

        cls.compact = CompactingAction.objects.create(
            context_heap=cls.heap1, ending_message=cls.h1_leaf,
            compact_trigger='auto', pre_compact_tokens=150_000,
        )
        RawImportedContent.objects.create(
            content_type=ContentType.objects.get_for_model(CompactingAction),
            object_id=cls.compact.id, raw_data={'type': 'summary'},
        )
        cls.orphan = CompactingAction.objects.create(compact_trigger='manual')
        Note.objects.create(
            content_type=ContentType.objects.get_for_model(Message),
            object_id=cls.h1_opener.id, from_entity=cls.magent, content='a note',
        )


class AllMessagesTests(LegacyFixture, TestCase):

    def get(self, **params):
        return self.client.get('/api/all_messages/', params)

    def walk(self, limit):
        """Follow `next` to the end; return (pages, every message dict in order)."""
        pages, after = [], None
        while True:
            params = {'limit': limit}
            if after:
                params['after'] = after
            response = self.get(**params)
            self.assertEqual(response.status_code, 200)
            data = response.json()
            pages.append(data)
            if not data['has_more']:
                self.assertIsNone(data['next'])
                break
            after = data['next']
            self.assertLess(len(pages), 50, 'pagination did not terminate')
        messages = [m for p in pages for e in p['eras'] for h in e['context_heaps'] for m in h['messages']]
        return pages, messages

    def test_first_page_keeps_the_old_shape(self):
        data = self.get().json()
        self.assertFalse(data['has_more'])
        self.assertIsNone(data['next'])
        [era] = data['eras']
        self.assertEqual(era['name'], 'Hardening Era')
        self.assertEqual(era['earliest_blockheight'], 20_000_001)
        self.assertEqual(era['latest_blockheight'], 20_000_099)
        heaps = {h['id']: h for h in era['context_heaps']}
        heap1 = heaps[str(self.heap1.id)]
        self.assertEqual(heap1['first_message_id'], str(self.h1_opener.id))
        self.assertEqual(heap1['compacting_action']['ending_message_id'], str(self.h1_leaf.id))
        self.assertEqual(heap1['child_heaps'], [])

        by_id = {m['id']: m for m in heap1['messages']}
        self.assertEqual(by_id[str(self.h1_result.id)]['tool_name'], 'Bash')
        self.assertEqual(by_id[str(self.h1_thought.id)]['signature'], 'sig')
        self.assertEqual(by_id[str(self.h1_opener.id)]['notes'][0]['content'], 'a note')
        self.assertEqual(by_id[str(self.h1_opener.id)]['recipients'], ['magent'])

        # The compact pseudo-message follows its leaf, with its raw import.
        ids = [m['id'] for m in heap1['messages']]
        compact_row = heap1['messages'][ids.index(str(self.h1_leaf.id)) + 1]
        self.assertEqual(compact_row['message_type'], 'CompactingAction')
        self.assertEqual(compact_row['raw_imported_content'], {'type': 'summary'})

        self.assertEqual([o['id'] for o in data['orphaned_compacting_actions']], [str(self.orphan.id)])

    def test_pages_cover_every_message_exactly_once(self):
        pages, messages = self.walk(limit=2)
        real = [m['id'] for m in messages if m['message_type'] != 'CompactingAction']
        expected = {str(i) for i in self.heap1_ids + self.heap2_ids}
        self.assertEqual(len(real), len(expected))
        self.assertEqual(set(real), expected)
        self.assertEqual(len(pages), 5)  # 9 messages, 2 per page
        # Orphans ride on the first page only.
        self.assertEqual(len(pages[0]['orphaned_compacting_actions']), 1)
        self.assertTrue(all(p['orphaned_compacting_actions'] == [] for p in pages[1:]))

    def test_heap_order_survives_paging(self):
        _, messages = self.walk(limit=1)
        heap1 = [m['id'] for m in messages if m['id'] in {str(i) for i in self.heap1_ids}]
        # Numbered messages in order, the unnumbered one last.
        self.assertEqual(heap1, [str(i) for i in self.heap1_ids])

    def test_bad_parameters_are_400s(self):
        for params in ({'limit': 'abc'}, {'limit': '-1'}, {'limit': '0'}, {'limit': '1.5'},
                       {'after': 'not-a-uuid'}):
            with self.subTest(params=params):
                self.assertEqual(self.get(**params).status_code, 400)

    def test_unknown_cursor_is_404(self):
        self.assertEqual(self.get(after=str(uuid.uuid4())).status_code, 404)


class HeapMessagesTests(LegacyFixture, TestCase):

    def url(self, heap_id):
        return f'/api/heap_messages/{heap_id}/'

    def test_whole_heap_in_one_page_by_default(self):
        data = self.client.get(self.url(self.heap1.id)).json()
        self.assertFalse(data['has_more'])
        self.assertIsNone(data['next'])
        ids = [m['id'] for m in data['messages'] if m['message_type'] != 'CompactingAction']
        self.assertEqual(ids, [str(i) for i in self.heap1_ids])
        types = [m['message_type'] for m in data['messages']]
        self.assertEqual(types.count('CompactingAction'), 1)
        self.assertEqual(types[types.index('CompactingAction') - 1], 'Message')

    def test_paging_with_after_walks_the_heap(self):
        seen, after = [], None
        for _ in range(10):
            params = {'limit': 2, **({'after': after} if after else {})}
            data = self.client.get(self.url(self.heap1.id), params).json()
            seen += [m['id'] for m in data['messages'] if m['message_type'] != 'CompactingAction']
            if not data['has_more']:
                break
            after = data['next']
        self.assertEqual(seen, [str(i) for i in self.heap1_ids])

    def test_bad_heap_id_is_400_and_unknown_is_404(self):
        self.assertEqual(self.client.get(self.url('not-a-uuid')).status_code, 400)
        self.assertEqual(self.client.get(self.url(uuid.uuid4())).status_code, 404)

    def test_bad_parameters_are_400s(self):
        url = self.url(self.heap1.id)
        for params in ({'limit': 'abc'}, {'limit': '0'}, {'after': 'zzz'},
                       {'after': str(self.heap2_ids[0])}):  # a message from another heap
            with self.subTest(params=params):
                self.assertEqual(self.client.get(url, params).status_code, 400)


class MessagesSinceTests(LegacyFixture, TestCase):

    def test_limit_and_has_more(self):
        url = f'/api/messages_since/{self.h1_opener.id}/'
        # message_number > 1 across all heaps: 2,3,4,5 in heap1 and 2,3 in heap2.
        data = self.client.get(url).json()
        self.assertEqual(len(data['messages']), 6)
        self.assertFalse(data['has_more'])
        data = self.client.get(url, {'limit': 2}).json()
        self.assertEqual(len(data['messages']), 2)
        self.assertTrue(data['has_more'])
        self.assertEqual(data['messages'][0]['era_id'], str(self.era.id))

    def test_bad_input_is_400_not_500(self):
        self.assertEqual(self.client.get('/api/messages_since/garbage/').status_code, 400)
        url = f'/api/messages_since/{self.h1_opener.id}/'
        self.assertEqual(self.client.get(url, {'limit': 'x'}).status_code, 400)
        # An unnumbered anchor used to reach filter(message_number__gt=None).
        url = f'/api/messages_since/{self.h1_unnumbered.id}/'
        self.assertEqual(self.client.get(url).status_code, 400)

    def test_unknown_message_is_404(self):
        self.assertEqual(self.client.get(f'/api/messages_since/{uuid.uuid4()}/').status_code, 404)


class SmallEndpointParamTests(LegacyFixture, TestCase):

    def test_recent_messages_bad_limit_is_400(self):
        for limit in ('abc', '-5', ''):
            with self.subTest(limit=limit):
                response = self.client.get('/api/recent_messages/', {'limit': limit})
                self.assertEqual(response.status_code, 200 if limit == '' else 400)

    def test_api_messages_bad_limit_is_400(self):
        for limit in ('abc', '-5'):
            with self.subTest(limit=limit):
                self.assertEqual(self.client.get('/api/messages/', {'limit': limit}).status_code, 400)

    def test_api_messages_keeps_parent_and_heap_ids(self):
        data = self.client.get('/api/messages/', {'limit': 100}).json()
        by_id = {m['id']: m for m in data}
        result = by_id[str(self.h1_result.id)]
        self.assertEqual(result['parent_uuid'], str(self.h1_tooluse.id))
        self.assertEqual(result['context_heap'], str(self.heap1.id))


class CapsTests(TestCase):
    """A heap bigger than any page: the caps hold and queries don't scale."""

    @classmethod
    def setUpTestData(cls):
        cls.justin = ThinkingEntity.objects.create(name='justin', is_biological_human=True)
        cls.magent = ThinkingEntity.objects.create(name='magent', is_biological_human=False)
        era = Era.objects.create(name='Big Era')
        cls.heap = ContextHeap.objects.create(era=era)
        Message.objects.bulk_create([
            Message(id=uuid.uuid4(), context_heap=cls.heap, message_number=n,
                    sender=cls.justin, content=f'message {n}', eth_blockheight=20_000_000 + n)
            for n in range(1, 1106)
        ])
        cls.first = Message.objects.get(context_heap=cls.heap, message_number=1)
        for m in Message.objects.filter(context_heap=cls.heap)[:300]:
            m.recipients.add(cls.magent)

    def count_messages(self, data):
        return sum(len(h['messages']) for e in data['eras'] for h in e['context_heaps'])

    def test_all_messages_clamps_limit(self):
        data = self.client.get('/api/all_messages/', {'limit': 10 ** 9}).json()
        self.assertEqual(self.count_messages(data), 1000)
        self.assertTrue(data['has_more'])
        data = self.client.get('/api/all_messages/').json()
        self.assertEqual(self.count_messages(data), 500)
        data = self.client.get('/api/all_messages/', {'after': data['next'], 'limit': 1000}).json()
        self.assertEqual(self.count_messages(data), 605)
        self.assertFalse(data['has_more'])

    def test_heap_messages_clamps_limit(self):
        url = f'/api/heap_messages/{self.heap.id}/'
        data = self.client.get(url, {'limit': 5000}).json()
        self.assertEqual(len(data['messages']), 1000)
        self.assertTrue(data['has_more'])
        data = self.client.get(url, {'after': data['next']}).json()
        self.assertEqual(len(data['messages']), 105)
        self.assertFalse(data['has_more'])

    def test_messages_since_clamps_limit(self):
        url = f'/api/messages_since/{self.first.id}/'
        data = self.client.get(url).json()
        self.assertEqual(len(data['messages']), 500)
        self.assertTrue(data['has_more'])
        data = self.client.get(url, {'limit': 10 ** 9}).json()
        self.assertEqual(len(data['messages']), 1000)

    def test_recent_messages_stays_capped_at_500(self):
        data = self.client.get('/api/recent_messages/', {'limit': 100000}).json()
        self.assertEqual(len(data['messages']), 500)

    def test_api_messages_clamps_limit(self):
        # types=message: the endpoint's default type list omits plain
        # messages (a long-standing quirk, not what this test is about).
        data = self.client.get('/api/messages/', {'limit': 100000, 'types': 'message'}).json()
        self.assertEqual(len(data), 1000)

    def assert_queries_do_not_scale(self, url, small, large):
        def run(params):
            with CaptureQueriesContext(connection) as ctx:
                self.assertEqual(self.client.get(url, params).status_code, 200)
            return len(ctx.captured_queries)
        few, many = run(small), run(large)
        self.assertEqual(few, many, f'{url}: {few} queries for a small page, {many} for a large one')
        self.assertLess(many, 20)

    def test_query_counts_are_fixed(self):
        self.assert_queries_do_not_scale(f'/api/heap_messages/{self.heap.id}/', {'limit': 5}, {'limit': 1000})
        self.assert_queries_do_not_scale('/api/all_messages/', {'limit': 5}, {'limit': 1000})
        self.assert_queries_do_not_scale(f'/api/messages_since/{self.first.id}/', {'limit': 5}, {'limit': 1000})
        self.assert_queries_do_not_scale('/api/recent_messages/', {'limit': 5}, {'limit': 500})
        self.assert_queries_do_not_scale('/api/messages/', {'limit': 5}, {'limit': 1000})
