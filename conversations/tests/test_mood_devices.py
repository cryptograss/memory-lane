"""Devices that time out and are renewed by name; statements attested with a key; servers and their redeploys.

Uses the real ssh-keygen with a throwaway key, as test_mood_auth does.
"""

import json
import os
import subprocess
import tempfile
from datetime import timedelta
from unittest import mock, skipUnless

from django.test import Client, TestCase, override_settings
from django.utils import timezone

from conversations.models import Device, Message, Mood, ThinkingEntity
from conversations.services import mood_auth, servers
from conversations.tests.test_mood_auth import HAS_SSH_KEYGEN, make_key, sign


class SignedInCase(TestCase):
    """justin's throwaway key, allowed to sign in; m26 to work in. No tests of its own."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.dir = tempfile.mkdtemp()
        cls.justin_key = make_key(cls.dir, 'justin')
        cls.signers = os.path.join(cls.dir, 'allowed_signers')
        with open(cls.signers, 'w') as f:
            pub = open(cls.justin_key + '.pub').read().strip()
            f.write(f'justin namespaces="{mood_auth.NAMESPACE}" {pub}\n')

    def setUp(self):
        from django.core.cache import cache
        cache.clear()  # sign-in is rate-limited, and the cache outlives a test
        ThinkingEntity.objects.create(name='justin', is_biological_human=True)
        ThinkingEntity.objects.create(name='magent', is_biological_human=False)
        Mood.objects.create(slug='m26')
        patcher = override_settings(MOOD_ALLOWED_SIGNERS=self.signers)
        patcher.enable()
        self.addCleanup(patcher.disable)

    def sign_in(self, label='phone', client=None):
        client = client or Client()
        challenge = client.get('/api/auth/challenge/').json()['challenge']
        message = mood_auth.signed_message(challenge, 'http://testserver')
        url = client.post('/api/auth/enroll/', json.dumps({'challenge': challenge, 'signature': sign(self.justin_key, message)}),
                          content_type='application/json').json()['url']
        path = url.split('testserver', 1)[1]
        client.get(path)
        client.post(path, {'label': label, 'csrfmiddlewaretoken': client.cookies['csrftoken'].value})
        return client

    def signed_post(self, url, purpose, extra, text=None):
        challenge = self.client.get('/api/auth/challenge/').json()['challenge']
        message = mood_auth.signed_message(challenge, 'http://testserver', purpose=purpose)
        if text is not None:
            message += '\n' + text
        body = {'challenge': challenge, 'signature': sign(self.justin_key, message), **extra}
        return self.client.post(url, json.dumps(body), content_type='application/json')



@skipUnless(HAS_SSH_KEYGEN, 'needs ssh-keygen')
class DevicesTest(SignedInCase):

    def test_your_devices_listed_and_one_revoked(self):
        phone = self.sign_in('phone')
        laptop = self.sign_in('laptop')
        listed = laptop.get('/api/auth/devices/').json()['devices']
        self.assertEqual([(d['label'], d['state'], d['this']) for d in listed],
                         [('laptop', 'live', True), ('phone', 'live', False)])
        phone_id = next(d['id'] for d in listed if d['label'] == 'phone')
        revoke = laptop.post(f'/api/auth/devices/{phone_id}/revoke/', HTTP_X_CSRFTOKEN=laptop.cookies['csrftoken'].value)
        self.assertEqual(revoke.status_code, 200)
        self.assertEqual(phone.get('/api/auth/devices/').status_code, 401)  # signed out
        self.assertEqual(Client().get('/api/auth/devices/').status_code, 401)

    def test_an_unused_device_times_out_and_renewing_brings_it_back(self):
        phone = self.sign_in('phone')
        Device.objects.update(last_used_at=timezone.now() - mood_auth.DEVICE_IDLE_LIMIT - timedelta(days=1))
        self.assertEqual(phone.get('/api/auth/devices/').status_code, 401)  # timed out
        self.assertEqual(self.signed_post('/api/auth/renew/', 'renew laptop', {'label': 'laptop'}).status_code, 404)
        # A signature for one device can't renew another.
        wrong = self.signed_post('/api/auth/renew/', 'renew laptop', {'label': 'phone'})
        self.assertEqual(wrong.status_code, 403)
        renewed = self.signed_post('/api/auth/renew/', 'renew phone', {'label': 'phone'})
        self.assertEqual((renewed.status_code, renewed.json()['state']), (200, 'live'))
        self.assertEqual(phone.get('/api/auth/devices/').status_code, 200)  # the same cookie works again

    def test_an_attestation_lands_in_general_with_its_proof(self):
        words = "I'll bring the PA on Saturday."
        made = self.signed_post('/api/attest/', 'attest', {'text': words}, text=words)
        self.assertEqual(made.status_code, 201)
        row = Message.objects.get(mood__slug='general', source_file='mood-attest')
        self.assertEqual((row.sender_id, row.content['text']), ('justin', words))
        self.assertTrue(row.content['key'].startswith('ssh-ed25519 '))
        # Anyone can check it with ssh-keygen alone, from what the record keeps.
        with tempfile.TemporaryDirectory() as tmp:
            signers, sig = os.path.join(tmp, 'allowed'), os.path.join(tmp, 'sig')
            open(signers, 'w').write(f"justin {row.content['key']}\n")
            open(sig, 'w').write(row.content['signature'])
            check = subprocess.run(['ssh-keygen', '-Y', 'verify', '-f', signers, '-I', 'justin', '-n',
                                    row.content['namespace'], '-s', sig], input=row.content['signed'],
                                   capture_output=True, text=True)
        self.assertEqual(check.returncode, 0, check.stderr)
        body = self.client.get('/api/moods/general/turns/').json()
        turns = body['turns']
        self.assertEqual(turns[0]['text'], words)
        self.assertIsNone(body['activity'])  # posted words, not a session's prompt with an agent at work
        self.assertTrue(turns[0]['attested']['signature'])
        # Signed words can't be swapped for others.
        forged = self.signed_post('/api/attest/', 'attest', {'text': 'I owe magent $100.'}, text=words)
        self.assertEqual(forged.status_code, 403)
        # Checked again on demand, by anyone: the signature, the key still being theirs, the words shown.
        checked = self.client.get(f'/api/moods/general/verify/{row.id}/').json()
        self.assertEqual({k: checked[k] for k in ('signer', 'signature_valid', 'key_is_current', 'statement_matches',
                                                  'origin')},
                         {'signer': 'justin', 'signature_valid': True, 'key_is_current': True,
                          'statement_matches': True, 'origin': 'http://testserver'})
        # Tampered with in the record, it says so.
        Message.objects.filter(pk=row.pk).update(content={**row.content, 'text': 'I owe magent $100.'})
        self.assertFalse(self.client.get(f'/api/moods/general/verify/{row.id}/').json()['statement_matches'])
        Message.objects.filter(pk=row.pk).update(content={**row.content, 'signed': row.content['signed'] + '!'})
        self.assertFalse(self.client.get(f'/api/moods/general/verify/{row.id}/').json()['signature_valid'])
        self.assertEqual(self.client.get(f'/api/moods/m26/verify/{row.id}/').status_code, 404)  # not this Mood's


@override_settings(MOOD_DEPLOY_KEY='d' * 40)
class ServersTest(TestCase):

    @classmethod
    def setUpTestData(cls):
        ThinkingEntity.objects.create(name='justin', is_biological_human=True)
        for slug in ('m26', 'delivery-kid', 'pickipedia-and-rabbithole'):
            Mood.objects.create(slug=slug, title=slug)

    def setUp(self):
        from django.core.cache import cache
        for s in servers.NAMES:
            cache.delete(f'server-check:{s}')
        cards = mock.patch('conversations.services.servers.wiki_card',
                           side_effect=lambda server: {'url': 'u', 'role': '', 'art': ''})
        cards.start()
        self.addCleanup(cards.stop)

    def test_a_servers_card_comes_from_its_pickipedia_page(self):
        from django.core.cache import cache
        mock.patch.stopall()
        cache.delete('server-card:Cryptograss:Hunter')
        page = {'query': {'pages': [{'revisions': [{'slots': {'main': {'content': (
            '{{Infobox resource\n| image = <pre style="font-size:5px">\n  ▓▒▒\n ▒▒▒▒\n</pre>\n'
            '| role = the storyteller\n| type = Server\n}}\nHunter is...')}}}]}]}}
        answer = mock.Mock(json=lambda: page)
        hunter = servers.SERVERS[0]
        with mock.patch('requests.get', return_value=answer) as get:
            card = servers.wiki_card(hunter)
            self.assertEqual(servers.wiki_card(hunter), card)  # kept: asked once
        self.assertEqual(get.call_count, 1)
        self.assertEqual(card, {'url': 'https://pickipedia.xyz/wiki/Cryptograss:Hunter', 'role': 'the storyteller',
                                'art': '  ▓▒▒\n ▒▒▒▒'})
        self.assertEqual(servers.wiki_card(servers.SERVERS[3]), {'url': 'https://pickipedia.xyz/', 'role': '', 'art': ''})

    def deploy(self, body, key='d' * 40):
        return self.client.post('/api/deploys/', json.dumps(body), content_type='application/json',
                                HTTP_AUTHORIZATION=f'Bearer {key}')

    def test_a_hunter_redeploy_is_told_in_every_mood_and_pulses_its_dot(self):
        self.assertEqual(self.deploy({'server': 'hunter', 'state': 'started'}, key='x').status_code, 401)
        self.assertEqual(self.deploy({'server': 'mars', 'state': 'started'}).status_code, 400)
        self.assertEqual(self.deploy({'server': 'hunter', 'state': 'started', 'by': 'justin'}).json()['moods'], 3)
        with mock.patch('conversations.services.servers.probe', return_value=(True, 5)):
            listed = {s['name']: s for s in self.client.get('/api/servers/').json()['servers']}
        self.assertTrue(listed['hunter']['deploying'])
        self.assertFalse(listed['pickipedia']['deploying'])
        self.deploy({'server': 'hunter', 'state': 'finished', 'commit': 'abc123def'})
        events = self.client.get('/api/moods/m26/turns/').json()['events']
        self.assertEqual([(e['server'], e['state']) for e in events], [('hunter', 'started'), ('hunter', 'finished')])
        self.assertIsNotNone(events[1]['took'])
        self.assertEqual(self.client.get('/api/moods/m26/turns/').json()['turns'], [])  # not a turn

    def test_every_servers_redeploy_is_told_everywhere(self):
        moods = len(self.client.get('/api/moods/').json()['moods'])
        for server in ('delivery-kid', 'pickipedia'):
            with self.subTest(server=server):
                self.assertEqual(self.deploy({'server': server, 'state': 'started'}).json()['moods'], moods)
        for slug in ('m26', 'delivery-kid', 'pickipedia-and-rabbithole'):
            self.assertEqual([e['server'] for e in self.client.get(f'/api/moods/{slug}/turns/').json()['events']],
                             ['delivery-kid', 'pickipedia'])
        # Told once in the recent-events list, not once per Mood.
        recent = self.client.get('/api/moods/recent/').json()['events']
        self.assertEqual([e['kind'] for e in recent].count('deploy'), 2)

    def test_an_open_page_can_tell_its_code_has_changed(self):
        page = self.client.get('/moods/').content.decode()
        with mock.patch('conversations.services.servers.probe', return_value=(True, 5)):
            now = self.client.get('/api/servers/').json()['page']
        self.assertRegex(now, r'^[0-9a-f]{12}$')
        self.assertIn(f'const PAGE_VERSION = "{now}";', page)  # the same code: no reload offered

    def test_a_redeploy_doesnt_make_a_mood_look_active(self):
        before = {m['slug']: (m['last_at'], m['message_count']) for m in self.client.get('/api/moods/').json()['moods']}
        self.deploy({'server': 'maybelle', 'state': 'finished'})
        after = {m['slug']: (m['last_at'], m['message_count']) for m in self.client.get('/api/moods/').json()['moods']}
        self.assertEqual(before, after)

    def test_without_a_key_nobody_tells_of_redeploys(self):
        with override_settings(MOOD_DEPLOY_KEY=''):
            self.assertEqual(self.deploy({'server': 'hunter', 'state': 'started'}).status_code, 503)


@skipUnless(HAS_SSH_KEYGEN, 'needs ssh-keygen')
class InterruptTest(SignedInCase):
    """Stop in the Mood: who may, the line it leaves, and what a runner reads."""

    def test_only_a_signed_in_person_may_stop_an_agent(self):
        self.assertEqual(self.client.post('/api/moods/m26/interrupt/', '{}', content_type='application/json').status_code, 401)
        client = self.sign_in()
        post = lambda body: client.post('/api/moods/m26/interrupt/', json.dumps(body), content_type='application/json',
                                        HTTP_X_CSRFTOKEN=client.cookies['csrftoken'].value)
        self.assertEqual(post({'agent': 'justin'}).status_code, 400)  # a person, not an agent
        stopped = post({'agent': 'magent'}).json()['interrupt']
        self.assertEqual((stopped['agent'], stopped['by']), ('magent', 'justin'))

    def test_what_a_runner_reads_and_the_line_in_the_thread(self):
        from conversations.services import settings as knobs
        self.assertEqual(self.client.get('/api/moods/m26/interrupt/', {'agent': 'magent'}).json(),
                         {'scram': knobs.scram(), 'interrupt': None})
        client = self.sign_in()
        client.post('/api/moods/m26/interrupt/', '{"agent": "magent"}', content_type='application/json',
                    HTTP_X_CSRFTOKEN=client.cookies['csrftoken'].value)
        state = self.client.get('/api/moods/m26/interrupt/', {'agent': 'magent'}).json()
        self.assertEqual(state['interrupt']['by'], 'justin')
        events = self.client.get('/api/moods/m26/turns/').json()['events']
        self.assertEqual([(e['type'], e['agent'], e['by']) for e in events], [('interrupt', 'magent', 'justin')])
        # A line, not a word: the Mood's "last said" and its turns are untouched.
        self.assertEqual(self.client.get('/api/moods/m26/turns/').json()['turns'], [])

    def test_stopping_ends_waking_and_working_at_once(self):
        import time
        import uuid
        from conversations.services.mood_view import activity
        mood = Mood.objects.get(slug='m26')
        now = time.time()
        Message.objects.create(id=uuid.uuid4(), sender_id='justin', mood=mood, source_file='mood-web',
                               content='@magent fix the', timestamp=int(now * 1000))
        self.assertEqual(activity(mood, now=now + 1)['doing'], 'waking')
        client = self.sign_in()
        client.post('/api/moods/m26/interrupt/', '{"agent": "magent"}', content_type='application/json',
                    HTTP_X_CSRFTOKEN=client.cookies['csrftoken'].value)
        self.assertIsNone(activity(mood, now=time.time() + 1))
        # What's posted after the stop wakes as usual.
        Message.objects.create(id=uuid.uuid4(), sender_id='justin', mood=mood, source_file='mood-web',
                               content='@magent ...banjo page, I meant', timestamp=int((time.time() + 2) * 1000))
        self.assertEqual(activity(mood, now=time.time() + 3)['doing'], 'waking')


@skipUnless(HAS_SSH_KEYGEN, 'needs ssh-keygen')
class NewAndArchivedMoodsTest(SignedInCase):
    """Starting a Mood from the page, and archiving one (and bringing it back)."""

    def post(self, client, url, body):
        return client.post(url, json.dumps(body), content_type='application/json',
                           HTTP_X_CSRFTOKEN=client.cookies['csrftoken'].value)

    def test_a_new_mood_gets_a_slug_from_its_title_and_a_line_saying_who(self):
        self.assertEqual(self.client.post('/api/moods/new/', '{"title": "x"}', content_type='application/json').status_code, 401)
        client = self.sign_in()
        self.assertEqual(self.post(client, '/api/moods/new/', {'title': '  '}).status_code, 400)
        made = self.post(client, '/api/moods/new/', {'title': 'Fiddle tunes, in C!', 'description': 'Which and why'})
        self.assertEqual(made.status_code, 201)
        self.assertEqual(made.json()['slug'], 'fiddle-tunes-in-c')
        again = self.post(client, '/api/moods/new/', {'title': 'Fiddle tunes in C'}).json()
        self.assertEqual(again['slug'], 'fiddle-tunes-in-c-2')  # never someone else's Mood
        mood = Mood.objects.get(slug='fiddle-tunes-in-c')
        self.assertEqual((mood.title, mood.description), ('Fiddle tunes, in C!', 'Which and why'))
        line = Message.objects.get(mood=mood)
        self.assertEqual((line.sender_id, line.content['type'], line.content['by']), ('system', 'created', 'justin'))
        listed = {m['slug']: m for m in self.client.get('/api/moods/').json()['moods']}
        self.assertEqual(listed['fiddle-tunes-in-c']['message_count'], 0)  # a system line isn't a word
        events = self.client.get('/api/moods/fiddle-tunes-in-c/turns/').json()['events']
        self.assertEqual([(e['type'], e['by']) for e in events], [('created', 'justin')])

    def test_a_pinned_mood_leads_the_list_for_everyone(self):
        Mood.objects.create(slug='general', title='general')
        client = self.sign_in()
        self.assertEqual(self.client.post('/api/moods/general/pin/', '{}', content_type='application/json').status_code, 401)
        self.assertEqual(self.post(client, '/api/moods/general/pin/', {'pinned': True}).json(),
                         {'slug': 'general', 'pinned': True})
        listed = self.client.get('/api/moods/').json()['moods']  # someone else's view: the same order
        self.assertEqual((listed[0]['slug'], listed[0]['pinned']), ('general', True))
        self.post(client, '/api/moods/general/pin/', {'pinned': False})
        self.assertFalse(any(m['pinned'] for m in self.client.get('/api/moods/').json()['moods']))

    def test_archiving_takes_a_mood_out_of_the_list_and_back(self):
        self.assertEqual(self.client.post('/api/moods/m26/archive/', '{}', content_type='application/json').status_code, 401)
        client = self.sign_in()
        self.assertEqual(self.post(client, '/api/moods/m26/archive/', {'archived': 'maybe'}).status_code, 400)
        self.assertEqual(self.post(client, '/api/moods/m26/archive/', {'archived': True}).json(),
                         {'slug': 'm26', 'archived': True})
        listed = {m['slug']: m['archived'] for m in self.client.get('/api/moods/').json()['moods']}
        self.assertTrue(listed['m26'])
        pulse = {m['slug']: m['archived'] for m in self.client.get('/api/moods/pulse/').json()['moods']}
        self.assertTrue(pulse['m26'])
        self.post(client, '/api/moods/m26/archive/', {'archived': False})
        self.assertFalse({m['slug']: m['archived'] for m in self.client.get('/api/moods/').json()['moods']}['m26'])
        # Its history is kept, like every setting: who, and when.
        from conversations.models import Setting
        self.assertEqual([(r.value, r.set_by_id) for r in Setting.objects.filter(key='archived').order_by('created_at')],
                         [(True, 'justin'), (False, 'justin')])


@skipUnless(HAS_SSH_KEYGEN, 'needs ssh-keygen')
class LoginLinkStatesTest(SignedInCase):
    """A link that can't be used says why: spent (and where), expired, or never ours."""

    def link(self):
        challenge = self.client.get('/api/auth/challenge/').json()['challenge']
        message = mood_auth.signed_message(challenge, 'http://testserver')
        url = Client().post('/api/auth/enroll/', json.dumps({'challenge': challenge, 'signature': sign(self.justin_key, message)}),
                            content_type='application/json').json()['url']
        return url.split('testserver', 1)[1]

    def test_spent_says_when_and_for_which_device_and_knows_this_browser(self):
        path = self.link()
        phone = Client()
        phone.get(path)
        phone.post(path, {'label': 'Justin’s phone ', 'csrfmiddlewaretoken': phone.cookies['csrftoken'].value})
        again = phone.get(path)  # the same browser, a second look
        self.assertEqual(again.status_code, 410)
        self.assertContains(again, "You're signed in here", status_code=410)
        elsewhere = Client().get(path)  # another browser: the home-screen app, say
        self.assertContains(elsewhere, 'This link was already used', status_code=410)
        self.assertContains(elsewhere, '“Justin’s phone”', status_code=410)
        self.assertContains(elsewhere, 'Have a sign-in link?', status_code=410)

    def test_expired_and_unknown_are_told_apart(self):
        from conversations.models import LoginCode
        path = self.link()
        LoginCode.objects.update(expires_at=timezone.now() - timedelta(minutes=1))
        self.assertContains(Client().get(path), 'This link has expired', status_code=410)
        cut = path.rstrip('/')[:-6] + '/'  # a link that lost its end when copied
        self.assertContains(Client().get(cut), "isn't a sign-in link we know", status_code=410)

    def test_a_live_link_still_asks_first(self):
        response = Client().get(self.link())
        self.assertContains(response, 'Write as justin on this device?')


