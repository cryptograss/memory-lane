"""Signing in with PickiPedia: the wiki tier chats and mentions people; the key tier makes the machines act."""

import json
import os
import tempfile
import time
import uuid
from unittest import mock

from django.test import Client, TestCase, override_settings

from conversations.models import Device, Message, Motion, ThinkingEntity
from conversations.services import motion_auth, wiki_auth

NAMES = tempfile.NamedTemporaryFile('w', suffix='.names', delete=False)
NAMES.write('# hunter name, PickiPedia name\njustin JMyles\nskyler SkymanJenkins\n')
NAMES.close()
SIGNERS = tempfile.NamedTemporaryFile('w', suffix='.signers', delete=False)
SIGNERS.write('justin namespaces="magenta-motions" ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIJustinsKeyForTests\n')
SIGNERS.close()
WIKI = dict(MOTION_WIKI_NAMES=NAMES.name, MOTION_ALLOWED_SIGNERS=SIGNERS.name,
            PICKIPEDIA_OAUTH_CLIENT_ID='client-1', PICKIPEDIA_OAUTH_CLIENT_SECRET='secret-1')


class Answer:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        return self._body


class FakeWiki:
    def __init__(self, username='Flanimal', blocked=False, token_status=200):
        self.username, self.blocked, self.token_status, self.asked = username, blocked, token_status, []

    def post(self, url, **kw):
        self.asked.append(('POST', url, kw))
        return Answer(self.token_status, {'access_token': 'tok', 'token_type': 'Bearer'})

    def get(self, url, **kw):
        self.asked.append(('GET', url, kw))
        return Answer(200, {'username': self.username, 'blocked': self.blocked, 'sub': 7})


@override_settings(**WIKI)
class NamesTest(TestCase):

    @classmethod
    def setUpTestData(cls):
        for name, human in (('justin', True), ('skyler', True), ('magent', False)):
            ThinkingEntity.objects.create(name=name, is_biological_human=human)

    def test_the_inventorys_names_and_their_aliases(self):
        self.assertEqual(wiki_auth.names(), {'justin': 'JMyles', 'skyler': 'SkymanJenkins'})
        self.assertEqual(wiki_auth.aliases(), {'jmyles': 'justin', 'skymanjenkins': 'skyler'})

    def test_mentions_work_by_either_name(self):
        from conversations.services.motion_view import mentions_in, render_html
        names = {'justin', 'skyler', 'magent'}
        self.assertEqual(mentions_in('@JMyles and @skyler, see this', names), ['justin', 'skyler'])
        self.assertIn('<span class="mention" data-who="justin">@JMyles</span>', render_html('hi @JMyles', names))

    def test_who_a_wiki_account_is_here(self):
        self.assertEqual(wiki_auth.entity_for('JMyles').name, 'justin')  # mapped: that person
        new = wiki_auth.entity_for('Watertower Band')  # anyone else: a name of their own
        self.assertEqual((new.name, new.is_biological_human), ('watertower_band', True))
        self.assertEqual(wiki_auth.entity_for('Watertower Band'), new)  # the same next time
        for wiki_name in ('Magent', 'Justin'):  # an agent's name; a key-holder's mapped to another account
            with self.subTest(wiki_name=wiki_name), self.assertRaises(wiki_auth.SignInRefused):
                wiki_auth.entity_for(wiki_name)

    def test_the_wiki_vouches_or_doesnt(self):
        wiki = FakeWiki('SkymanJenkins')
        self.assertEqual(wiki_auth.profile_for('code-1', 'https://m/cb', http=wiki)['username'], 'SkymanJenkins')
        self.assertEqual(wiki.asked[0][2]['data']['client_secret'], 'secret-1')
        self.assertEqual(wiki.asked[1][2]['headers'], {'Authorization': 'Bearer tok'})
        with self.assertRaises(wiki_auth.SignInRefused):
            wiki_auth.profile_for('code-1', 'https://m/cb', http=FakeWiki(blocked=True))
        with self.assertRaises(wiki_auth.SignInRefused):
            wiki_auth.profile_for('code-1', 'https://m/cb', http=FakeWiki(token_status=400))


