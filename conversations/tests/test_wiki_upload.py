"""A picture from a Mood onto PickiPedia: by choice, named, licensed, credited; never twice, never over another."""

import json
import uuid
from unittest import mock

from django.core.cache import cache
from django.test import Client, TestCase, override_settings

from conversations.models import Message, Mood, ThinkingEntity
from conversations.services import media, mood_auth, wiki_upload

PNG = b'\x89PNG\r\n\x1a\n' + b'\x00' * 64
BOT = {'WIKI_UPLOAD_USERNAME': 'Magent@uploads', 'WIKI_UPLOAD_PASSWORD': 'pw'}


class Answer:
    def __init__(self, body):
        self.body = body

    def json(self):
        return self.body


class FakeWiki:
    """MediaWiki's action API as far as signing in and uploading go."""

    def __init__(self, upload=None):
        self.headers, self.uploads = {}, []
        self.answer = upload or (lambda name: {'result': 'Success', 'filename': name.replace(' ', '_')})

    def get(self, url, params=None, timeout=None):
        return Answer({'query': {'tokens': {'logintoken': 'L', 'csrftoken': 'C'}}})

    def post(self, url, data=None, files=None, timeout=None):
        if data['action'] == 'login':
            ok = (data['lgname'], data['lgpassword']) == ('Magent@uploads', 'pw')
            return Answer({'login': {'result': 'Success' if ok else 'Failed'}})
        self.uploads.append((data, files))
        return Answer({'upload': self.answer(data['filename'])})


@override_settings(**BOT)
class WikiUploadTest(TestCase):

    def setUp(self):
        cache.clear()
        self.justin = ThinkingEntity.objects.create(name='justin', is_biological_human=True)
        self.mood = Mood.objects.create(slug='general', title='general')
        self.picture = media.store(PNG)
        self.message = Message.objects.create(id=uuid.uuid4(), sender=self.justin, mood=self.mood, timestamp=1,
                                              content=f'look ![image]({self.picture.url})', source_file='mood-web')

    def put(self, wiki, name='Hillberry jam', rights='cc-by-sa-4.0'):
        return wiki_upload.to_pickipedia(self.picture, name, 'Saturday night, the porch', rights, 'justin',
                                         self.mood, self.message.id, http=wiki)

    def test_it_goes_up_named_described_licensed_credited_and_the_mood_says_so(self):
        wiki = FakeWiki()
        done = self.put(wiki)
        self.assertEqual((done['file'], done['new']), ('Hillberry jam.png', True))
        sent, files = wiki.uploads[0]
        self.assertEqual((sent['filename'], files['file'][2]), ('Hillberry jam.png', 'image/png'))
        self.assertIn('Saturday night, the porch', sent['text'])
        self.assertIn(f'https://magenta.cryptograss.live/moods/general/#m-{self.message.id}', sent['text'])
        self.assertIn('CC BY-SA 4.0', sent['text'])
        self.assertIn('[[Category:From magenta]]', sent['text'])
        events = Client().get('/api/moods/general/turns/').json()['events']
        event = next(e for e in events if e['type'] == 'wiki-upload')
        self.assertEqual((event['by'], event['file'], event['sha']), ('justin', 'Hillberry jam.png', self.picture.sha256))

    def test_credited_by_their_pickipedia_name(self):
        wiki = FakeWiki()
        with mock.patch('conversations.services.wiki_auth.names', return_value={'justin': 'JMyles'}):
            self.put(wiki)
        self.assertIn('Shared by [[User:JMyles|JMyles]] in magenta', wiki.uploads[0][0]['text'])

    def test_never_twice(self):
        wiki = FakeWiki()
        self.put(wiki)
        again = self.put(wiki, name='Another name')
        self.assertEqual((again['file'], again['new'], len(wiki.uploads)), ('Hillberry jam.png', False, 1))

    def test_the_very_same_picture_already_there_is_that_one(self):
        done = self.put(FakeWiki(lambda name: {'result': 'Warning', 'warnings': {'duplicate': ['Old_jam.png']}}))
        self.assertEqual((done['file'], done['page']), ('Old jam.png', 'https://pickipedia.xyz/wiki/File:Old_jam.png'))

    def test_a_taken_name_is_refused_never_overwritten(self):
        with self.assertRaises(wiki_upload.UploadError) as caught:
            self.put(FakeWiki(lambda name: {'result': 'Warning', 'warnings': {'exists': name}}))
        self.assertEqual(caught.exception.status, 409)
        self.assertFalse(Message.objects.filter(source_file='wiki-upload').exists())

    def test_a_name_pickipedia_will_take(self):
        self.assertEqual(wiki_upload.file_name('  jam: [Saturday] / porch.JPG ', 'image/jpeg'), 'Jam Saturday porch.jpg')
        with self.assertRaises(wiki_upload.UploadError):
            wiki_upload.file_name('', 'image/png')
        with self.assertRaises(wiki_upload.UploadError):
            wiki_upload.file_name('memo', 'audio/webm')


@override_settings(**BOT)
class WikiUploadEndpointTest(TestCase):

    def setUp(self):
        cache.clear()
        self.justin = ThinkingEntity.objects.create(name='justin', is_biological_human=True)
        Mood.objects.create(slug='general', title='general')
        self.picture = media.store(PNG)

    def client_for(self, tier):
        _, token = mood_auth.enrol_device(self.justin, 'laptop', tier=tier)
        client = Client()
        client.cookies[mood_auth.COOKIE] = token
        return client

    def ask(self, client):
        return client.post(f'/api/media/{self.picture.sha256}/pickipedia/', json.dumps(
            {'mood': 'general', 'name': 'Jam', 'description': '', 'rights': 'ask'}), content_type='application/json')

    def test_an_ssh_key_sign_in_only_and_only_when_set_up(self):
        self.assertEqual(self.ask(Client()).status_code, 401)
        self.assertEqual(self.ask(self.client_for('wiki')).status_code, 403)
        with override_settings(WIKI_UPLOAD_USERNAME=''):
            self.assertEqual(self.ask(self.client_for('key')).status_code, 503)
        with mock.patch('requests.Session', FakeWiki):
            self.assertEqual(self.ask(self.client_for('key')).status_code, 201)

    def test_the_page_offers_it_only_when_set_up(self):
        self.assertIn('const wikiUpload = true;', Client().get('/moods/').content.decode())
        with override_settings(WIKI_UPLOAD_USERNAME=''):
            self.assertIn('const wikiUpload = false;', Client().get('/moods/').content.decode())