class WikiFeedTest(TestCase):
    """PickiPedia's recent changes, as lines in #general: new ones only, once each, never a turn."""

    @classmethod
    def setUpTestData(cls):
        Mood.objects.create(slug='general', title='general')
        ThinkingEntity.objects.create(name='magent', is_biological_human=False)

    def setUp(self):
        from django.core.cache import cache
        cache.clear()

    def wiki(self, *rcids):
        changes = [{'type': 'edit', 'title': f'Page {n}', 'rcid': n, 'revid': 900 + n, 'user': 'SkymanJenkins',
                    'oldlen': 100, 'newlen': 100 + n, 'timestamp': '2026-10-03T23:00:00Z', 'comment': f'why {n}'}
                   for n in sorted(rcids, reverse=True)]  # the wiki answers newest first
        return mock.Mock(get=mock.Mock(return_value=mock.Mock(json=lambda: {'query': {'recentchanges': changes}})))

    def test_the_first_look_takes_only_the_latest_then_only_whats_new(self):
        from conversations.services import wiki_feed
        self.assertEqual(wiki_feed.refresh(self.wiki(1, 2, 3, 4, 5)), 3)  # not the wiki's history
        # The wiki answers with older ones too, never shown: history, not news.
        self.assertEqual(wiki_feed.refresh(self.wiki(1, 2, 3, 4, 5, 6)), 1)
        self.assertEqual(wiki_feed.refresh(self.wiki(4, 5, 6)), 0)
        body = self.client.get('/api/moods/general/turns/').json()
        lines = [(e['type'], e['title'], e['user'], e['delta']) for e in body['events']]
        self.assertEqual(body['events'][0]['at'], '2026-10-03T23:00:00Z')  # the wiki's own time
        self.assertEqual(lines, [('wiki', 'Page 3', 'SkymanJenkins', 3), ('wiki', 'Page 4', 'SkymanJenkins', 4),
                                 ('wiki', 'Page 5', 'SkymanJenkins', 5), ('wiki', 'Page 6', 'SkymanJenkins', 6)])
        self.assertEqual(body['turns'], [])  # lines, not words: nothing to answer
        self.assertEqual(self.client.get('/api/moods/').json()['moods'][0]['message_count'], 0)

    def test_a_moods_todo_list_is_shown_in_that_mood_only(self):
        from conversations.models import MoodAlias
        from conversations.services import wiki_feed
        jam = Mood.objects.create(slug='jam', title='jam')
        MoodAlias.objects.create(slug='old-jam', mood=jam)
        http = self.wiki(1)
        found = http.get.return_value.json()['query']['recentchanges']
        found += [{'type': 'edit', 'title': title, 'rcid': n, 'revid': 900 + n, 'user': 'Magent', 'oldlen': 1,
                   'newlen': 2, 'timestamp': '2026-10-03T23:00:00Z', 'comment': ''}
                  for n, title in ((2, 'Cryptograss:Moods/jam/todo'), (3, 'Cryptograss:Moods/old-jam/todo'),
                                   (4, 'Cryptograss:Moods/nobody-here/todo'), (5, 'Cryptograss:Moods/jam'))]
        wiki_feed.refresh(http)
        lines = lambda slug: [e['title'] for e in self.client.get(f'/api/moods/{slug}/turns/').json()['events']]
        self.assertEqual(sorted(lines('general')), ['Cryptograss:Moods/jam', 'Page 1'])  # not the to-do lists
        self.assertEqual(sorted(lines('jam')), ['Cryptograss:Moods/jam/todo', 'Cryptograss:Moods/old-jam/todo'])

    def test_at_most_once_a_minute(self):
        from conversations.services import wiki_feed
        http = self.wiki(1)
        self.assertTrue(wiki_feed.nudge(http, wait=True))
        asked = http.get.call_count  # its changes, and its new releases
        self.assertFalse(wiki_feed.nudge(http, wait=True))
        self.assertEqual(http.get.call_count, asked)

    def releases(self, *made):
        """A wiki whose new Release: pages are `made` ((rcid, cid, minutes ago)), each page's YAML saying who uploaded it."""
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        listed = [{'type': 'new', 'ns': 3004, 'title': f'Release:{cid}', 'rcid': rcid, 'user': 'Blue Railroad Imports',
                   'timestamp': (now - timedelta(minutes=ago)).strftime('%Y-%m-%dT%H:%M:%SZ')}
                  for rcid, cid, ago in sorted(made, reverse=True)]

        def get(url, params=None, **kwargs):
            if url.endswith('/index.php'):
                return mock.Mock(text=f"title: 'Take {params['title'][-1]} '\nuploaded_by: wiki:Watertowerband\nrelease_type: video\n")
            return mock.Mock(json=lambda: {'query': {'recentchanges': listed}})
        return mock.Mock(get=mock.Mock(side_effect=get))

    def test_a_new_release_is_shown_once_with_its_player(self):
        from conversations.services import embeds, wiki_feed
        old, new = 'Qm' + 'a' * 44, 'Qm' + 'b' * 44
        # The first look: only what's recent, not every release ever made.
        self.assertEqual(wiki_feed.refresh_releases(self.releases((7, old, 600), (8, new, 20))), 1)
        self.assertEqual(wiki_feed.refresh_releases(self.releases((7, old, 600), (8, new, 20))), 0)  # once
        newer = 'Qm' + 'c' * 44
        self.assertEqual(wiki_feed.refresh_releases(self.releases((8, new, 21), (9, newer, 1))), 1)
        listed = {new: {'title': 'Take b', 'type': 'video/mov', 'page': new, 'thumbnail': ''}}
        with mock.patch.object(embeds, 'releases', return_value=listed):
            events = self.client.get('/api/moods/general/turns/').json()['events']
        self.assertEqual([(e['type'], e['title'], e['by'], e['cid']) for e in events],
                         [('release', 'Take b', 'Watertowerband', new), ('release', 'Take c', 'Watertowerband', newer)])
        self.assertIn(f'data-hls="https://ipfs.delivery-kid.cryptograss.live/ipfs/{new}/master.m3u8"', events[0]['html'])
        self.assertNotIn('<video', events[1]['html'])  # not in the release list yet: a link until it is
        self.assertEqual(self.client.get('/api/moods/').json()['moods'][0]['message_count'], 0)  # lines, not words

    def test_a_redeploy_line_links_its_server(self):
        from conversations.services import servers
        servers.record_deploy('delivery-kid', 'finished', by='jmyles')
        servers.record_deploy('pickipedia', 'finished', by='jmyles')
        events = self.client.get('/api/moods/general/turns/').json()['events']
        self.assertEqual({e['server']: e['server_url'] for e in events},
                         {'delivery-kid': 'https://pickipedia.xyz/wiki/Cryptograss:Delivery-kid',
                          'pickipedia': 'https://pickipedia.xyz/'})


