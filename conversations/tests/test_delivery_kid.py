"""A video sent into a Mood: to delivery-kid from the browser, then a ReleaseDraft page as its sender.

The token is the one delivery-kid checks; the key never leaves this server; the page is written only
with PickiPedia's yes, only for the sender's own draft, and only from what delivery-kid says it holds.
"""

import hashlib
import hmac
import json
import time
from unittest import mock
from urllib.parse import parse_qs, urlparse

import yaml
from django.core.cache import cache
from django.test import Client, TestCase, override_settings

from conversations.models import Message, Mood, ThinkingEntity
from conversations.services import delivery_kid, mood_auth

DRAFT = '4377f026-4b91-4b9e-9b89-24166905b7c3'
SETUP = {'PICKIPEDIA_UPLOAD_CLIENT_ID': 'uploads-id', 'PICKIPEDIA_UPLOAD_CLIENT_SECRET': 'uploads-secret',
         'DELIVERY_KID_API_KEY': 'dk-key', 'DELIVERY_KID_URL': 'https://dk.example/'}
ANALYSED = {'draft_id': DRAFT, 'commit': '6075344a', 'status': 'uploaded', 'files': [{
    'original_filename': 'moos-hallway-water-jam-1.MOV', 'detected_title': 'moos hallway water jam 1',
    'media_type': 'video', 'format': 'MOV', 'duration_seconds': 157.978333, 'width': 1920, 'height': 1080,
    'video_codec': 'hevc', 'audio_codec': 'aac', 'size_bytes': 234668037,
    'creation_time': '2024-08-17T23:35:23.000000Z'}]}


def dk_verify(headers, action='upload'):
    """delivery-kid's own check (pinning-service/app/auth.py: create_upload_token), restated."""
    message = f"{action}:{headers['X-Upload-User']}:{headers['X-Upload-Timestamp']}"
    return hmac.compare_digest(headers['X-Upload-Token'],
                               hmac.new(b'dk-key', message.encode(), hashlib.sha256).hexdigest())


class Answer:
    def __init__(self, body, status=200):
        self.body, self.status_code = body, status

    def json(self):
        return self.body


class Fakes:
    """PickiPedia's OAuth 2 and action API, and delivery-kid's draft endpoint, which shows a draft
    only to whoever uploaded it."""

    def __init__(self, user='JMyles', uploaded_by='JMyles'):
        self.user, self.uploaded_by = user, uploaded_by
        self.headers, self.edits, self.dk_asked = {}, [], []

    def get(self, url, params=None, timeout=None, headers=None):
        if url.endswith('/oauth2/resource/profile'):
            return Answer({'username': self.user, 'blocked': False})
        if url.startswith('https://dk.example/draft-content/'):
            self.dk_asked.append(headers)
            if not dk_verify(headers):
                return Answer({'detail': {'error': 'Invalid or expired token'}}, 401)
            if headers['X-Upload-User'].lower() != self.uploaded_by.lower():
                return Answer({'detail': 'Not your draft'}, 403)
            return Answer(ANALYSED)
        return Answer({'query': {'tokens': {'csrftoken': 'C'}}})

    def post(self, url, data=None, files=None, timeout=None, headers=None):
        if url.endswith('/oauth2/access_token'):
            return Answer({'access_token': f'token-for-{self.user}'})
        self.edits.append({'data': data, 'as': self.headers.get('Authorization')})
        return Answer({'edit': {'result': 'Success'}})


