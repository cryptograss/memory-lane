"""Finding a message and what was said around it: prefixes, context, exact search."""

import asyncio
import uuid

from django.test import TestCase, TransactionTestCase

from conversations.mcp import tools
from conversations.models import ContextHeap, ConversationParticipant, Era, Message, Mood, ThinkingEntity, Thought, ToolUse
from conversations.services.memory import message_context, readable, resolve_message, search, snippet


def call(name, **args):
    return asyncio.run(tools.TOOL_HANDLERS[name](args))[0].text


class Fixture:

    def setUp(self):
        self.justin = ThinkingEntity.objects.create(name='justin')
        self.magent = ThinkingEntity.objects.create(name='magent', participant_type='ai', is_biological_human=False)
        ConversationParticipant.objects.create(name='tool-result', participant_type='tool')
        self.heap = ContextHeap.objects.create(era=Era.objects.create(name='E'), type='fresh')
        self.session = uuid.uuid4()
        self.mood = Mood.objects.create(slug='porch')
        self.said = []
        for i, text in enumerate(['first', 'second', 'third', 'fourth', 'fifth']):
            self.said.append(Message.objects.create(
                id=uuid.uuid4(), sender=self.justin if i % 2 == 0 else self.magent, content=text,
                timestamp=1_760_000_000_000 + i * 1000, session_id=self.session, context_heap=self.heap,
                mood=self.mood))
        self.call = ToolUse.objects.create(
            id=uuid.UUID('03e36f39-2c40-4a9b-93eb-eeffcc3d28dc'), sender=self.magent,
            content={'command': 'docker exec wiki php run.php changePassword --user=JMyles --password=x'},
            tool_name='Bash', tool_id='t1', context_heap=self.heap, timestamp=None)
        self.thought = Thought.objects.create(id=uuid.uuid4(), sender=self.magent, signature='s',
                                              content=[{'type': 'thinking', 'thinking': 'They want a name.'}],
                                              timestamp=1_760_000_010_000, session_id=self.session,
                                              context_heap=self.heap)

    def test_a_prefix_or_link_finds_the_message(self):
        for ref in ('03e36f39', ' 03E36F39 ', f'https://x/moods/porch/#m-{self.call.id}', str(self.call.id)):
            self.assertEqual(resolve_message(ref)[0].id, self.call.id, ref)
        message, _, problem = resolve_message('03e3')
        self.assertIsNone(message)
        self.assertIn('at least 6', problem)
        self.assertIn('No message id starts', resolve_message('ffffff00')[2])

    def test_an_ambiguous_prefix_lists_the_candidates(self):
        a = Message.objects.create(id=uuid.UUID('abcdef00-0000-4000-8000-000000000001'), sender=self.justin, content='a')
        b = Message.objects.create(id=uuid.UUID('abcdef00-0000-4000-8000-000000000002'), sender=self.justin, content='b')
        message, candidates, problem = resolve_message('abcdef00')
        self.assertIsNone(message)
        self.assertEqual({c.id for c in candidates}, {a.id, b.id})
        self.assertIn('starts 2', problem)

    def test_context_is_the_session_around_the_message_in_order(self):
        middle = self.said[2]
        text = message_context(str(middle.id)[:8], before=1, after=1)
        self.assertIn(f'session {self.session}', text)
        self.assertIn('3 of 6', text)
        order = [text.index(w) for w in ('second', 'third', 'fourth')]
        self.assertEqual(order, sorted(order))
        self.assertNotIn('first', text.split('\n', 3)[3])
        self.assertIn(f'>>> [justin · message', text)

    def test_a_message_without_a_session_gets_its_heap(self):
        text = message_context('03e36f39', before=2, after=2)
        self.assertIn('this message has no session', text)
        self.assertIn('Bash: docker exec wiki php run.php changePassword', text)  # the command, not dropped

    def test_thinking_and_tool_calls_are_shown(self):
        self.assertEqual(readable(self.thought), 'They want a name.')
        self.assertTrue(readable(self.call).startswith('Bash: docker exec'))

    def test_exact_search_finds_a_command_and_points_at_it(self):
        hits = search('changePassword --user=JMyles', exact=True)
        self.assertEqual([h['message'].id for h in hits], [self.call.id])
        self.assertEqual(hits[0]['kind'], 'tool_use')
        self.assertIn('changePassword --user=JMyles', hits[0]['snippet'])

    def test_exact_search_matches_quotes_as_stored(self):
        quoted = Message.objects.create(id=uuid.uuid4(), sender=self.justin, content='he said "no dice" twice')
        self.assertEqual([h['message'].id for h in search('said "no dice"', exact=True)], [quoted.id])

    def test_search_can_be_limited_to_one_sender(self):
        self.assertEqual({h['message'].sender_id for h in search('first', exact=True, sender='magent')}, set())
        self.assertEqual(len(search('first', exact=True, sender='justin')), 1)

    def test_snippet_centres_on_the_match(self):
        text = 'x' * 500 + ' needle ' + 'y' * 500
        s = snippet(text, ['needle'], width=20)
        self.assertIn('needle', s)
        self.assertTrue(s.startswith('… ') and s.endswith(' …'))
        self.assertLess(len(s), 60)



class MessageContextTest(Fixture, TestCase):
    pass


class MemoryToolsTest(Fixture, TransactionTestCase):
    """Through the MCP handlers, which run on their own connection and so need committed rows."""

    def test_get_message_by_id_takes_a_prefix_and_lists_ambiguity(self):
        self.assertIn(str(self.call.id), call('get_message_by_id', message_id='03e36f39'))
        a = Message.objects.create(id=uuid.UUID('abcdef00-0000-4000-8000-000000000001'), sender=self.justin, content='a')
        b = Message.objects.create(id=uuid.UUID('abcdef00-0000-4000-8000-000000000002'), sender=self.justin, content='b')
        text = call('get_message_by_id', message_id='abcdef00')
        self.assertIn(str(a.id), text)
        self.assertIn(str(b.id), text)

    def test_search_output_carries_ids_and_points_at_context(self):
        text = call('search_messages', query='changePassword --user=JMyles', exact=True)
        self.assertIn(str(self.call.id), text)
        self.assertIn('tool_use', text)
        self.assertIn('get_message_context', text)

    def test_context_tool(self):
        text = call('get_message_context', message_id=str(self.said[2].id)[:8], before=1, after=1)
        self.assertIn('>>>', text)
        self.assertIn('Mood: porch', text)

    def test_word_search_names_the_mood_and_suggests_exact_when_empty(self):
        text = call('search_messages', query='zzzzunfindable')
        self.assertIn('exact: true', text)

    def test_read_mood_takes_a_time_without_an_offset(self):  # #76
        text = call('read_mood', slug='porch', **{'from': '2025-10-09'})
        self.assertNotIn('neither', text)
        self.assertIn('first', text)