@skipUnless(HAS_SSH_KEYGEN, 'needs ssh-keygen')
class FromPhoneTest(SignedInCase):
    """A post written on a phone is marked as such; one from a computer isn't."""

    def test_the_browser_says_where_it_was_written(self):
        client = self.sign_in()
        say = lambda agent: client.post('/api/moods/m26/say/', json.dumps({'text': 'on my way'}),
                                        content_type='application/json', HTTP_USER_AGENT=agent,
                                        HTTP_X_CSRFTOKEN=client.cookies['csrftoken'].value)
        say('Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148')
        say('Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/130.0 Safari/537.36')
        turns = self.client.get('/api/moods/m26/turns/').json()['turns']
        self.assertEqual([t['mobile'] for t in turns], [True, False])
        self.assertEqual(Message.objects.filter(client_version='magenta-web/mobile').count(), 1)



@override_settings(MOOD_ADMINS=('justin',))
class EveryonesDevicesTest(TestCase):
    """An admin, on a key device, sees everyone's live devices of either tier, and can sign out only their own."""

    def setUp(self):
        self.justin = ThinkingEntity.objects.create(name='justin', is_biological_human=True)
        self.skyler = ThinkingEntity.objects.create(name='skyler', is_biological_human=True)

    def client_for(self, entity, label, tier='key'):
        device, token = mood_auth.enrol_device(entity, label, tier=tier)
        client = Client()
        client.cookies[mood_auth.COOKIE] = token
        return client, device

    def test_an_admin_sees_everyones_live_devices_by_person(self):
        laptop, _ = self.client_for(self.justin, 'laptop')
        _, sky_phone = self.client_for(self.skyler, 'PickiPedia sign-in (SkymanJenkins, Android)', tier='wiki')
        _, gone = self.client_for(self.skyler, 'old laptop')
        Device.objects.filter(pk=gone.pk).update(revoked_at=timezone.now())

        self.assertTrue(laptop.get('/api/auth/devices/').json()['admin'])
        people = {p['name']: p['devices'] for p in laptop.get('/api/auth/devices/?all=1').json()['people']}
        self.assertEqual([(d['label'], d['tier'], d['this']) for d in people['justin']], [('laptop', 'key', True)])
        self.assertEqual([(d['label'], d['tier'], bool(d['signed_out_at'])) for d in people['skyler']],
                         [('PickiPedia sign-in (SkymanJenkins, Android)', 'wiki', False),
                          ('old laptop', 'key', True)])  # signed out lately: listed, with when
        # Live first, the most lately used first; then the signed out.
        _, sky_laptop = self.client_for(self.skyler, 'laptop')
        Device.objects.filter(pk=sky_laptop.pk).update(last_used_at=timezone.now() + timedelta(minutes=1))
        people = {p['name']: p['devices'] for p in laptop.get('/api/auth/devices/?all=1').json()['people']}
        self.assertEqual([d['label'] for d in people['skyler']],
                         ['laptop', 'PickiPedia sign-in (SkymanJenkins, Android)', 'old laptop'])
        Device.objects.filter(pk=sky_laptop.pk).delete()
        long_gone = self.client_for(self.skyler, 'older laptop')[1]
        Device.objects.filter(pk=long_gone.pk).update(revoked_at=timezone.now() - timedelta(days=30))
        people = {p['name']: p['devices'] for p in laptop.get('/api/auth/devices/?all=1').json()['people']}
        self.assertNotIn('older laptop', [d['label'] for d in people['skyler']])  # long ago: not

        # Seeing isn't signing out: someone else's device is still revoked only by a signed kick.
        revoke = laptop.post(f'/api/auth/devices/{sky_phone.pk}/revoke/')
        self.assertEqual(revoke.status_code, 404)
        self.assertIsNone(Device.objects.get(pk=sky_phone.pk).revoked_at)

    def test_only_an_admin_on_a_key_device(self):
        sky, _ = self.client_for(self.skyler, 'laptop')
        self.assertFalse(sky.get('/api/auth/devices/').json()['admin'])
        self.assertEqual(sky.get('/api/auth/devices/?all=1').status_code, 403)
        wiki, _ = self.client_for(self.justin, 'PickiPedia sign-in (JMyles, Linux)', tier='wiki')
        self.assertFalse(wiki.get('/api/auth/devices/').json()['admin'])
        self.assertEqual(wiki.get('/api/auth/devices/?all=1').status_code, 403)
        self.assertEqual(Client().get('/api/auth/devices/?all=1').status_code, 401)


@skipUnless(HAS_SSH_KEYGEN, 'needs ssh-keygen')
class SignInsAreAnnouncedTest(SignedInCase):

    def test_an_ssh_sign_in_is_said_in_general(self):
        Mood.objects.create(slug='general', title='general')
        self.sign_in('laptop')
        events = self.client.get('/api/moods/general/turns/').json()['events']
        self.assertEqual([(e['kind'], e['who'], e['tier'], e['label']) for e in events],
                         [('signed-in', 'justin', 'key', 'laptop')])
