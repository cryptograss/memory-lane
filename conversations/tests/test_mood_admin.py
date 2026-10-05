"""Moderation by an admin's SSH key: kick, ban, AZ5 (the scram) and lift. Real ssh-keygen."""

import json
import os
import tempfile
from datetime import timedelta
from unittest import skipUnless

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone

from conversations.models import Device, LoginCode, Mood, Setting, ThinkingEntity
from conversations.services import mood_auth
from conversations.tests.test_mood_auth import HAS_SSH_KEYGEN, make_key, sign
from conversations.views_admin import admin_purpose


@skipUnless(HAS_SSH_KEYGEN, 'needs ssh-keygen')
class AdminTest(TestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.dir = tempfile.mkdtemp()
        cls.keys = {name: make_key(cls.dir, name) for name in ('justin', 'skyler')}
        cls.signers = os.path.join(cls.dir, 'allowed_signers')
        with open(cls.signers, 'w') as f:
            for name, key in cls.keys.items():
                f.write(f'{name} namespaces="{mood_auth.NAMESPACE}" {open(key + ".pub").read().strip()}\n')

    def setUp(self):
        cache.clear()
        self.justin = ThinkingEntity.objects.create(name='justin', is_biological_human=True)
        self.skyler = ThinkingEntity.objects.create(name='skyler', is_biological_human=True)
        ThinkingEntity.objects.create(name='magent', is_biological_human=False)
        Mood.objects.create(slug='m26')
        for patch in (override_settings(MOOD_ALLOWED_SIGNERS=self.signers),
                      override_settings(MOOD_ADMINS=('justin',))):
            patch.enable()
            self.addCleanup(patch.disable)
        self.devices = {p.name: Device.objects.create(entity=p, label='phone', token_hash=mood_auth.digest(p.name))
                        for p in (self.justin, self.skyler)}
        LoginCode.objects.create(code_hash='c-skyler', entity=self.skyler,
                                 expires_at=timezone.now() + timedelta(minutes=10))

    def admin(self, action, target='', key='justin', signed_as=None, device=''):
        challenge = self.client.get('/api/auth/challenge/').json()['challenge']
        purpose = admin_purpose(*(signed_as or (action, target, device)))
        signature = sign(self.keys[key], mood_auth.signed_message(challenge, 'http://testserver', purpose))
        return self.client.post('/api/auth/admin/', json.dumps({'challenge': challenge, 'signature': signature,
                                                                'action': action, 'target': target or None,
                                                                'device': device or None}),
                                content_type='application/json')

    def enroll(self, key):
        challenge = self.client.get('/api/auth/challenge/').json()['challenge']
        signature = sign(self.keys[key], mood_auth.signed_message(challenge, 'http://testserver'))
        return self.client.post('/api/auth/enroll/', json.dumps({'challenge': challenge, 'signature': signature}),
                                content_type='application/json')

    def live(self, name):
        return Device.objects.filter(entity_id=name, revoked_at__isnull=True).count()

    def test_only_an_admins_signature_acts(self):
        self.assertEqual(self.admin('kick', 'justin', key='skyler').status_code, 403)
        self.assertEqual(self.live('justin'), 1)
        with override_settings(MOOD_ADMINS=()):
            self.assertEqual(self.admin('kick', 'skyler').status_code, 503)

    def test_a_signature_for_one_action_is_not_good_for_another(self):
        response = self.admin('az5', signed_as=('kick', 'skyler'))
        self.assertEqual(response.status_code, 403)
        self.assertIsNone(Setting.objects.filter(key='scram').first())
        self.assertEqual(self.admin('kick', 'justin', signed_as=('kick', 'skyler')).status_code, 403)

    def test_a_kick_signs_someone_out_and_they_can_come_back(self):
        result = self.admin('kick', 'skyler').json()
        self.assertEqual((result['devices_signed_out'], result['links_spent']), (1, 1))
        self.assertEqual((self.live('skyler'), self.live('justin')), (0, 1))
        self.assertEqual(self.enroll('skyler').status_code, 200)

    def test_a_ban_keeps_them_out_until_unbanned(self):
        self.admin('ban', 'skyler')
        self.assertEqual(self.live('skyler'), 0)
        self.assertEqual(self.enroll('skyler').status_code, 403)
        self.admin('unban', 'skyler')
        self.assertEqual(self.enroll('skyler').status_code, 200)

    def test_az5_signs_everyone_out_and_locks_everything_until_lifted(self):
        result = self.admin('az5').json()
        self.assertEqual(result['devices_signed_out'], 2)
        self.assertEqual(result['scram']['by'], 'justin')
        self.assertEqual(self.live('justin') + self.live('skyler'), 0)

        self.client.cookies[mood_auth.COOKIE] = 'skyler'  # even a device that somehow still worked
        say = self.client.post('/api/moods/m26/say/', json.dumps({'text': 'hi'}), content_type='application/json')
        self.assertEqual(say.status_code, 423)
        self.assertEqual(self.enroll('justin').status_code, 423)
        self.assertEqual(self.client.post('/api/moods/m26/typing/', '{}', content_type='application/json').status_code, 423)
        self.assertIsNotNone(self.client.get('/api/moods/pulse/').json()['scram'])  # runners stand still
        self.assertIsNotNone(self.client.get('/api/moods/m26/turns/').json()['scram'])  # the page says so

        self.assertEqual(self.admin('lift').status_code, 200)
        self.assertIsNone(self.client.get('/api/moods/pulse/').json()['scram'])
        self.assertEqual(self.enroll('justin').status_code, 200)

    def test_one_device_can_be_signed_out_alone(self):
        laptop = Device.objects.create(entity=self.skyler, label='laptop', token_hash=mood_auth.digest('sky-laptop'))
        short = laptop.id.hex[:8]
        result = self.admin('kick-device', 'skyler', device=short).json()
        self.assertEqual((result['devices_signed_out'], result['device']), (1, 'laptop'))
        self.assertEqual(self.live('skyler'), 1)  # the phone is still signed in
        self.assertIsNotNone(Device.objects.get(pk=laptop.pk).revoked_at)
        self.assertEqual(LoginCode.objects.get(code_hash='c-skyler').used_at, None)  # links untouched

    def test_a_device_kick_reaches_only_that_persons_devices(self):
        justins = self.devices['justin'].id.hex[:8]
        self.assertEqual(self.admin('kick-device', 'skyler', device=justins).status_code, 404)
        self.assertEqual(self.live('justin'), 1)
        # A signature for one device isn't good for another.
        other = Device.objects.create(entity=self.skyler, label='laptop', token_hash=mood_auth.digest('sky-laptop'))
        forged = self.admin('kick-device', 'skyler', device=other.id.hex[:8],
                            signed_as=('kick-device', 'skyler', self.devices['skyler'].id.hex[:8]))
        self.assertEqual(forged.status_code, 403)
        self.assertEqual(self.live('skyler'), 2)

    def test_device_kicks_are_checked(self):
        self.assertEqual(self.admin('kick-device', 'skyler').status_code, 400)  # needs a device
        self.assertEqual(self.admin('kick', 'skyler', device='abcd1234').status_code, 400)  # kick takes none
        self.assertEqual(self.admin('kick-device', 'skyler', device='zz').status_code, 400)
        self.assertEqual(self.live('skyler'), 1)

    def test_requests_are_checked(self):
        self.assertEqual(self.admin('az5', 'skyler').status_code, 400)  # az5 takes no name
        self.assertEqual(self.admin('kick').status_code, 400)  # kick needs one
        self.assertEqual(self.admin('explode').status_code, 400)
        self.assertEqual(self.admin('kick', 'nobody').status_code, 404)

    def test_the_client_signs_what_the_server_checks(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location('mood_admin', 'tools/mood_admin.py')
        client = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(client)
        self.assertEqual(client.signed_message('c', 'https://ML.example', 'admin kick skyler'),
                         mood_auth.signed_message('c', 'https://ml.example', admin_purpose('kick', 'skyler')))

    def test_the_tool_makes_you_say_which(self):
        import subprocess, sys
        for args, said in ((['kick', 'skyler'], 'say which'), (['kick', 'skyler', '--device', 'ab12', '--ban'], '--ban'),
                           (['kick', 'skyler', '--all', '--device', 'ab12'], 'say which'), (['unban', 'skyler', '--all'], 'no --all')):
            run = subprocess.run([sys.executable, 'tools/mood_admin.py', *args, '--key', '/nonexistent'],
                                 capture_output=True, text=True)
            self.assertNotEqual(run.returncode, 0, args)
            self.assertIn(said, run.stderr, args)
