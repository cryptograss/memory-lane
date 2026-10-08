"""Emoji reactions: a nod, from any sign-in, taken back by pressing again; seen by everyone on their next look."""

import json
import uuid

from django.core.cache import cache
from django.test import Client, TestCase

from conversations.models import Message, Mood, ThinkingEntity
from conversations.services import mood_auth, reactions


class ReactionsTest(TestCase):

    def setUp(self):
        cache.clear()
        self.justin = ThinkingEntity.objects.create(name='justin', is_biological_human=True)
        self.skyler = ThinkingEntity.objects.create(name='skyler', is_biological_human=True)
        self.magent = ThinkingEntity.objects.create(name='magent', is_biological_human=False)
        self.mood = Mood.objects.create(slug='general', title='general')
        self.message = Message.objects.create(id=uuid.uuid4(), sender=self.magent, mood=self.mood, timestamp=1,
                                              content=[{'type': 'text', 'text': 'Deploy finished, all green.'}])

    def client_for(self, who, tier='key'):
        _, token = mood_auth.enrol_device(who, 'phone', tier=tier)
        client = Client()
        client.cookies[mood_auth.COOKIE] = token
        return client

    def react(self, client, emoji, message=None):
        return client.post(f'/api/moods/general/messages/{(message or self.message).id}/react/',
                           json.dumps({'emoji': emoji}), content_type='application/json')

    def test_on_and_off_from_either_sign_in(self):
        justin, skyler = self.client_for(self.justin), self.client_for(self.skyler, 'wiki')
        self.assertEqual(self.react(justin, '🎉').json(), {'on': True, 'reactions': {'🎉': ['justin']}})
        self.assertEqual(self.react(skyler, '🎉').json()['reactions'], {'🎉': ['justin', 'skyler']})
        self.react(skyler, '🪕')
        self.assertEqual(self.react(justin, '🎉').json(), {'on': False, 'reactions': {'🎉': ['skyler'], '🪕': ['skyler']}})

    def test_an_emoji_not_words_and_signed_in(self):
        justin = self.client_for(self.justin)
        for bad in ('lol', '<b>x</b>', '', ' ', '👍' * 20):
            self.assertEqual(self.react(justin, bad).status_code, 400, bad)
        self.assertEqual(self.react(Client(), '👍').status_code, 401)
        self.assertTrue(reactions.valid('👍🏽') and reactions.valid('❤️'))  # a skin tone, a variation selector

    def test_every_look_brings_them_and_they_are_never_a_message(self):
        self.react(self.client_for(self.skyler), '👍')
        page = Client().get('/api/moods/general/turns/').json()
        self.assertEqual(page['reactions'], {str(self.message.id): {'👍': ['skyler']}})
        self.assertEqual([t['id'] for t in page['turns']], [str(self.message.id)])
        self.assertEqual(page['events'], [])
