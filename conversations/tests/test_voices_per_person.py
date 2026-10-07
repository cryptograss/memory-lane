"""Each speaker in a voice of their own, the narrator saying who speaks, and mentions said aloud."""

import json
import uuid
from unittest import mock

from django.core.cache import cache
from django.test import Client, TestCase, override_settings

from conversations.models import Message, Mood, Setting, ThinkingEntity
from conversations.services import mood_auth, voice
from conversations.tests.test_voice import MP3, Answer, FakeEleven

QUARTET = {'voices': [{'name': name, 'voice_id': f'v-{name.lower()}', 'preview_url': f'https://x/{name}.mp3'}
                      for name in ('Aria', 'Bill', 'Clyde', 'Dolly')]}


class FakeQuartet(FakeEleven):
    def get(self, url, **kw):
        self.asked.append(('GET', url, kw))
        return Answer(body=QUARTET)


@override_settings(ELEVENLABS_API_KEY='test-eleven-key')
class SpeakersTest(TestCase):

    def setUp(self):
        cache.clear()
        self.magent = ThinkingEntity.objects.create(name='magent', is_biological_human=False)
        self.justin = ThinkingEntity.objects.create(name='justin', is_biological_human=True)
        self.skyler = ThinkingEntity.objects.create(name='skyler', is_biological_human=True)
        self.mood = Mood.objects.create(slug='magenta-interface', title='magenta-interface')
        self.fake = FakeQuartet()

    def said(self, who, text):
        return Message.objects.create(id=uuid.uuid4(), sender=who, mood=self.mood, timestamp=1,
                                      content=text if who.is_biological_human else [{'type': 'text', 'text': text}])

    def voice_used(self):
        return self.fake.asked[-1][1].rsplit('/', 1)[-1]

    def test_each_person_gets_a_voice_of_their_own_and_keeps_it(self):
        voice.speak(self.said(self.magent, 'Shipped.'), 'justin', http=self.fake)
        self.assertEqual(self.voice_used(), 'v-aria')  # the narrator: the house voice (here, the first)
        voice.speak(self.said(self.justin, 'Nice.'), 'justin', http=self.fake)
        justins = self.voice_used()
        voice.speak(self.said(self.skyler, 'Agreed.'), 'justin', http=self.fake)
        skylers = self.voice_used()
        self.assertEqual(len({'v-aria', justins, skylers}), 3)  # nobody sounds like anybody else
        self.assertEqual(voice.chosen_voices(), {'justin': justins, 'skyler': skylers})
        given = Setting.objects.get(key='speaker_voice', agent=self.justin)
        self.assertEqual((given.set_by, given.note), (None, 'given at their first reading aloud'))
        voice.speak(self.said(self.justin, 'Again.'), 'justin', http=self.fake)
        self.assertEqual(self.voice_used(), justins)  # tomorrow too
        self.assertEqual(Setting.objects.filter(key='speaker_voice').count(), 2)  # given once

    def test_a_chosen_voice_wins_and_a_directed_one_wins_over_that(self):
        from conversations.services import settings as knobs
        knobs.change('speaker_voice', 'Dolly', agent=self.justin, by=self.justin)
        voice.speak(self.said(self.justin, 'Hi.'), 'justin', http=self.fake)
        self.assertEqual(self.voice_used(), 'v-dolly')
        voice.speak(self.said(self.magent, 'Hi.\n```voice\nvoice: Clyde\n---\nHi.\n```'), 'justin', http=self.fake)
        self.assertEqual(self.voice_used(), 'v-clyde')

    def test_the_narrator_says_who_speaks_once_per_wording(self):
        message = self.said(self.skyler, 'Bus at nine.')
        url = voice.intro(message, 'justin', http=self.fake)
        self.assertEqual((self.voice_used(), self.fake.asked[-1][2]['json']['text']), ('v-aria', 'Skyler says:'))
        asked = len(self.fake.asked)
        self.assertEqual(voice.intro(self.said(self.skyler, 'And a capo.'), 'justin', http=self.fake), url)
        self.assertEqual(len(self.fake.asked), asked)  # the same words: made once
        voice.intro(message, 'justin', where=True, http=self.fake)
        self.assertEqual(self.fake.asked[-1][2]['json']['text'], 'In magenta interface, Skyler says:')
        row = Message.objects.filter(source_file='voice', content__intro='skyler').first()
        self.assertEqual((row.content['type'], row.content['by']), ('spoken', 'justin'))

    def test_a_narrator_with_a_voice_of_its_own_introduces_in_it(self):
        from conversations.services import settings as knobs
        knobs.change('speaker_voice', 'Bill', agent=self.magent, by=self.justin)
        voice.intro(self.said(self.skyler, 'Hi.'), 'justin', http=self.fake)
        self.assertEqual(self.voice_used(), 'v-bill')

    def test_without_the_list_everyone_is_the_house_voice_and_nothing_is_kept(self):
        class Closed(FakeEleven):
            def get(self, url, **kw):
                return Answer(500, {'detail': 'down'})
        fake = Closed()
        with self.assertRaises(voice.VoiceError):
            voice.speak(self.said(self.justin, 'Hi.'), 'justin', http=fake)  # as before: the list is down
        self.assertFalse(Setting.objects.filter(key='speaker_voice').exists())


