"""▶ starts at once: a message is read in pieces, the first short, each stitched onto the ones before."""

import json
import uuid
from unittest import mock

from django.core.cache import cache
from django.test import Client, TestCase, override_settings

from conversations.models import Message, Mood, ThinkingEntity
from conversations.services import mood_auth, voice
from conversations.tests.test_voice import MP3, VOICES

SENTENCE = 'The bus leaves the lot at nine, with the banjo in the back. '


class Made:
    def __init__(self, n):
        self.status_code, self.content, self.headers = 200, MP3 + bytes([n]), {'request-id': f'req-{n}'}


class FakeEleven:
    def __init__(self):
        self.made = []

    def get(self, url, **kw):
        answer = mock.Mock(status_code=200)
        answer.json.return_value = VOICES
        return answer

    def post(self, url, **kw):
        self.made.append(kw['json'])
        return Made(len(self.made))


class PiecesTest(TestCase):

    def test_a_short_first_piece_then_a_middling_one_then_the_rest(self):
        script = '\n\n'.join(f'Paragraph {n}. ' + SENTENCE * 6 for n in range(1, 6))  # ~380 characters each
        parts = voice.pieces(script)
        self.assertTrue(voice.FIRST_PIECE_MIN <= len(parts[0]) <= voice.FIRST_PIECE_CHARS)
        self.assertLessEqual(len(parts[1]), voice.SECOND_PIECE_CHARS)
        self.assertTrue(parts[0].startswith('Paragraph 1.') and parts[1].startswith('Paragraph 2.'))

    def test_a_one_line_opening_borrows_from_the_next_paragraph(self):
        script = 'Here is where things stand.\n\n' + SENTENCE * 12
        first = voice.pieces(script)[0]
        self.assertTrue(first.startswith('Here is where things stand.\n\nThe bus'))
        self.assertTrue(voice.FIRST_PIECE_MIN <= len(first) <= voice.FIRST_PIECE_CHARS)
        self.assertTrue(first.endswith('back.'))

    def test_short_paragraphs_together_are_one_piece(self):
        script = 'Shipped.\n\nSecond paragraph here.\n\nThird, about the deploy.'
        self.assertEqual(voice.pieces(script), [script])

    def test_a_long_first_paragraph_is_cut_where_a_sentence_ends(self):
        first = SENTENCE * 12  # ~720 characters
        parts = voice.pieces(first)
        self.assertLessEqual(len(parts[0]), voice.FIRST_PIECE_CHARS)
        self.assertTrue(parts[0].endswith('back.'))
        self.assertEqual(' '.join(parts), first.strip())

    def test_no_piece_runs_past_its_length_and_nothing_is_lost(self):
        script = '\n\n'.join(SENTENCE * 8 for _ in range(10))
        parts = voice.pieces(script)
        self.assertTrue(all(len(p) <= voice.PIECE_CHARS for p in parts))
        self.assertEqual(''.join(parts).replace(' ', '').replace('\n', ''), script.replace(' ', '').replace('\n', ''))

    def test_a_short_message_is_one_piece(self):
        self.assertEqual(voice.pieces('Soundcheck at five.'), ['Soundcheck at five.'])
        self.assertEqual(voice.pieces('  \n\n '), [])


@override_settings(ELEVENLABS_API_KEY='test-eleven-key')
class StitchedTest(TestCase):

    def setUp(self):
        cache.clear()
        self.magent = ThinkingEntity.objects.create(name='magent', is_biological_human=False)
        self.justin = ThinkingEntity.objects.create(name='justin', is_biological_human=True)
        self.mood = Mood.objects.create(slug='general', title='general')
        self.message = Message.objects.create(
            id=uuid.uuid4(), sender=self.magent, mood=self.mood, timestamp=1,
            content=[{'type': 'text', 'text': 'Shipped.\n\n' + '\n\n'.join(f'Paragraph {n}. ' + SENTENCE * 20 for n in (1, 2))}])

    def test_each_piece_is_made_knowing_the_ones_before_and_kept(self):
        fake = FakeEleven()
        first = voice.speak_piece(self.message, 'justin', http=fake)
        self.assertEqual(first['pieces'], 3)
        self.assertTrue(fake.made[0]['text'].startswith('Shipped.\n\nParagraph 1.'))
        self.assertNotIn('previous_request_ids', fake.made[0])
        voice.speak_piece(self.message, 'justin', http=fake, piece=1)
        voice.speak_piece(self.message, 'justin', http=fake, piece=2)
        self.assertEqual(fake.made[1]['previous_request_ids'], ['req-1'])
        self.assertEqual(fake.made[2]['previous_request_ids'], ['req-1', 'req-2'])
        again = voice.speak_piece(self.message, 'justin', http=fake, piece=1)
        self.assertEqual(len(fake.made), 3)  # kept: never paid twice
        self.assertTrue(again['url'].endswith('.mp3'))

    def test_the_endpoint_says_how_many_and_paces_only_a_start(self):
        _, token = mood_auth.enrol_device(self.justin, 'phone', tier='key')
        client = Client()
        client.cookies[mood_auth.COOKIE] = token
        fake = FakeEleven()
        url = f'/api/moods/general/speak/{self.message.id}/'
        with mock.patch('requests.post', fake.post), mock.patch('requests.get', fake.get):
            first = client.post(url).json()
            self.assertEqual(first['pieces'], 3)
            later = [client.post(f'{url}?piece={i}').json()['url'] for i in (1, 2)] * 4  # eight more asks
        self.assertEqual(len(fake.made), 3)
        self.assertEqual(len(set(later)), 2)
