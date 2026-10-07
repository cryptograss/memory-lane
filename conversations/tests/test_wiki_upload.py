"""A picture from a Mood onto PickiPedia: by whoever shared it, as themselves, under the license they gave it.

Never twice, never over another, never by someone else, never as someone else.
"""

import hashlib
import json
import uuid
from unittest import mock
from urllib.parse import parse_qs, urlparse

from django.core.cache import cache
from django.test import Client, TestCase, override_settings

from conversations.models import Media, Message, Mood, ThinkingEntity
from conversations.services import media, mood_auth, wiki_upload

PNG = b'\x89PNG\r\n\x1a\n' + b'\x00' * 64
CONSUMER = {'PICKIPEDIA_UPLOAD_CLIENT_ID': 'uploads-id', 'PICKIPEDIA_UPLOAD_CLIENT_SECRET': 'uploads-secret'}
BACK = 'https://magenta.example/moods/auth/wiki/upload'


class Answer:
    def __init__(self, body, status=200):
        self.body, self.status_code = body, status

    def json(self):
        return self.body


class FakePickiPedia:
    """Its OAuth 2 endpoints and as much of its action API as uploading asks."""

    def __init__(self, user='JMyles', holding=None, taken=(), upload=None):
        self.user, self.holding, self.taken = user, holding or {}, set(taken)
        self.headers, self.uploads, self.exchanged = {}, [], []
        self.answer = upload or (lambda name: {'result': 'Success', 'filename': name.replace(' ', '_')})

    def get(self, url, params=None, timeout=None, headers=None):
        if url.endswith('/oauth2/resource/profile'):
            return Answer({'username': self.user, 'blocked': False})
        params = params or {}
        if params.get('list') == 'allimages':
            name = self.holding.get(params['aisha1'])
            return Answer({'query': {'allimages': [{'name': name}] if name else []}})
        if 'titles' in params:
            title = params['titles']
            return Answer({'query': {'pages': {'1' if title in self.taken else '-1':
                                               {'title': title} if title in self.taken else {'title': title, 'missing': ''}}}})
        return Answer({'query': {'tokens': {'csrftoken': 'C'}}})

    def post(self, url, data=None, files=None, timeout=None, headers=None):
        if url.endswith('/oauth2/access_token'):
            self.exchanged.append(data)
            ok = (data['client_id'], data['client_secret']) == ('uploads-id', 'uploads-secret')
            return Answer({'access_token': f'token-for-{self.user}'} if ok else {'error': 'invalid_client'}, 200 if ok else 401)
        self.uploads.append({'data': data, 'files': files, 'as': self.headers.get('Authorization')})
        return Answer({'upload': self.answer(data['filename'])})


