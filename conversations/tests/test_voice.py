"""Voice: memos transcribed, messages read aloud -- ElevenLabs faked, everything else real."""

import json
import uuid
from unittest import mock, skipUnless

from django.core.cache import cache
from django.test import TestCase, override_settings

from conversations.models import Media, Message, Motion, ThinkingEntity
from conversations.services import voice
from conversations.tests.test_motion_devices import HAS_SSH_KEYGEN, SignedInCase

MP3 = b'ID3\x04\x00\x00\x00\x00\x00\x00' + b'\x00' * 64
WEBM = b'\x1a\x45\xdf\xa3' + b'\x00' * 64
VOICES = {'voices': [{'name': 'Aria', 'voice_id': 'v-aria', 'labels': {'accent': 'american'}},
                     {'name': 'George - warm storyteller', 'voice_id': 'v-george'}]}


class Answer:
    def __init__(self, status=200, body=None, content=b''):
        self.status_code, self._body, self.content = status, body, content
        self.text = json.dumps(body) if body is not None else ''

    def json(self):
        return self._body


class FakeEleven:
    """What ElevenLabs answers, and what it was asked."""

    def __init__(self, stt_status=200):
        self.asked = []
        self.stt_status = stt_status

    def get(self, url, **kw):
        self.asked.append(('GET', url, kw))
        return Answer(body=VOICES)

    def post(self, url, **kw):
        self.asked.append(('POST', url, kw))
        if url.endswith('/speech-to-text'):
            if self.stt_status != 200 and 'keyterms' in kw['data']:
                return Answer(self.stt_status, {'detail': {'message': 'keyterms: invalid'}})
            return Answer(body={'text': 'Bring the capo to soundcheck.', 'audio_duration_secs': 42.0,
                                'language_code': 'eng'})
        return Answer(content=MP3)


class DirectionTest(TestCase):

    def test_a_voice_block_is_split_off_with_its_settings(self):
        text = ('Shipped today.\n\n```voice\nvoice: George\nstability: 0.4\nspeed: 3\nmood: grand\n---\n'
                '[warmly] Shipped today. [pause] Rest well.\n```\n')
        rest, direction = voice.split_voice(text)
        self.assertEqual(rest, 'Shipped today.')
        self.assertEqual(direction['voice'], 'George')
        self.assertEqual(direction['settings'], {'stability': 0.4})  # speed out of range, mood unknown: dropped
        self.assertEqual(direction['script'], '[warmly] Shipped today. [pause] Rest well.')

    def test_a_block_with_no_settings_is_all_script(self):
        rest, direction = voice.split_voice('Hi.\n```voice\n[whispers] hi\n```')
        self.assertEqual((rest, direction['script'], direction['settings']), ('Hi.', '[whispers] hi', {}))
        self.assertEqual(voice.split_voice('no block here'), ('no block here', None))

    def test_plain_reading_drops_markdown_and_names_code_and_links(self):
        said = voice.plain('## Done\n- **Merged** [#69](https://github.com/x/69)\n```py\nx = 1\n```\n'
                           'See https://example.com and `make test`. [[Banjo]] too.')
        self.assertEqual(said, 'Done\nMerged #69\n (code) \nSee (a link) and make test. Banjo too.')


