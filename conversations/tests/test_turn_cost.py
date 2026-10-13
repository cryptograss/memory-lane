"""What a turn cost, at API list prices: each call priced from its usage (mood_view.PRICES)."""

import uuid

from django.test import TestCase

from conversations.models import Message, Mood, ThinkingEntity
from conversations.services.mood_view import call_usd, how_payload


class TurnCostTest(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.magent = ThinkingEntity.objects.create(name='magent', is_biological_human=False)
        cls.mood = Mood.objects.create(slug='m26')

    def test_a_call_is_priced_from_its_four_kinds_of_token(self):
        # A silent quiet wake from the record: nothing cached, the whole session written to the cache.
        self.assertAlmostEqual(call_usd('claude-opus-5-5', 2, 35, 0, 420_158), (2 * 4 + 35 * 20 + 420_158 * 8) / 1e6)
        # The same call with its context still cached: a twentieth of that.
        self.assertAlmostEqual(call_usd('claude-opus-5-5', 2, 35, 420_158, 0), (2 * 4 + 35 * 20 + 420_158 * 0.20) / 1e6)

    def test_the_first_matching_prefix_is_the_model(self):
        self.assertEqual(call_usd('claude-opus-5-5', 1_000_000, 0, 0, 0), 4.0)
        self.assertEqual(call_usd('claude-opus-5', 1_000_000, 0, 0, 0), 5.0)
        self.assertEqual(call_usd('claude-fable-5-1', 0, 0, 1_000_000, 0), 0.25)
        self.assertEqual(call_usd('claude-fable-5', 0, 0, 1_000_000, 0), 1.0)
        self.assertEqual(call_usd('claude-sonnet-5-5', 0, 1_000_000, 0, 0), 10.0)

    def test_a_model_with_no_price_gets_none_not_a_guess(self):
        self.assertIsNone(call_usd('claude-opus-4-1', 1000, 1000, 0, 0))
        self.assertIsNone(call_usd(None, 1000, 1000, 0, 0))
        msg = Message.objects.create(id=uuid.uuid4(), sender=self.magent, mood=self.mood, content='x',
                                     model_backend='<synthetic>', input_tokens=0, output_tokens=0)
        self.assertNotIn('usd', how_payload(msg))

    def test_the_payload_carries_it(self):
        msg = Message.objects.create(id=uuid.uuid4(), sender=self.magent, mood=self.mood, content='x',
                                     model_backend='claude-opus-5-5', input_tokens=2, output_tokens=456,
                                     cache_read_input_tokens=501_358, cache_creation_input_tokens=50_680)
        how = how_payload(msg)
        self.assertEqual((how['out'], how['ctx'], how['cached']), (456, 552_040, 501_358))
        self.assertAlmostEqual(how['usd'], (2 * 4 + 456 * 20 + 501_358 * 0.20 + 50_680 * 8) / 1e6, places=5)

    def test_a_silent_reply_says_what_it_cost(self):
        Message.objects.create(id=uuid.uuid4(), sender=self.magent, mood=self.mood,
                               content=[{'type': 'text', 'text': '<silent>nothing new</silent>'}], stop_reason='end_turn',
                               model_backend='claude-opus-5-5', input_tokens=2, output_tokens=35,
                               cache_read_input_tokens=0, cache_creation_input_tokens=420_158)
        [quiet] = self.client.get('/api/moods/m26/turns/').json()['quiet']
        self.assertEqual(quiet['reason'], 'nothing new')
        self.assertAlmostEqual(quiet['usd'], 3.36198, places=4)


class WakeToolsTest(TestCase):
    """Which tools an agent's turn had: its wake says, and a terminal has them all."""

    @classmethod
    def setUpTestData(cls):
        cls.magent = ThinkingEntity.objects.create(name='magent', is_biological_human=False)
        cls.poller = ThinkingEntity.objects.create(name='mood-poller', is_biological_human=False)
        cls.mood = Mood.objects.create(slug='m26')

    def turn(self, session, wake=None, reply='<silent>nothing new</silent>'):
        if wake is not None:
            Message.objects.create(id=uuid.uuid4(), sender=self.poller, mood=self.mood, session_id=session,
                                   content=f'<mood-wake mood="m26" reason="quiet">\nquiet.\n{wake}\n</mood-wake>')
        Message.objects.create(id=uuid.uuid4(), sender=self.magent, mood=self.mood, session_id=session,
                               content=[{'type': 'text', 'text': reply}], stop_reason='end_turn')

    def test_each_turn_says_whether_it_could_only_look(self):
        from conversations.services.mood_view import wake_tools
        look, full, old, terminal = (uuid.uuid4() for _ in range(4))
        self.turn(look, 'This turn can look but not touch: read and search files under ~/workspace.')
        self.turn(full, 'This turn has your full tools, as in your terminal.')
        self.turn(old, 'Answer in the Mood by replying normally.')  # from before a wake said
        self.turn(terminal)
        self.assertEqual(wake_tools({look, full, old, terminal, None}), {look: 'look', full: 'full', terminal: 'full'})

    def test_the_turns_endpoint_carries_it(self):
        self.turn(uuid.uuid4(), 'This turn can look but not touch.')
        self.turn(uuid.uuid4(), 'This turn has your full tools.', reply='Done: #171 is up.')
        body = self.client.get('/api/moods/m26/turns/').json()
        self.assertEqual([q.get('tools') for q in body['quiet']], ['look'])
        self.assertEqual([t.get('tools') for t in body['turns'] if t['sender'] == 'magent'], ['full'])