@override_settings(**CONSUMER)
class WikiUploadTest(TestCase):

    def setUp(self):
        cache.clear()
        self.justin = ThinkingEntity.objects.create(name='justin', is_biological_human=True)
        self.skyler = ThinkingEntity.objects.create(name='skyler', is_biological_human=True)
        self.mood = Mood.objects.create(slug='general', title='general')
        self.picture = media.store(PNG, added_by=self.justin)
        self.message = Message.objects.create(id=uuid.uuid4(), sender=self.justin, mood=self.mood, timestamp=1,
                                              content=f'look ![image]({self.picture.url})', source_file='mood-web')
        names = mock.patch('conversations.services.wiki_auth.names', return_value={'justin': 'JMyles'})
        names.start()
        self.addCleanup(names.stop)

    def begin(self, wiki, by='justin', name='Hillberry jam', license='cc-by-sa-4.0'):
        return wiki_upload.begin(self.picture, name, 'Saturday night, the porch', license, by, self.mood,
                                 self.message.id, BACK, http=wiki)

    def test_it_goes_up_as_them_under_their_license_and_the_mood_says_so(self):
        wiki = FakePickiPedia()
        started = self.begin(wiki, license='cc0')
        asked = parse_qs(urlparse(started['go']).query)
        self.assertEqual((asked['client_id'], asked['redirect_uri'], asked['state']),
                         (['uploads-id'], [BACK], [started['state']]))
        self.assertEqual(wiki.uploads, [])  # nothing until PickiPedia's yes
        self.assertEqual(Media.objects.get(pk=self.picture.pk).license, 'cc0')
        done = wiki_upload.finish(started['state'], 'code-1', 'justin', BACK, http=wiki)
        self.assertEqual((done['file'], done['new'], done['back']),
                         ('Hillberry jam.png', True, f'/moods/general/#m-{self.message.id}'))
        sent = wiki.uploads[0]
        self.assertEqual(sent['as'], 'Bearer token-for-JMyles')  # as them, not a bot
        self.assertEqual((sent['data']['filename'], sent['files']['file'][2]), ('Hillberry jam.png', 'image/png'))
        self.assertIn('Saturday night, the porch', sent['data']['text'])
        self.assertIn(f'https://magenta.cryptograss.live/moods/general/#m-{self.message.id}', sent['data']['text'])
        self.assertIn('CC0', sent['data']['text'])
        self.assertIn('[[Category:From magenta]]', sent['data']['text'])
        events = Client().get('/api/moods/general/turns/').json()['events']
        event = next(e for e in events if e['type'] == 'wiki-upload')
        self.assertEqual((event['by'], event['file'], event['sha'], event['already']),
                         ('justin', 'Hillberry jam.png', self.picture.sha256, None))
        self.assertEqual(event['page'], 'https://pickipedia.xyz/wiki/File:Hillberry_jam.png')

    def test_only_whoever_shared_it(self):
        with self.assertRaises(wiki_upload.UploadError) as caught:
            self.begin(FakePickiPedia(), by='skyler')
        self.assertEqual(caught.exception.status, 403)

    def test_only_as_themselves(self):
        wiki = FakePickiPedia(user='SomeoneElse')
        started = self.begin(wiki)
        with self.assertRaises(wiki_upload.UploadError) as caught:
            wiki_upload.finish(started['state'], 'code-1', 'justin', BACK, http=wiki)
        self.assertEqual(caught.exception.status, 403)
        self.assertIn('signed in to PickiPedia as SomeoneElse', str(caught.exception))
        self.assertEqual(wiki.uploads, [])

    def test_a_yes_counts_once_and_only_for_whoever_asked(self):
        wiki = FakePickiPedia()
        started = self.begin(wiki)
        with self.assertRaises(wiki_upload.UploadError):
            wiki_upload.finish(started['state'], 'code-1', 'skyler', BACK, http=wiki)
        with self.assertRaises(wiki_upload.UploadError):  # spent by the try above
            wiki_upload.finish(started['state'], 'code-1', 'justin', BACK, http=wiki)
        self.assertEqual(wiki.uploads, [])

    def test_never_twice(self):
        wiki = FakePickiPedia()
        wiki_upload.finish(self.begin(wiki)['state'], 'code-1', 'justin', BACK, http=wiki)
        again = self.begin(wiki, name='Another name')
        self.assertEqual((again['file'], again['new'], len(wiki.uploads)), ('Hillberry jam.png', False, 1))

    def test_the_very_same_picture_on_the_wiki_already_is_that_one_and_nobody_is_sent_anywhere(self):
        wiki = FakePickiPedia(holding={hashlib.sha1(PNG).hexdigest(): 'Old_jam.png'})
        found = self.begin(wiki)
        self.assertEqual((found['file'], found['page'], found['new']),
                         ('Old jam.png', 'https://pickipedia.xyz/wiki/File:Old_jam.png', False))
        self.assertNotIn('go', found)
        self.assertTrue(Message.objects.get(source_file='wiki-upload').content['already'])

    def test_a_taken_name_is_refused_before_anyone_goes_and_never_overwritten(self):
        with self.assertRaises(wiki_upload.UploadError) as caught:
            self.begin(FakePickiPedia(taken={'File:Hillberry jam.png'}))
        self.assertEqual(caught.exception.status, 409)
        wiki = FakePickiPedia(upload=lambda name: {'result': 'Warning', 'warnings': {'exists': name}})
        with self.assertRaises(wiki_upload.UploadError) as caught:  # taken between asking and coming back
            wiki_upload.finish(self.begin(wiki)['state'], 'code-1', 'justin', BACK, http=wiki)
        self.assertEqual(caught.exception.status, 409)
        self.assertFalse(Message.objects.filter(source_file='wiki-upload').exists())

    def test_two_licenses_only(self):
        with self.assertRaises(wiki_upload.UploadError):
            self.begin(FakePickiPedia(), license='cc-by-4.0')

    def test_a_name_pickipedia_will_take(self):
        self.assertEqual(wiki_upload.file_name('  jam: [Saturday] / porch.JPG ', 'image/jpeg'), 'Jam Saturday porch.jpg')
        with self.assertRaises(wiki_upload.UploadError):
            wiki_upload.file_name('', 'image/png')
        with self.assertRaises(wiki_upload.UploadError):
            wiki_upload.file_name('memo', 'audio/webm')


