"""Web Push: a mention or an answer sent to a closed magenta, encrypted for the device, once."""

import base64
import json
import os
import uuid
from datetime import timedelta
from unittest import mock

from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from conversations.models import Message, Mood, PushSubscription, ReadMark, ThinkingEntity
from conversations.services import mood_auth, push

KEY = push.new_private_key()


def b64url(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b'=').decode()


class Browser:
    """A browser's side of a subscription: the keys it gives, and what it can decrypt."""

    def __init__(self):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.auth = os.urandom(16)
        self.endpoint = f'https://push.example/send/{uuid.uuid4()}'
        self.p256dh = b64url(self.key.public_key().public_bytes(serialization.Encoding.X962,
                                                                serialization.PublicFormat.UncompressedPoint))

    def subscription(self):
        return {'endpoint': self.endpoint, 'keys': {'p256dh': self.p256dh, 'auth': b64url(self.auth)}}

    def read(self, body):
        import http_ece
        return json.loads(http_ece.decrypt(body, private_key=self.key, auth_secret=self.auth, version='aes128gcm'))


class PushService:
    """What the push service was sent; answers `status`."""

    def __init__(self, status=201):
        self.sent, self.status = [], status

    def post(self, url, **kw):
        self.sent.append((url, kw))
        return mock.Mock(status_code=self.status, text='', reason='', headers={})


