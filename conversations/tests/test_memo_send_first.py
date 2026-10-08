"""A voice memo is ready to send the moment it's stopped; it's posted when its words are in.

Scribe starts on it at the stop (voice.hear_later), so its words are usually
in by the send; if not, the send is answered at once and the memo posted when
they are -- with them, so a spoken "Magent, ..." wakes as a typed one does.
"""

import json
from unittest import mock

from django.core.cache import cache
from django.test import Client, TestCase, override_settings

from conversations.models import Message, Mood, ThinkingEntity
from conversations.services import mood_auth, voice
from conversations.tests.test_voice import WEBM

INLINE = mock.patch('conversations.services.voice._in_background', lambda fn, *a: fn(*a))
HEARD = {'text': 'Magent, bring the capo to soundcheck.', 'seconds': 4.0, 'language': 'eng'}


@override_settings(ELEVENLABS_API_KEY='test-eleven-key')
class MemoSendFirstTest(TestCase):

    def setUp(self):
        cache.clear()
        self.justin = ThinkingEntity.objects.create(name='justin', is_biological_human=True)
        self.skyler = ThinkingEntity.objects.create(name='skyler', is_biological_human=True)
        ThinkingEntity.objects.create(name='magent', is_biological_human=False)
        Mood.objects.create(slug='general', title='general')

    def client_for(self, who, tier='key'):
        _, token = mood_auth.enrol_device(who, 'phone', tier=tier)
        client = Client()
        client.cookies[mood_auth.COOKIE] = token
        return client

    def record(self, client, transcribe=None):
        with mock.patch('conversations.services.voice.transcribe', transcribe or mock.Mock(return_value=dict(HEARD))), INLINE:
            return client.post('/api/moods/general/memo/', WEBM, content_type='audio/webm').json()['url']

    def send(self, client, text):
        return client.post('/api/moods/general/say/', json.dumps({'text': text}), content_type='application/json')

    def posted(self):
        return Message.objects.get(source_file='mood-web').content

    def test_heard_by_the_send_it_goes_at_once_with_its_words(self):
        client = self.client_for(self.justin)
        url = self.record(client)
        sent = self.send(client, f'🎙 [voice memo · 0:04]({url})\n\nfrom the bus')
        self.assertEqual(sent.status_code, 201)
        self.assertEqual(self.posted(), f'🎙 [voice memo · 0:04]({url})\n\n@magent, bring the capo to soundcheck.\n\nfrom the bus')

    def test_not_heard_yet_the_send_is_answered_now_and_the_memo_posted_when_it_is(self):
        client = self.client_for(self.justin)
        url = self.record(client, transcribe=mock.Mock(side_effect=AssertionError('not yet')))
        sha = url.rsplit('/', 1)[1].split('.')[0]
        cache.delete(f'memo:heard:{sha}')
        cache.set(f'memo:hearing:{sha}', 1, 60)  # Scribe still at it

        def scribe_finishes(seconds):
            cache.set(f'memo:heard:{sha}', {'text': 'Soundcheck at five.'}, 60)
        with mock.patch('conversations.services.voice.time.sleep', scribe_finishes), \
                mock.patch('conversations.services.voice._in_background', lambda fn, *a: fn(*a)):
            sent = self.send(client, f'🎙 [voice memo · 0:04]({url})')
        self.assertEqual((sent.status_code, sent.json()), (202, {'pending': sha}))
        self.assertEqual(self.posted(), f'🎙 [voice memo · 0:04]({url})\n\nSoundcheck at five.')

    def test_a_memo_scribe_couldnt_hear_still_goes_saying_so(self):
        client = self.client_for(self.justin)
        url = self.record(client, transcribe=mock.Mock(side_effect=voice.VoiceError('ElevenLabs answered 500: down')))
        self.assertEqual(self.send(client, f'🎙 [voice memo · 0:04]({url})').status_code, 201)
        self.assertIn('(not transcribed: ElevenLabs answered 500: down)', self.posted())

    def test_from_a_wiki_sign_in_an_agents_name_said_stays_a_name(self):
        client = self.client_for(self.skyler, tier='wiki')
        url = self.record(client, transcribe=mock.Mock(return_value={'text': 'Magent, ask at Justin about the bus.'}))
        self.assertEqual(self.send(client, f'🎙 [voice memo · 0:04]({url})').status_code, 201)
        self.assertIn('Magent, ask @justin about the bus.', self.posted())

    def test_heard_once_however_often_its_stopped_and_sent(self):
        client = self.client_for(self.justin)
        scribe = mock.Mock(return_value=dict(HEARD))
        self.record(client, transcribe=scribe)
        url = self.record(client, transcribe=scribe)  # the same recording again
        self.assertEqual(scribe.call_count, 1)
        text = f'🎙 [voice memo · 0:04]({url})\n\n@magent, bring the capo to soundcheck.'
        self.assertIsNone(voice.memo_in(text))  # its words are there already: posted as written

    def test_a_memo_link_from_before_is_posted_as_written(self):
        client = self.client_for(self.justin)
        text = '🎙 [voice memo · 0:04](/moods/media/' + 'a' * 64 + '.webm)\n\nwords from before'
        self.assertEqual(self.send(client, text).status_code, 201)
        self.assertEqual(self.posted(), text)
