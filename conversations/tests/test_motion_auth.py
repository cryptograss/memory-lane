"""Writing into Motions: SSH keys enroll devices; devices say things.

Uses the real ssh-keygen with a throwaway key, so the signature check is
the one production runs.
"""

import json
import os
import subprocess
import tempfile
from unittest import mock, skipUnless
import shutil

from django.test import Client, TestCase, override_settings

from conversations.models import Device, Message, Motion, ThinkingEntity
from conversations.services import motion_auth

HAS_SSH_KEYGEN = shutil.which('ssh-keygen') is not None


def make_key(directory, name):
    key = os.path.join(directory, name)
    subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', key], check=True)
    return key


def sign(key, challenge, namespace=motion_auth.NAMESPACE):
    with tempfile.TemporaryDirectory() as tmp:
        message = os.path.join(tmp, 'm')
        with open(message, 'w') as f:
            f.write(challenge)
        subprocess.run(['ssh-keygen', '-q', '-Y', 'sign', '-f', key, '-n', namespace, message],
                       check=True, capture_output=True)
        return open(message + '.sig').read()


@skipUnless(HAS_SSH_KEYGEN, 'needs ssh-keygen')
class MotionAuthTest(TestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.dir = tempfile.mkdtemp()
        cls.justin_key = make_key(cls.dir, 'justin')
        cls.stranger_key = make_key(cls.dir, 'stranger')
        cls.signers = os.path.join(cls.dir, 'allowed_signers')
        with open(cls.signers, 'w') as f:
            pub = open(cls.justin_key + '.pub').read().strip()
            f.write(f'justin namespaces="{motion_auth.NAMESPACE}" {pub}\n')

    def setUp(self):
        ThinkingEntity.objects.create(name='justin', is_biological_human=True)
        ThinkingEntity.objects.create(name='skyler', is_biological_human=True)
        ThinkingEntity.objects.create(name='magent', is_biological_human=False)
        self.motion = Motion.objects.create(slug='m26')
        patcher = override_settings(MOTION_ALLOWED_SIGNERS=self.signers)
        patcher.enable()
        self.addCleanup(patcher.disable)

    def enroll(self, name=None, key=None, namespace=motion_auth.NAMESPACE, origin='http://testserver'):
        challenge = self.client.get('/api/auth/challenge/').json()['challenge']
        message = motion_auth.signed_message(challenge, origin)
        body = {'challenge': challenge, 'signature': sign(key or self.justin_key, message, namespace)}
        if name:
            body['name'] = name
        return self.client.post('/api/auth/enroll/', json.dumps(body), content_type='application/json')

    def sign_in(self, client=None):
        client = client or self.client
        path = self.enroll().json()['url'].split('testserver', 1)[1]
        client.get(path)  # as a browser does: the form carries the CSRF token
        client.post(path, {'label': 'phone', 'csrfmiddlewaretoken': client.cookies['csrftoken'].value})
        return client

    # --- enrollment -----------------------------------------------------------

    def test_a_valid_signature_gets_a_one_time_link_that_enrolls_a_device(self):
        response = self.enroll()
        self.assertEqual(response.status_code, 200)
        path = response.json()['url'].split('testserver', 1)[1]

        page = self.client.get(path)
        self.assertContains(page, 'Write as justin on this device?')
        self.assertFalse(Device.objects.exists())  # a GET (a link preview) spends nothing

        done = self.client.post(path, {'label': 'phone'})
        self.assertRedirects(done, '/motions/', fetch_redirect_response=False)
        device = Device.objects.get()
        self.assertEqual((device.entity_id, device.label), ('justin', 'phone'))
        cookie = done.cookies[motion_auth.COOKIE]
        self.assertTrue(cookie['httponly'])
        self.assertNotEqual(device.token_hash, cookie.value)  # only the hash is stored

        again = Client().post(path, {'label': 'someone else'})
        self.assertEqual(again.status_code, 410)
        self.assertEqual(Device.objects.count(), 1)

    def test_a_preview_marks_the_devices_it_enrolls(self):
        with override_settings(DEVICE_LABEL_PREFIX='preview · '):
            self.sign_in()
        self.assertEqual(Device.objects.get().label, 'preview · phone')

    def test_the_key_says_who_you_are(self):
        response = self.enroll()
        self.assertEqual(response.json()['name'], 'justin')

    def test_naming_yourself_still_works(self):
        self.assertEqual(self.enroll(name='justin').json()['name'], 'justin')

    def test_a_key_speaks_only_for_its_own_name(self):
        self.assertEqual(self.enroll(name='skyler').status_code, 403)

    def test_an_unlisted_key_is_refused(self):
        self.assertEqual(self.enroll(key=self.stranger_key).status_code, 403)

    def test_a_signature_for_another_purpose_is_refused(self):
        self.assertEqual(self.enroll(namespace='git').status_code, 403)

    def test_a_signature_made_for_another_server_is_refused(self):
        # What a server relaying a production challenge would hold.
        self.assertEqual(self.enroll(origin='https://justin1.hunter.cryptograss.live').status_code, 403)

    def test_a_bare_challenge_signature_is_refused(self):
        challenge = self.client.get('/api/auth/challenge/').json()['challenge']
        body = {'challenge': challenge, 'signature': sign(self.justin_key, challenge)}
        response = self.client.post('/api/auth/enroll/', json.dumps(body), content_type='application/json')
        self.assertEqual(response.status_code, 403)

    def test_the_client_signs_what_the_server_checks(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location('motion_login', 'tools/motion_login.py')
        client = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(client)
        self.assertEqual(client.NAMESPACE, motion_auth.NAMESPACE)
        self.assertEqual(client.signed_message('c', 'https://Memory-Lane.example/'),
                         motion_auth.signed_message('c', 'https://memory-lane.example'))

    def test_enrolling_is_rate_limited_and_size_capped(self):
        from django.core.cache import cache
        cache.clear()
        with mock.patch('conversations.views_auth.ENROLL_PER_MINUTE', 2):
            codes = [self.enroll(key=self.stranger_key).status_code for _ in range(3)]
        self.assertEqual(codes, [403, 403, 429])
        cache.clear()
        big = self.client.post('/api/auth/enroll/', '{"x": "' + 'a' * 70_000 + '"}',
                               content_type='application/json')
        self.assertEqual(big.status_code, 413)

    def test_a_stale_challenge_is_refused(self):
        with mock.patch.object(motion_auth, 'CHALLENGE_MAX_AGE', -1):
            self.assertEqual(self.enroll().status_code, 400)

    def test_without_a_signers_file_nobody_enrolls(self):
        with override_settings(MOTION_ALLOWED_SIGNERS=''):
            self.assertEqual(self.enroll().status_code, 403)

    # --- saying things --------------------------------------------------------

    def say(self, text, client=None):
        return (client or self.client).post('/api/motions/m26/say/', json.dumps({'text': text}),
                                            content_type='application/json')

    def test_reading_needs_nothing_writing_needs_a_device(self):
        self.assertEqual(self.client.get('/api/motions/m26/turns/').status_code, 200)
        self.assertEqual(self.say('hello').status_code, 401)

    def test_a_device_says_things_as_its_person_redacted(self):
        self.sign_in()
        response = self.say('@magent the key is DB_PASSWORD=hunter22, [[Tony Rice]]')
        self.assertEqual(response.status_code, 201)

        turns = self.client.get('/api/motions/m26/turns/').json()['turns']
        self.assertEqual([t['sender'] for t in turns], ['justin'])
        self.assertIn('DB_PASSWORD=[REDACTED]', turns[0]['text'])
        self.assertEqual(self.client.get('/api/mentions/magent/').json()['mentions'][0]['turn']['sender'], 'justin')

    def test_writes_need_the_csrf_token(self):
        strict = self.sign_in(Client(enforce_csrf_checks=True))
        self.assertEqual(self.say('hello', client=strict).status_code, 403)
        strict.get('/motions/')  # sets csrftoken, as the page does
        token = strict.cookies['csrftoken'].value
        ok = strict.post('/api/motions/m26/say/', json.dumps({'text': 'hello'}),
                         content_type='application/json', HTTP_X_CSRFTOKEN=token)
        self.assertEqual(ok.status_code, 201)

    def test_empty_long_and_fast_are_refused(self):
        self.sign_in()
        self.assertEqual(self.say('   ').status_code, 400)
        self.assertEqual(self.say('x' * 20_001).status_code, 400)
        with mock.patch('conversations.views_auth.PER_MINUTE', 2):
            self.say('one'); self.say('two')
            self.assertEqual(self.say('three').status_code, 429)

    def test_odd_bodies_are_refused_not_crashed_on(self):
        self.sign_in()
        token = self.client.cookies['csrftoken'].value
        for body in ('{"text": 5}', '{"text": ["a"]}', '[1]'):
            response = self.client.post('/api/motions/m26/say/', body, content_type='application/json',
                                        HTTP_X_CSRFTOKEN=token)
            self.assertEqual(response.status_code, 400, body)
        self.assertEqual(self.say('a\x00b').status_code, 201)
        self.assertEqual(Message.objects.get(source_file='motion-web').content, 'ab')

    def test_signing_out_revokes_the_device(self):
        self.sign_in()
        self.client.post('/api/auth/logout/')
        self.assertIsNotNone(Device.objects.get().revoked_at)
        self.assertEqual(self.say('still here?').status_code, 401)

    def test_the_page_knows_who_is_writing(self):
        self.assertContains(self.client.get('/motions/'), 'const viewer = "" || null')
        self.sign_in()
        self.assertContains(self.client.get('/motions/'), 'const viewer = "justin" || null')
        self.assertEqual(self.client.get('/api/auth/me/').json(), {'name': 'justin'})