@override_settings(ELEVENLABS_API_KEY='test-eleven-key')
class SpeakerEndpointsTest(TestCase):

    def setUp(self):
        cache.clear()
        self.magent = ThinkingEntity.objects.create(name='magent', is_biological_human=False)
        self.justin = ThinkingEntity.objects.create(name='justin', is_biological_human=True)
        self.skyler = ThinkingEntity.objects.create(name='skyler', is_biological_human=True)
        self.mood = Mood.objects.create(slug='general', title='general')

    def client_for(self, entity, tier='key'):
        _, token = mood_auth.enrol_device(entity, 'test', tier=tier)
        client = Client()
        client.cookies[mood_auth.COOKIE] = token
        return client

    def choose(self, client, name, chosen):
        return client.post('/api/voice/speaker/', json.dumps({'name': name, 'voice': chosen}),
                           content_type='application/json')

    def test_your_own_voice_from_any_sign_in_anyone_elses_with_your_key(self):
        fake = FakeQuartet()
        wiki = self.client_for(self.skyler, tier='wiki')
        with mock.patch('requests.get', fake.get):
            self.assertEqual(self.choose(wiki, 'skyler', 'Dolly').status_code, 201)
            self.assertEqual(self.choose(wiki, 'justin', 'Bill').status_code, 403)
            self.assertEqual(self.choose(wiki, 'magent', 'Bill').status_code, 403)
            key = self.client_for(self.justin)
            self.assertEqual(self.choose(key, 'magent', 'Bill').status_code, 201)
            self.assertEqual(self.choose(key, 'justin', 'Nobody').status_code, 400)
            self.assertEqual(self.choose(key, 'ghost', 'Bill').status_code, 404)
            self.assertEqual(Client().post('/api/voice/speaker/', '{}', content_type='application/json').status_code, 401)
            listed = Client().get('/api/voice/voices/').json()
        self.assertEqual(listed['speakers'], {'skyler': 'Dolly', 'magent': 'Bill'})
        self.assertEqual(listed['narrator'], 'magent')
        self.assertEqual(listed['voices'][0]['preview_url'], 'https://x/Aria.mp3')

    def test_speak_can_say_who_speaks_first_or_only_that(self):
        fake = FakeQuartet()
        message = Message.objects.create(id=uuid.uuid4(), sender=self.skyler, mood=self.mood, timestamp=1,
                                         content='Bus at nine.')
        client = self.client_for(self.justin)
        with mock.patch('requests.get', fake.get), mock.patch('requests.post', fake.post):
            both = client.post(f'/api/moods/general/speak/{message.id}/?intro=where').json()
            texts = [kw['json']['text'] for method, _, kw in fake.asked if method == 'POST']
            only = client.post(f'/api/moods/general/speak/{message.id}/?only=intro').json()
        self.assertEqual(set(both), {'intro', 'url'})
        self.assertEqual(texts, ['In general, Skyler says:', 'Bus at nine.'])
        self.assertEqual(set(only), {'intro'})

    def test_a_memo_that_names_someone_is_posted_mentioning_them(self):
        from conversations.tests.test_voice import WEBM
        client = self.client_for(self.justin)
        heard = {'text': 'Hey magnet, can you ask at skyler about the bus?', 'seconds': 3.0, 'language': 'eng'}
        with mock.patch('conversations.services.voice.transcribe', return_value=heard), \
                mock.patch('conversations.services.voice._in_background', lambda fn, *a: fn(*a)):
            url = client.post('/api/moods/general/memo/', WEBM, content_type='audio/webm').json()['url']
            client.post('/api/moods/general/say/', json.dumps({'text': f'🎙 [voice memo · 0:03]({url})'}),
                        content_type='application/json')
        posted = Message.objects.get(source_file='mood-web').content
        self.assertIn('Hey @magent, can you ask @skyler about the bus?', posted)