@override_settings(**CONSUMER)
class EndpointsTest(TestCase):

    def setUp(self):
        cache.clear()
        self.skyler = ThinkingEntity.objects.create(name='skyler', is_biological_human=True)
        self.justin = ThinkingEntity.objects.create(name='justin', is_biological_human=True)
        Mood.objects.create(slug='general', title='general')
        self.picture = media.store(PNG, added_by=self.skyler)
        names = mock.patch('conversations.services.wiki_auth.names', return_value={'skyler': 'SkymanJenkins'})
        names.start()
        self.addCleanup(names.stop)

    def client_for(self, who, tier):
        _, token = mood_auth.enrol_device(who, 'phone', tier=tier)
        client = Client()
        client.cookies[mood_auth.COOKIE] = token
        return client

    def ask(self, client):
        return client.post(f'/api/media/{self.picture.sha256}/pickipedia/', json.dumps(
            {'mood': 'general', 'message': str(uuid.uuid4()), 'name': 'Jam', 'description': ''}),
            content_type='application/json')

    def test_a_wiki_sign_in_puts_its_own_picture_up_there_and_back(self):
        wiki = FakePickiPedia(user='SkymanJenkins')
        client = self.client_for(self.skyler, 'wiki')
        with mock.patch('requests.Session', lambda: wiki), mock.patch('requests.post', wiki.post), \
                mock.patch('requests.get', wiki.get):
            asked = self.ask(client)
            self.assertEqual(asked.status_code, 200)
            state = parse_qs(urlparse(asked.json()['go']).query)['state'][0]
            back = client.get(f'/moods/auth/wiki/upload?code=code-1&state={state}')
        self.assertEqual(back.status_code, 302)
        self.assertTrue(back['Location'].startswith('/moods/general/#m-'))
        self.assertEqual(wiki.uploads[0]['as'], 'Bearer token-for-SkymanJenkins')
        self.assertTrue(Message.objects.filter(source_file='wiki-upload', content__by='skyler').exists())

    def test_said_no_or_signed_out_or_not_theirs(self):
        wiki = FakePickiPedia(user='SkymanJenkins')
        client = self.client_for(self.skyler, 'wiki')
        with mock.patch('requests.Session', lambda: wiki):
            state = parse_qs(urlparse(self.ask(client).json()['go']).query)['state'][0]
        said_no = client.get(f'/moods/auth/wiki/upload?error=access_denied&state={state}')
        self.assertEqual(said_no.status_code, 400)
        self.assertIn("allow it on PickiPedia, so nothing went up", said_no.content.decode())
        self.assertEqual(self.ask(Client()).status_code, 401)
        self.assertEqual(self.ask(self.client_for(self.justin, 'key')).status_code, 403)  # skyler's picture
        with override_settings(PICKIPEDIA_UPLOAD_CLIENT_ID=''):
            self.assertEqual(self.ask(client).status_code, 503)
        self.assertEqual(wiki.uploads, [])

    def test_a_pictures_license_is_its_sharers_to_choose(self):
        skyler, justin = self.client_for(self.skyler, 'wiki'), self.client_for(self.justin, 'key')
        url = f'/api/media/{self.picture.sha256}/license/'
        self.assertEqual(Client().get(url).json(), {'license': 'cc-by-sa-4.0', 'mine': False})
        set_to = lambda client, license: client.post(url, json.dumps({'license': license}), content_type='application/json')
        self.assertEqual(set_to(justin, 'cc0').status_code, 403)
        self.assertEqual(set_to(skyler, 'cc-by-4.0').status_code, 400)
        self.assertEqual(set_to(skyler, 'cc0').json(), {'license': 'cc0', 'mine': True})
        self.assertEqual(Media.objects.get(pk=self.picture.pk).license, 'cc0')

    def test_attaching_one_says_its_license(self):
        client = self.client_for(self.justin, 'key')
        said = client.post('/api/moods/general/media/', PNG + b'\x01', content_type='application/octet-stream').json()
        self.assertEqual((said['license'], said['mine']), ('cc-by-sa-4.0', True))
        again = client.post('/api/moods/general/media/', PNG, content_type='application/octet-stream').json()
        self.assertFalse(again['mine'])  # skyler shared these bytes first

    def test_the_page_offers_it_only_when_set_up(self):
        self.assertIn('const wikiUpload = true;', Client().get('/moods/').content.decode())
        with override_settings(PICKIPEDIA_UPLOAD_CLIENT_ID=''):
            self.assertIn('const wikiUpload = false;', Client().get('/moods/').content.decode())