@override_settings(**WIKI)
class SignInTest(TestCase):

    @classmethod
    def setUpTestData(cls):
        ThinkingEntity.objects.create(name='skyler', is_biological_human=True)

    def test_off_to_the_wiki_and_back_as_a_wiki_device(self):
        client = Client()
        away = client.get('/motions/auth/wiki/')
        self.assertEqual(away.status_code, 302)
        self.assertTrue(away['Location'].startswith('https://pickipedia.xyz/rest.php/oauth2/authorize?'))
        state = client.cookies[wiki_auth.STATE_COOKIE].value
        self.assertIn(f'state={state}', away['Location'])
        self.assertEqual(client.get('/motions/auth/wiki/callback', {'code': 'c', 'state': 'forged'}).status_code, 400)
        wiki = FakeWiki('SkymanJenkins')
        with mock.patch('requests.post', wiki.post), mock.patch('requests.get', wiki.get):
            back = client.get('/motions/auth/wiki/callback', {'code': 'c', 'state': state})
        self.assertEqual(wiki.asked[0][2]['data']['redirect_uri'], 'http://testserver/motions/auth/wiki/callback')
        self.assertEqual((back.status_code, back['Location']), (302, '/motions/'))
        device = Device.objects.get()
        self.assertEqual((device.entity_id, device.tier), ('skyler', 'wiki'))
        self.assertEqual(client.get('/api/auth/me/').json(), {'name': 'skyler'})

    def test_not_set_up_no_wiki_sign_in(self):
        with override_settings(PICKIPEDIA_OAUTH_CLIENT_ID=''):
            self.assertEqual(Client().get('/motions/auth/wiki/').status_code, 404)


