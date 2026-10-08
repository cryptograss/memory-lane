"""An agent's turn says what it wrote, what each call read, and how much of that came from the cache."""

import time
import uuid

from django.test import TestCase

from conversations.models import Message, Mood, ThinkingEntity


class TurnUsageTest(TestCase):

    def test_usage_per_row(self):
        magent = ThinkingEntity.objects.create(name='magent', is_biological_human=False)
        mood = Mood.objects.create(slug='general', title='general')
        Message.objects.create(id=uuid.uuid4(), sender=magent, mood=mood, timestamp=int(time.time() * 1000),
                               content=[{'type': 'text', 'text': 'Done.'}], stop_reason='end_turn',
                               output_tokens=120, input_tokens=30, cache_read_input_tokens=9000,
                               cache_creation_input_tokens=500)
        turn = self.client.get('/api/moods/general/turns/').json()['turns'][-1]
        self.assertEqual((turn['out'], turn['ctx'], turn['cached']), (120, 9530, 9000))