@override_settings(**SETUP)
class DeliveryKidTest(TestCase):

    def setUp(self):
        cache.clear()
        self.justin = ThinkingEntity.objects.create(name='justin', is_biological_human=True)
        self.skyler = ThinkingEntity.objects.create(name='skyler', is_biological_human=True)
        self.mood = Mood.objects.create(slug='uploads-and-embeds', title='uploads-and-embeds')
        names = mock.patch('conversations.services.wiki_auth.names', return_value={'justin': 'JMyles'})
        names.start()
        self.addCleanup(names.stop)
        no_chain = mock.patch('conversations.services.delivery_kid._block_now', return_value=25300000)
        no_chain.start()
        self.addCleanup(no_chain.stop)

    def client_for(self, who, tier='key', label='phone'):
        _, token = mood_auth.enrol_device(who, label, tier=tier)
        client = Client()
        client.cookies[mood_auth.COOKIE] = token
        return client

    def ticket(self, client):
        return client.post(f'/api/moods/{self.mood.slug}/video/ticket/')

    def draft(self, client, title='MOOS hallway water jam', draft=DRAFT):
        return client.post(f'/api/moods/{self.mood.slug}/video/draft/', json.dumps({'draft': draft, 'title': title}),
                           content_type='application/json')

    # -- the token -------------------------------------------------------------------------

    def test_the_token_is_the_one_delivery_kid_checks_and_the_key_stays_here(self):
        answer = self.ticket(self.client_for(self.justin))
        self.assertEqual(answer.status_code, 200)
        body = answer.json()
        self.assertEqual(body['url'], 'https://dk.example/draft-content')
        self.assertEqual(body['headers']['X-Upload-User'], 'JMyles')
        self.assertTrue(dk_verify(body['headers']))
        self.assertFalse(dk_verify(body['headers'], action='finalize'), 'an upload token must not finalize')
        self.assertLess(abs(int(body['headers']['X-Upload-Timestamp']) - time.time() * 1000), 5000)
        self.assertNotIn('dk-key', answer.content.decode())

    def test_a_pickipedia_sign_in_uploads_under_its_own_name(self):
        client = self.client_for(self.skyler, 'wiki', 'PickiPedia sign-in (SkymanJenkins, Firefox on Android)')
        self.assertEqual(self.ticket(client).json()['headers']['X-Upload-User'], 'SkymanJenkins')

    def test_no_name_no_sign_in_or_not_set_up_means_no_token(self):
        self.assertEqual(self.ticket(Client()).status_code, 401)
        self.assertEqual(self.ticket(self.client_for(self.skyler)).status_code, 403)  # key device, no mapped name
        with override_settings(DELIVERY_KID_API_KEY=''):
            self.assertEqual(self.ticket(self.client_for(self.justin)).status_code, 503)
        with override_settings(PICKIPEDIA_UPLOAD_CLIENT_ID=''):  # nowhere to write its page
            self.assertEqual(self.ticket(self.client_for(self.justin)).status_code, 503)

    # -- the page --------------------------------------------------------------------------

    def test_the_draft_page_reads_as_the_wikis_own(self):
        text = delivery_kid.draft_yaml(DRAFT, ANALYSED, 'Title: with "quotes" & a colon', 'JMyles', 25300000)
        page = yaml.safe_load(text)
        self.assertEqual(page['draft_id'], DRAFT)
        self.assertEqual(page['type'], 'video')
        self.assertEqual(page['source'], 'magenta')
        self.assertEqual(page['uploader'], 'JMyles')
        self.assertEqual(page['upload_blockheight'], 25300000)
        self.assertIsNone(page['blockheight'])
        self.assertEqual(page['content']['title'], 'Title: with "quotes" & a colon')
        self.assertEqual(page['files'][0]['original_filename'], 'moos-hallway-water-jam-1.MOV')
        self.assertEqual(page['files'][0]['size_bytes'], 234668037)
        self.assertEqual(page['files'][0]['creation_time'], '2024-08-17T23:35:23.000000Z')
        self.assertNotIn('status', page)  # files are in: a plain draft, as the wiki writes it after upload

    def test_sent_then_written_as_them_with_a_line_in_the_mood(self):
        fakes = Fakes()
        client = self.client_for(self.justin)
        with mock.patch('requests.Session', lambda: fakes), mock.patch('requests.post', fakes.post), \
                mock.patch('requests.get', fakes.get):
            asked = self.draft(client)
            self.assertEqual(asked.status_code, 200)
            go = urlparse(asked.json()['go'])
            self.assertEqual(parse_qs(go.query)['client_id'], ['uploads-id'])
            state = parse_qs(go.query)['state'][0]
            back = client.get(f'/moods/auth/wiki/upload?code=code-1&state={state}')
        self.assertEqual(back.status_code, 302)
        self.assertEqual(back['Location'], f'/moods/{self.mood.slug}/')
        edit = fakes.edits[0]
        self.assertEqual(edit['as'], 'Bearer token-for-JMyles')
        self.assertEqual(edit['data']['action'], 'edit')
        self.assertEqual(edit['data']['title'], f'ReleaseDraft:{DRAFT}')
        self.assertEqual(edit['data']['createonly'], 1)  # never over a page that's there
        self.assertEqual(yaml.safe_load(edit['data']['text'])['content']['title'], 'MOOS hallway water jam')
        self.assertEqual(fakes.dk_asked[0]['X-Upload-User'], 'JMyles')  # asked as the name PickiPedia vouched for
        line = Message.objects.get(source_file='release-draft')
        self.assertEqual(line.content['by'], 'justin')
        self.assertEqual(line.content['draft'], DRAFT)
        self.assertTrue(line.content['page'].endswith(f'/wiki/ReleaseDraft:{DRAFT}'))
        events = self.client.get(f'/api/moods/{self.mood.slug}/turns/').json().get('events', [])
        self.assertTrue(any(e.get('type') == 'release-draft' and e.get('draft') == DRAFT for e in events))

    def test_only_their_own_draft_and_only_as_themselves(self):
        # Someone else's draft: delivery-kid won't show it to them, so no page is written.
        fakes = Fakes(uploaded_by='SkymanJenkins')
        client = self.client_for(self.justin)
        with mock.patch('requests.Session', lambda: fakes), mock.patch('requests.post', fakes.post), \
                mock.patch('requests.get', fakes.get):
            state = parse_qs(urlparse(self.draft(client).json()['go']).query)['state'][0]
            refused = client.get(f'/moods/auth/wiki/upload?code=code-1&state={state}')
        self.assertEqual(refused.status_code, 403)
        self.assertIn("someone else", refused.content.decode())
        self.assertEqual(fakes.edits, [])
        # Signed in to PickiPedia as someone else: refused before delivery-kid is even asked.
        fakes = Fakes(user='SkymanJenkins')
        with mock.patch('requests.Session', lambda: fakes), mock.patch('requests.post', fakes.post), \
                mock.patch('requests.get', fakes.get):
            state = parse_qs(urlparse(self.draft(client).json()['go']).query)['state'][0]
            refused = client.get(f'/moods/auth/wiki/upload?code=code-1&state={state}')
        self.assertEqual(refused.status_code, 403)
        self.assertEqual((fakes.edits, fakes.dk_asked), ([], []))
        self.assertFalse(Message.objects.filter(source_file='release-draft').exists())

    def test_said_no_stale_or_not_a_draft_id(self):
        client = self.client_for(self.justin)
        self.assertEqual(self.draft(client, draft='../../etc/passwd').status_code, 400)
        self.assertEqual(self.draft(Client()).status_code, 401)
        state = parse_qs(urlparse(self.draft(client).json()['go']).query)['state'][0]
        said_no = client.get(f'/moods/auth/wiki/upload?error=access_denied&state={state}')
        self.assertEqual(said_no.status_code, 400)
        self.assertIn('no ReleaseDraft page', said_no.content.decode())
        stale = client.get('/moods/auth/wiki/upload?code=code-1&state=nothing-like-it')
        self.assertEqual(stale.status_code, 400)
        self.assertFalse(Message.objects.filter(source_file='release-draft').exists())

    def test_the_page_says_whether_videos_can_go(self):
        client = self.client_for(self.justin)
        self.assertIn('const dkUpload = true;', client.get(f'/moods/{self.mood.slug}/').content.decode())
        with override_settings(DELIVERY_KID_API_KEY=''):
            self.assertIn('const dkUpload = false;', client.get(f'/moods/{self.mood.slug}/').content.decode())