@override_settings(**WIKI)
class TiersTest(TestCase):

    @classmethod
    def setUpTestData(cls):
        for name, human in (('justin', True), ('skyler', True), ('magent', False)):
            ThinkingEntity.objects.create(name=name, is_biological_human=human)
        Motion.objects.create(slug='m26', title='magenta-interface')

    def client_for(self, name, tier):
        _, token = motion_auth.enrol_device(ThinkingEntity.objects.get(name=name), 'test', tier=tier)
        client = Client()
        client.cookies[motion_auth.COOKIE] = token
        client.get('/motions/')  # a CSRF cookie
        return client

    def post(self, client, url, body):
        return client.post(url, json.dumps(body), content_type='application/json',
                           HTTP_X_CSRFTOKEN=client.cookies['csrftoken'].value)

    def test_a_wiki_device_chats_but_does_not_run_things(self):
        sky = self.client_for('skyler', 'wiki')
        self.assertEqual(self.post(sky, '/api/motions/m26/say/', {'text': 'hi @JMyles'}).status_code, 201)
        for url, body in (('/api/motions/new/', {'title': 'Mine'}), ('/api/motions/m26/archive/', {'archived': True}),
                          ('/api/motions/m26/pin/', {'pinned': True}), ('/api/motions/m26/rename/', {'title': 'x'}),
                          ('/api/motions/m26/interrupt/', {'agent': 'magent'}),
                          ('/api/settings/', {'key': 'mention_effort', 'value': 'low', 'motion': 'm26', 'agent': 'magent'})):
            with self.subTest(url=url):
                refused = self.post(sky, url, body)
                self.assertEqual(refused.status_code, 403)
                self.assertIn('SSH key', refused.json()['error'])
        key = self.client_for('justin', 'key')
        self.assertEqual(self.post(key, '/api/motions/m26/pin/', {'pinned': True}).status_code, 200)

    def test_a_wiki_post_addressing_an_agent_is_refused(self):
        sky = self.client_for('skyler', 'wiki')
        refused = self.post(sky, '/api/motions/m26/say/', {'text': '@magent @JMyles soundcheck at five?'})
        self.assertEqual((refused.status_code, refused.json()['agents']), (403, ['magent']))
        self.assertFalse(Message.objects.filter(motion_id='m26').exists())
        # Agents named in code, or a person addressed: fine.
        self.assertEqual(self.post(sky, '/api/motions/m26/say/', {'text': 'run `@magent /compact`, @JMyles'}).status_code, 201)

    def test_a_wiki_posts_agent_mention_wakes_nobody_but_people_are_told(self):
        # Refused at the door now; one that got in anyhow (posted before, say) still wakes nobody.
        sky = ThinkingEntity.objects.get(name='skyler')
        Message.objects.create(id=uuid.uuid4(), sender=sky, motion_id='m26', source_file='motion-web',
                               client_version='magenta-web/wiki', content='@magent @JMyles soundcheck at five?',
                               timestamp=int(time.time() * 1000))
        self.assertEqual(Client().get('/api/mentions/magent/').json()['mentions'], [])  # the poller sees nothing
        told = Client().get('/api/mentions/justin/').json()['mentions']  # Justin is notified
        self.assertEqual([m['turn']['tier'] for m in told], ['wiki'])
        from conversations.services.motion_view import activity
        self.assertIsNone(activity(Motion.objects.get(slug='m26')))  # nobody shown waking
        # The same words from a key device do wake magent.
        self.post(self.client_for('justin', 'key'), '/api/motions/m26/say/', {'text': '@magent soundcheck at five?'})
        self.assertEqual(len(Client().get('/api/mentions/magent/').json()['mentions']), 1)

    def test_the_page_knows_the_tier_and_the_names(self):
        page = self.client_for('skyler', 'wiki').get('/motions/m26/').content.decode()
        self.assertIn('const viewerTier = "wiki"', page)
        self.assertIn('"skyler": "SkymanJenkins"', page)


class ReadMarksTest(TestCase):
    """Where someone has read up to is kept on the server, so every device of theirs agrees."""

    @classmethod
    def setUpTestData(cls):
        ThinkingEntity.objects.create(name='justin', is_biological_human=True)
        Motion.objects.create(slug='m26')
        Motion.objects.create(slug='general')

    def device(self, tier='key'):
        _, token = motion_auth.enrol_device(ThinkingEntity.objects.get(name='justin'), 'test', tier=tier)
        client = Client()
        client.cookies[motion_auth.COOKIE] = token
        client.get('/motions/')
        return client

    def test_read_on_the_laptop_known_on_the_phone(self):
        laptop, phone = self.device(), self.device('wiki')
        self.assertEqual(Client().get('/api/seen/').status_code, 401)
        self.assertEqual(phone.get('/api/seen/').json(), {'seen': {}})
        marked = laptop.post('/api/seen/', json.dumps({'motion': 'm26'}), content_type='application/json',
                             HTTP_X_CSRFTOKEN=laptop.cookies['csrftoken'].value).json()['seen']
        self.assertEqual(list(marked), ['m26'])
        self.assertEqual(phone.get('/api/seen/').json()['seen'], marked)  # the phone knows
        # It only moves forward, and once per person per Mood.
        from conversations.models import ReadMark
        from django.utils import timezone
        from datetime import timedelta
        ReadMark.objects.update(seen_at=timezone.now() + timedelta(hours=1))
        later = phone.get('/api/seen/').json()['seen']['m26']
        phone.post('/api/seen/', json.dumps({'motion': 'm26'}), content_type='application/json',
                   HTTP_X_CSRFTOKEN=phone.cookies['csrftoken'].value)
        self.assertEqual(phone.get('/api/seen/').json()['seen']['m26'], later)
        self.assertEqual(ReadMark.objects.count(), 1)