class SpokenMentionsTest(TestCase):
    NAMES = {'magent', 'justin', 'skyler'}

    def heard(self, text, aliases=None):
        return voice.spoken_mentions(text, self.NAMES, aliases)

    def test_a_name_said_to_someone_becomes_a_mention(self):
        self.assertEqual(self.heard('Magent, can you check the deploy?'), '@magent, can you check the deploy?')
        self.assertEqual(self.heard('OK Justin I think it works'), 'OK @justin I think it works')
        self.assertEqual(self.heard('hey, Skyler: soundcheck at five'), 'hey, @skyler: soundcheck at five')
        self.assertEqual(self.heard('I asked at magent and at Skyler.'), 'I asked @magent and @skyler.')
        self.assertEqual(self.heard('Magnet, are you there?'), '@magent, are you there?')  # as Scribe may hear it
        self.assertEqual(self.heard('at JMyles see this', {'jmyles': 'justin'}), '@justin see this')

    def test_a_name_merely_said_is_left_alone(self):
        for text in ('Justin went to the store.', 'The magnet fell off.', 'That was what magent said.',
                     'Look at magenta-interface', 'cat skyler', 'Already @magent, hi'):
            self.assertEqual(self.heard(text), text)


class ReaderReportsTest(TestCase):

    def setUp(self):
        cache.clear()
        self.justin = ThinkingEntity.objects.create(name='justin', is_biological_human=True)
        Mood.objects.create(slug='general', title='general')

    def test_what_the_reader_did_is_kept_and_nothing_secret_with_it(self):
        _, token = mood_auth.enrol_device(self.justin, 'Pixel', tier='wiki')
        client = Client()
        client.cookies[mood_auth.COOKIE] = token
        said = client.post('/api/voice/heard/', json.dumps({
            'mood': 'general', 'message': 'a947485f', 'clip': 'memo', 'outcome': 'failed', 'hidden': True,
            'error': 'NotAllowedError: play() failed; token=ghp_0123456789abcdefghijklmnopqrstuvwxyz'}),
            content_type='application/json')
        self.assertEqual(said.status_code, 201)
        row = Message.objects.get(source_file='voice').content
        self.assertEqual((row['type'], row['by'], row['clip'], row['outcome'], row['hidden'], row['device']),
                         ('heard', 'justin', 'memo', 'failed', True, 'Pixel'))
        self.assertNotIn('ghp_', row['error'])
        self.assertEqual(client.post('/api/voice/heard/', json.dumps({'mood': 'general', 'outcome': 'exploded'}),
                                     content_type='application/json').status_code, 400)
        self.assertEqual(Client().post('/api/voice/heard/', '{}', content_type='application/json').status_code, 401)