@override_settings(ELEVENLABS_API_KEY='test-eleven-key')
class SpeakAndTranscribeTest(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.magent = ThinkingEntity.objects.create(name='magent', is_biological_human=False)
        ThinkingEntity.objects.create(name='justin', is_biological_human=True)
        cls.motion = Motion.objects.create(slug='m26', title='magenta-interface')

    def setUp(self):
        cache.clear()

    def say(self, text):
        return Message.objects.create(id=uuid.uuid4(), sender=self.magent, motion=self.motion,
                                      content=[{'type': 'text', 'text': text}], timestamp=1)

    def test_spoken_once_then_kept(self):
        fake = FakeEleven()
        message = self.say('Hello **there**.\n```voice\nvoice: george\nstyle: 0.3\n---\n[warmly] Hello there.\n```')
        url = voice.speak(message, 'justin', http=fake)
        self.assertTrue(url.endswith('.mp3'))
        _, where, sent = fake.asked[-1]
        self.assertTrue(where.endswith('/text-to-speech/v-george'))  # "george" matched "George - warm storyteller"
        self.assertEqual(sent['json'], {'text': '[warmly] Hello there.', 'model_id': 'eleven_v4',
                                        'voice_settings': {'style': 0.3}})
        self.assertEqual(sent['headers'], {'xi-api-key': 'test-eleven-key'})
        asked = len(fake.asked)
        self.assertEqual(voice.speak(message, 'justin', http=fake), url)  # the same again: not paid twice
        self.assertEqual(len(fake.asked), asked)
        row = Message.objects.get(source_file='voice')
        self.assertEqual((row.content['type'], row.content['by'], row.content['chars']), ('spoken', 'justin', 21))

    def test_the_house_voice_then_the_first(self):
        from conversations.services import settings as knobs
        fake = FakeEleven()
        voice.speak(self.say('One.'), 'justin', http=fake)
        self.assertTrue(fake.asked[-1][1].endswith('/v-aria'))
        knobs.change('voice', 'George', by=None)
        voice.speak(self.say('Two.'), 'justin', http=fake)
        self.assertTrue(fake.asked[-1][1].endswith('/v-george'))

    def test_the_days_budget_holds(self):
        from conversations.services import settings as knobs
        knobs.change('voice_usd_per_day', 0.01)
        with self.assertRaises(voice.VoiceError) as caught:
            voice.speak(self.say('x' * 500), 'justin', http=FakeEleven())  # 500 chars: $0.04
        self.assertEqual(caught.exception.status, 429)

    def test_a_memo_is_transcribed_expecting_the_projects_words(self):
        from conversations.services import media
        fake = FakeEleven()
        memo = media.store(WEBM, audio=True)
        heard = voice.transcribe(memo, self.motion, 'justin', http=fake)
        self.assertEqual(heard, {'text': 'Bring the capo to soundcheck.', 'seconds': 42.0, 'language': 'eng'})
        _, where, sent = fake.asked[-1]
        self.assertEqual(sent['data']['model_id'], 'scribe_v2')
        for term in ('banjo', 'justin', 'magent', 'magenta-interface'):
            self.assertIn(term, sent['data']['keyterms'])
        self.assertEqual(sent['files']['file'][2], 'audio/webm')

    def test_keyterms_refused_it_asks_plainly(self):
        from conversations.services import media
        fake = FakeEleven(stt_status=422)
        heard = voice.transcribe(media.store(WEBM, audio=True), self.motion, 'justin', http=fake)
        self.assertEqual(heard['text'], 'Bring the capo to soundcheck.')
        self.assertNotIn('keyterms', fake.asked[-1][2]['data'])

    def test_no_key_no_voice(self):
        with override_settings(ELEVENLABS_API_KEY=''):
            with self.assertRaises(voice.VoiceError) as caught:
                voice.speak(self.say('hi'), 'justin', http=FakeEleven())
        self.assertEqual(caught.exception.status, 503)

    def test_audio_is_taken_only_where_asked_for(self):
        from conversations.services import media
        self.assertIsNone(media.store(WEBM))  # not an image
        stored = media.store(WEBM, audio=True)
        self.assertEqual((stored.mime, stored.url[-5:]), ('audio/webm', '.webm'))
        self.assertEqual(self.client.get(stored.url)['Content-Type'], 'audio/webm')


class PageTest(TestCase):

    def test_a_voice_block_is_performed_not_shown(self):
        magent = ThinkingEntity.objects.create(name='magent', is_biological_human=False)
        motion = Motion.objects.create(slug='m26')
        Message.objects.create(id=uuid.uuid4(), sender=magent, motion=motion, timestamp=1, stop_reason='end_turn',
                               content=[{'type': 'text', 'text': 'Done.\n```voice\n[sighs] Done.\n```'}])
        turn = self.client.get('/api/motions/m26/turns/').json()['turns'][0]
        self.assertEqual((turn['text'], turn['voiced']), ('Done.', True))
        self.assertNotIn('sighs', turn['html'])

    def test_a_memo_is_a_player_and_other_local_links_are_not(self):
        from conversations.services.motion_view import render_html
        sha = 'a' * 64
        self.assertEqual(render_html(f'🎙 [voice memo · 0:42](/motions/media/{sha}.webm)\n\nBring the capo.'),
                         f'<p>🎙 <span class="memo">voice memo · 0:42</span><audio controls preload="none" src="/motions/media/{sha}.webm" '
                         f'title="voice memo · 0:42"></audio></p>\n<p>Bring the capo.</p>')
        self.assertNotIn('<audio', render_html(f'[x](/motions/media/{sha}.exe)'))
        self.assertNotIn('<audio', render_html('[x](javascript:alert(1)) [y](/motions/media/zz.mp3)'))


@skipUnless(HAS_SSH_KEYGEN, 'needs ssh-keygen')
@override_settings(ELEVENLABS_API_KEY='test-eleven-key')
class VoiceEndpointsTest(SignedInCase):

    def post(self, client, url, body=b'', content_type='application/octet-stream'):
        return client.post(url, body, content_type=content_type, HTTP_X_CSRFTOKEN=client.cookies['csrftoken'].value)

    def test_memo_and_speak_need_a_signed_in_device(self):
        self.assertEqual(self.client.post('/api/motions/m26/memo/', WEBM, content_type='audio/webm').status_code, 401)
        self.assertEqual(self.client.post(f'/api/motions/m26/speak/{uuid.uuid4()}/').status_code, 401)

    def test_a_memo_comes_back_as_audio_and_words(self):
        client = self.sign_in()
        with mock.patch('requests.post', FakeEleven().post), mock.patch('requests.get', FakeEleven().get):
            self.assertEqual(self.post(client, '/api/motions/m26/memo/', b'not audio').status_code, 400)
            heard = self.post(client, '/api/motions/m26/memo/', WEBM).json()
        self.assertEqual(heard['text'], 'Bring the capo to soundcheck.')
        self.assertTrue(heard['url'].endswith('.webm'))
        self.assertTrue(Media.objects.filter(mime='audio/webm').exists())

    def test_speak_reads_a_message_of_this_mood_only(self):
        client = self.sign_in()
        magent = ThinkingEntity.objects.get(name='magent')
        here = Message.objects.create(id=uuid.uuid4(), sender=magent, motion_id='m26', timestamp=1,
                                      content=[{'type': 'text', 'text': 'Soundcheck at five.'}])
        with mock.patch('requests.post', FakeEleven().post), mock.patch('requests.get', FakeEleven().get):
            spoken = self.post(client, f'/api/motions/m26/speak/{here.id}/').json()
            Motion.objects.create(slug='other')
            elsewhere = self.post(client, f'/api/motions/other/speak/{here.id}/')
        self.assertTrue(spoken['url'].endswith('.mp3'))
        self.assertEqual(elsewhere.status_code, 404)

    def test_voices_listed_with_todays_spend(self):
        with mock.patch('requests.get', FakeEleven().get):
            listed = self.client.get('/api/voice/voices/').json()
        self.assertEqual([v['name'] for v in listed['voices']], ['Aria', 'George - warm storyteller'])
        self.assertEqual((listed['model'], listed['spent_today_usd']), ('eleven_v4', 0))
        with override_settings(ELEVENLABS_API_KEY=''):
            self.assertEqual(self.client.get('/api/voice/voices/').json(), {'enabled': False, 'voices': []})