@override_settings(WEBPUSH_VAPID_PRIVATE_KEY=KEY, WEBPUSH_CONTACT='https://magenta.example')
class PushTest(TestCase):

    def setUp(self):
        cache.clear()
        self.justin = ThinkingEntity.objects.create(name='justin', is_biological_human=True)
        self.skyler = ThinkingEntity.objects.create(name='skyler', is_biological_human=True)
        self.magent = ThinkingEntity.objects.create(name='magent', is_biological_human=False)
        self.mood = Mood.objects.create(slug='general', title='general')
        self.phone = Browser()

    def client_for(self, entity, tier='key'):
        _, token = mood_auth.enrol_device(entity, 'phone', tier=tier)
        client = Client()
        client.cookies[mood_auth.COOKIE] = token
        return client

    def subscribe(self, entity, browser, tier='key'):
        return self.client_for(entity, tier).post('/api/push/subscribe/', json.dumps(browser.subscription()),
                                                  content_type='application/json')

    def said(self, who, text, **kw):
        content = text if who.is_biological_human else [{'type': 'text', 'text': text}]
        return Message.objects.create(id=uuid.uuid4(), sender=who, mood=self.mood, content=content, timestamp=1, **kw)

    def test_the_key_browsers_subscribe_with_is_ours(self):
        point = base64.urlsafe_b64decode(push.public_key() + '=')
        self.assertEqual((len(point), point[0]), (65, 4))  # an uncompressed P-256 point
        from django.utils.html import escapejs
        self.assertIn(f'const PUSH_KEY = "{escapejs(push.public_key())}";', self.client.get('/moods/').content.decode())
        with override_settings(WEBPUSH_VAPID_PRIVATE_KEY=''):
            self.assertEqual(push.public_key(), '')
            self.assertEqual(self.subscribe(self.justin, self.phone).status_code, 503)

    def test_a_key_that_cant_be_read_turns_push_off_not_the_page(self):
        for bad in ('not-a-key', 'AAAA' * 10):
            with self.subTest(key=bad), override_settings(WEBPUSH_VAPID_PRIVATE_KEY=bad):
                cache.clear()
                page = self.client.get('/moods/general/')
                self.assertEqual(page.status_code, 200)
                self.assertIn('const PUSH_KEY = "";', page.content.decode())
                told = self.client.get('/api/push/').json()
                self.assertFalse(told['enabled'])
                self.assertIn("is set but can't be used", told['problem'])
                self.assertNotIn(bad, told['problem'])  # never the key
                self.assertEqual(self.subscribe(self.justin, self.phone).status_code, 503)
        with override_settings(WEBPUSH_VAPID_PRIVATE_KEY=f' "{KEY}"\n'):  # wrapped, as a vault or .env may
            cache.clear()
            self.assertEqual(self.client.get('/api/push/').json(), {'enabled': True, 'problem': ''})

    def test_a_key_is_read_in_whichever_shape_it_was_made(self):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        made = ec.generate_private_key(ec.SECP256R1())
        der = made.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.TraditionalOpenSSL,
                                 serialization.NoEncryption())
        pem = made.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                                 serialization.NoEncryption()).decode()
        params = bytes.fromhex('06082a8648ce3d030107')  # what openssl ecparam writes first, without -noout
        expected = None
        for shape, value in (('DER, base64url', b64url(der)), ('DER, base64', base64.b64encode(der).decode()),
                             ('parameters, then DER', base64.b64encode(params + der).decode()),
                             ('PEM', pem), ('PEM, its newlines escaped', pem.replace('\n', '\\n'))):
            with self.subTest(shape=shape), override_settings(WEBPUSH_VAPID_PRIVATE_KEY=value):
                cache.clear()
                self.assertTrue(push.enabled())
                expected = expected or push.public_key()
                self.assertEqual(push.public_key(), expected)  # the same key, whatever its shape
        public = made.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        with override_settings(WEBPUSH_VAPID_PRIVATE_KEY=b64url(public)):
            cache.clear()
            self.assertIn("that's a public key", push.problem())

    def test_a_mention_reaches_a_closed_phone_encrypted_for_it_and_only_once(self):
        self.assertEqual(self.subscribe(self.justin, self.phone).status_code, 201)
        self.said(self.skyler, '@justin the bus leaves at nine')
        service = PushService()
        with mock.patch('requests.post', service.post):
            self.assertEqual(push.sweep(), 1)
            self.assertEqual(push.sweep(), 0)  # told already
        url, sent = service.sent[0]
        self.assertEqual(url, self.phone.endpoint)
        self.assertEqual(sent['headers']['content-encoding'], 'aes128gcm')
        self.assertIn(f"k={push.public_key()}", sent['headers']['authorization'])  # signed with our key
        told = self.phone.read(sent['data'])
        self.assertEqual((told['title'], told['body']), ('skyler mentioned you', 'general: @justin the bus leaves at nine'))
        self.assertEqual(told['data']['slug'], 'general')
        self.assertIsNotNone(PushSubscription.objects.get().last_sent_at)

    def test_an_agents_answer_is_pushed_too(self):
        self.subscribe(self.justin, self.phone)
        self.said(self.justin, '@magent is the deploy done?', source_file='mood-web')
        self.said(self.magent, 'Done, all green.', stop_reason='end_turn')
        service = PushService()
        with mock.patch('requests.post', service.post):
            push.sweep()
        self.assertEqual(self.phone.read(service.sent[-1][1]['data'])['title'], 'magent answered you')

    def test_not_what_theyve_read_nor_to_anyone_else_nor_to_a_signed_out_device(self):
        self.subscribe(self.justin, self.phone)
        laptop = Browser()
        self.subscribe(self.skyler, laptop)
        self.said(self.skyler, '@justin read on the laptop already')
        ReadMark.objects.create(entity=self.justin, mood=self.mood, seen_at=timezone.now() + timedelta(seconds=1))
        service = PushService()
        with mock.patch('requests.post', service.post):
            self.assertEqual(push.sweep(), 0)
        ReadMark.objects.all().delete()
        PushSubscription.objects.filter(endpoint=self.phone.endpoint).update(
            device=mood_auth.enrol_device(self.justin, 'old')[0])
        from conversations.models import Device
        Device.objects.filter(label='old').update(revoked_at=timezone.now())
        self.said(self.skyler, '@justin and again')
        with mock.patch('requests.post', service.post):
            self.assertEqual(push.sweep(), 0)
        self.assertEqual(service.sent, [])

    def test_a_subscription_the_service_has_forgotten_is_forgotten(self):
        self.subscribe(self.justin, self.phone)
        self.said(self.skyler, '@justin hello?')
        with mock.patch('requests.post', PushService(status=410).post):
            push.sweep()
        self.assertFalse(PushSubscription.objects.exists())

    def test_subscribing_needs_a_device_and_unsubscribing_ends_it(self):
        anon = Client().post('/api/push/subscribe/', json.dumps(self.phone.subscription()), content_type='application/json')
        self.assertEqual(anon.status_code, 401)
        client = self.client_for(self.justin, tier='wiki')  # either tier: it's their own bell
        self.assertEqual(client.post('/api/push/subscribe/', json.dumps({'endpoint': 'http://plain'}),
                                     content_type='application/json').status_code, 400)
        self.assertEqual(client.post('/api/push/subscribe/', json.dumps(self.phone.subscription()),
                                     content_type='application/json').status_code, 201)
        client.post('/api/push/subscribe/', json.dumps(self.phone.subscription()), content_type='application/json')
        self.assertEqual(PushSubscription.objects.count(), 1)  # the same endpoint again: one
        client.post('/api/push/unsubscribe/', json.dumps({'endpoint': self.phone.endpoint}), content_type='application/json')
        self.assertFalse(PushSubscription.objects.exists())

    def test_the_clock_runs_from_the_pulse_and_from_pages(self):
        with mock.patch('conversations.services.push.threading.Thread') as thread:
            self.client.get('/api/moods/pulse/')
            self.client.get('/api/moods/live/')
        sweeps = [c for c in thread.call_args_list if c.kwargs.get('target') is push._sweep_and_close]
        self.assertEqual(len(sweeps), 1)  # at most every EVERY seconds (the wiki feed has its own thread)
        with override_settings(WEBPUSH_VAPID_PRIVATE_KEY=''):
            cache.clear()
            self.assertFalse(push.nudge())
