"""/yarn: the team's saved clips, kept in the record (services/yarn.py)."""

import json

from django.core.cache import cache
from django.test import Client, TestCase

from conversations.models import Message, Mood, ThinkingEntity
from conversations.services import mood_auth, yarn
from conversations.services.mood_view import render_html

CLIP = 'ffb40a1a-a936-49ee-962a-ef53e0cb7237'
OTHER = '1ab70c93-fce1-460d-8575-3bac5a666e96'


class YarnLibraryTest(TestCase):

    def setUp(self):
        cache.delete(yarn.CACHE_KEY)  # the cache is shared, not rolled back with the test
        self.addCleanup(cache.delete, yarn.CACHE_KEY)
        self.justin = ThinkingEntity.objects.create(name='justin', is_biological_human=True)
        self.skyler = ThinkingEntity.objects.create(name='skyler', is_biological_human=True)
        ThinkingEntity.objects.create(name='magent', is_biological_human=False)
        Mood.objects.create(slug='general')

    def client_for(self, entity, tier='key'):
        _, token = mood_auth.enrol_device(entity, 'test', tier=tier)
        client = Client(enforce_csrf_checks=False)
        client.cookies[mood_auth.COOKIE] = token
        return client

    def say(self, client, text):
        return client.post('/api/moods/general/say/', json.dumps({'text': text}), content_type='application/json')

    def posted(self):
        return list(Message.objects.filter(mood__slug='general').order_by('created_at').values_list('content', flat=True))

    def test_save_then_post_by_name(self):
        justin = self.client_for(self.justin)
        r = self.say(justin, f'/yarn save Tony https://www.yarn.co/yarn-clip/{CLIP}')
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.json()['note'], 'Saved "tony": /yarn tony posts it')
        self.assertEqual(self.say(justin, '/yarn tony').status_code, 201)
        self.assertEqual(self.posted(), [f'/yarn save tony https://www.yarn.co/yarn-clip/{CLIP}',
                                         f'/yarn tony https://www.yarn.co/yarn-clip/{CLIP}'])
        # The save is one line, without the clip; the use is just the clip.
        saved, used = (render_html(t) for t in self.posted())
        self.assertEqual(saved, '<p>🎬 saved <strong>tony</strong></p>')
        self.assertIn(f'data-yarn="{CLIP}"', used)
        self.assertNotIn('tony', used.split('<a', 1)[0])
        # As uses were first posted, the same.
        self.assertEqual(render_html(f'🎬 tony https://www.yarn.co/yarn-clip/{CLIP}'), used)

    def test_listing_posts_nothing(self):
        justin = self.client_for(self.justin)
        self.assertEqual(self.say(justin, '/yarn').json(), {'note': 'No clips saved yet: /yarn save <name> <yarn link>'})
        self.say(justin, f'/yarn save tony https://getyarn.io/yarn-clip/{CLIP}')
        self.say(justin, f'/yarn save bus https://www.yarn.co/yarn-clip/{OTHER}')
        self.assertEqual(self.say(justin, '/yarn list').json(), {'note': 'Saved clips: bus, tony'})
        self.assertEqual(len(self.posted()), 2)  # the two saves; the listings posted nothing
        self.assertEqual([c['name'] for c in self.client.get('/api/yarn/').json()['clips']], ['bus', 'tony'])

    def test_a_name_belongs_to_whoever_saved_it(self):
        justin, skyler = self.client_for(self.justin), self.client_for(self.skyler)
        self.say(justin, f'/yarn save tony https://www.yarn.co/yarn-clip/{CLIP}')
        r = self.say(skyler, f'/yarn save tony https://www.yarn.co/yarn-clip/{OTHER}')
        self.assertEqual((r.status_code, r.json()['error']), (400, '"tony" is justin\'s clip; pick another name'))
        self.assertEqual(self.say(skyler, '/yarn forget tony').status_code, 400)
        self.assertEqual(self.say(skyler, '/yarn tony').status_code, 201)  # but anyone may post it
        # Re-saved by its owner: later posts get the new clip; what was said stands.
        self.say(justin, f'/yarn save tony https://www.yarn.co/yarn-clip/{OTHER}')
        self.say(skyler, '/yarn tony')
        clips = [t for t in self.posted() if t.startswith('/yarn tony')]
        self.assertEqual([c.rsplit('/', 1)[1] for c in clips], [CLIP, OTHER])

    def test_the_rule_holds_even_for_a_post_that_skipped_the_checks(self):
        Message.objects.create(id='00000000-0000-0000-0000-000000000001', sender=self.justin, mood_id=Mood.objects.get().pk,
                               content=f'/yarn save tony https://www.yarn.co/yarn-clip/{CLIP}', source_file='mood-web')
        Message.objects.create(id='00000000-0000-0000-0000-000000000002', sender=self.skyler, mood_id=Mood.objects.get().pk,
                               content=f'/yarn save tony https://www.yarn.co/yarn-clip/{OTHER}', source_file='mood-web')
        Message.objects.create(id='00000000-0000-0000-0000-000000000003', sender=self.skyler, mood_id=Mood.objects.get().pk,
                               content='/yarn forget tony', source_file='mood-web')
        self.assertEqual(yarn.library()['tony']['clip'], CLIP)

    def test_forget(self):
        justin = self.client_for(self.justin)
        self.say(justin, f'/yarn save tony https://www.yarn.co/yarn-clip/{CLIP}')
        r = self.say(justin, '/yarn forget tony')
        self.assertEqual((r.status_code, r.json()['note']), (201, 'Forgot "tony"'))
        self.assertIn('🎬 forgot <strong>tony</strong>', render_html(self.posted()[-1]))
        r = self.say(justin, '/yarn tony')
        self.assertEqual((r.status_code, r.json()['error']),
                         (400, 'No clip called "tony": save one with /yarn save tony <yarn link>'))

    def test_what_is_refused(self):
        justin = self.client_for(self.justin)
        for text in ('/yarn save tony https://example.com/clip', '/yarn save tony', '/yarn save save '
                     f'https://www.yarn.co/yarn-clip/{CLIP}', '/yarn save "<b>" https://www.yarn.co/yarn-clip/' + CLIP,
                     '/yarn tony rice', '/yarn nobody'):
            with self.subTest(text=text):
                self.assertEqual(self.say(justin, text).status_code, 400)
        self.assertEqual(self.posted(), [])
        self.assertEqual(self.say(justin, '/yarnish is just text').status_code, 201)

    def test_a_pickipedia_sign_in_can_use_them_too(self):
        self.say(self.client_for(self.justin), f'/yarn save tony https://www.yarn.co/yarn-clip/{CLIP}')
        wiki = self.client_for(self.skyler, tier='wiki')
        self.assertEqual(self.say(wiki, '/yarn tony').status_code, 201)
