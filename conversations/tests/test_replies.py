"""Replies: a post opening '↩ #m-<id>' answers that message and addresses its author."""

import json
import uuid

from django.core.cache import cache
from django.test import Client, TestCase

from conversations.models import Message, Mood, ThinkingEntity
from conversations.services import mood_auth


class RepliesTest(TestCase):

    def setUp(self):
        cache.clear()  # sign-in and posting are rate-limited, and the cache outlives a test
        self.justin = ThinkingEntity.objects.create(name='justin', is_biological_human=True)
        self.skyler = ThinkingEntity.objects.create(name='skyler', is_biological_human=True)
        self.magent = ThinkingEntity.objects.create(name='magent', is_biological_human=False)
        self.mood = Mood.objects.create(slug='general')
        self.sky_said = Message.objects.create(id=uuid.uuid4(), sender=self.skyler, mood=self.mood,
                                               content='Who has the capo tonight?', source_file='mood-web')
        self.i_said = Message.objects.create(id=uuid.uuid4(), sender=self.magent, mood=self.mood, stop_reason='end_turn',
                                             content=[{'type': 'text', 'text': 'The bus leaves at nine.'}])

    def client_for(self, entity, tier='key'):
        _, token = mood_auth.enrol_device(entity, 'test', tier=tier)
        client = Client()
        client.cookies[mood_auth.COOKIE] = token
        return client

    def reply(self, client, to, text):
        return client.post('/api/moods/general/say/', json.dumps({'text': f'↩ #m-{to.id}\n{text}'}),
                           content_type='application/json')

    def turn(self, message_id):
        turns = self.client.get('/api/moods/general/turns/').json()['turns']
        return next(t for t in turns if t['id'] == str(message_id))

    def test_a_reply_shows_what_it_answers_and_addresses_its_author(self):
        r = self.reply(self.client_for(self.justin), self.sky_said, 'I do!')
        self.assertEqual(r.status_code, 201)
        turn = self.turn(r.json()['id'])
        self.assertEqual(turn['reply'], {'id': str(self.sky_said.id), 'sender': 'skyler',
                                         'snippet': 'Who has the capo tonight?', 'is_human': True})
        self.assertEqual(turn['mentions'], ['skyler'])
        self.assertEqual(turn['html'], '<p>I do!</p>')  # the marker isn't shown; the page draws the quote

    def test_the_person_replied_to_is_notified(self):
        self.reply(self.client_for(self.justin), self.sky_said, 'I do!')
        notices = self.client.get('/api/notices/skyler/').json()['notices']
        self.assertIn(('mention', f'↩ #m-{self.sky_said.id}\nI do!'), [(n['kind'], n['turn']['text']) for n in notices])
        self.assertNotIn('mention', [n['kind'] for n in self.client.get('/api/notices/justin/').json()['notices']])

    def test_a_reply_to_an_agent_wakes_nobody_but_an_at_mention_still_does(self):
        justin = self.client_for(self.justin)
        quiet = self.reply(justin, self.i_said, 'Nine sharp?')
        self.assertEqual(self.turn(quiet.json()['id'])['mentions'], [])
        self.assertEqual(self.client.get('/api/mentions/magent/').json()['mentions'], [])
        loud = self.reply(justin, self.i_said, '@magent nine sharp?')
        mentions = self.client.get('/api/mentions/magent/').json()['mentions']
        self.assertEqual([m['turn']['id'] for m in mentions], [loud.json()['id']])

    def test_a_pickipedia_sign_in_can_reply_to_anyone_but_still_cant_mention_an_agent(self):
        wiki = self.client_for(self.skyler, tier='wiki')
        self.assertEqual(self.reply(wiki, self.i_said, 'Nine sharp?').status_code, 201)
        self.assertEqual(self.reply(wiki, self.sky_said, 'answering myself').status_code, 201)
        refused = self.reply(wiki, self.i_said, '@magent nine sharp?')
        self.assertEqual((refused.status_code, refused.json()['agents']), (403, ['magent']))

    def test_a_reply_to_something_gone_is_just_a_post(self):
        r = self.client_for(self.justin).post('/api/moods/general/say/', json.dumps(
            {'text': f'↩ #m-{uuid.uuid4()}\nstill here'}), content_type='application/json')
        turn = self.turn(r.json()['id'])
        self.assertIsNone(turn['reply'])
        self.assertEqual(turn['mentions'], [])

    def test_only_the_opening_marker_makes_a_reply(self):
        r = self.client_for(self.justin).post('/api/moods/general/say/', json.dumps(
            {'text': f'see ↩ #m-{self.sky_said.id} above'}), content_type='application/json')
        self.assertIsNone(self.turn(r.json()['id'])['reply'])

    def test_replying_to_yourself_addresses_nobody(self):
        r = self.reply(self.client_for(self.skyler), self.sky_said, 'and a spare capo')
        turn = self.turn(r.json()['id'])
        self.assertEqual(turn['reply']['sender'], 'skyler')  # still shown as a reply
        self.assertEqual(turn['mentions'], [])
