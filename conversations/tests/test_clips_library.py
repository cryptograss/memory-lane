"""/clips: the team's saved clips, kept in the record (services/clips.py)."""

import json

from django.core.cache import cache
from django.test import Client, TestCase

from conversations.models import Message, Mood, ThinkingEntity
from conversations.services import clips, mood_auth
from conversations.services.mood_view import render_html

CLIP = 'ffb40a1a-a936-49ee-962a-ef53e0cb7237'
OTHER = '1ab70c93-fce1-460d-8575-3bac5a666e96'


class ClipsLibraryTest(TestCase):

    def setUp(self):
        cache.delete(clips.CACHE_KEY)  # the cache is shared, not rolled back with the test
        self.addCleanup(cache.delete, clips.CACHE_KEY)
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
        r = self.say(justin, f'/clips save Tony https://www.yarn.co/yarn-clip/{CLIP}')
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.json()['note'], 'Saved "tony": /clips tony posts it')
        self.assertEqual(self.say(justin, '/clips tony').status_code, 201)
        self.assertEqual(self.posted(), [f'/clips save tony https://www.yarn.co/yarn-clip/{CLIP}',
                                         f'/clips tony https://www.yarn.co/yarn-clip/{CLIP}'])
        # The save is one line, without the clip; the use is just the clip.
        saved, used = (render_html(t) for t in self.posted())
        self.assertEqual(saved, '<p>🎬 saved <strong>tony</strong></p>')
        self.assertIn(f'data-yarn="{CLIP}"', used)
        self.assertNotIn('tony', used.split('<a', 1)[0])
        # As uses were first posted, the same.
        self.assertEqual(render_html(f'🎬 tony https://www.yarn.co/yarn-clip/{CLIP}'), used)

    def test_listing_posts_nothing(self):
        justin = self.client_for(self.justin)
        self.assertEqual(self.say(justin, '/clips').json(), {'note': 'No clips saved yet: /clips save <name> <yarn link>'})
        self.say(justin, f'/clips save tony https://getyarn.io/yarn-clip/{CLIP}')
        self.say(justin, f'/clips save bus https://www.yarn.co/yarn-clip/{OTHER}')
        self.assertEqual(self.say(justin, '/clips list').json(), {'note': 'Saved clips: bus, tony'})
        self.assertEqual(len(self.posted()), 2)  # the two saves; the listings posted nothing
        self.assertEqual([c['name'] for c in self.client.get('/api/clips/').json()['clips']], ['bus', 'tony'])

    def test_a_name_belongs_to_whoever_saved_it(self):
        justin, skyler = self.client_for(self.justin), self.client_for(self.skyler)
        self.say(justin, f'/clips save tony https://www.yarn.co/yarn-clip/{CLIP}')
        r = self.say(skyler, f'/clips save tony https://www.yarn.co/yarn-clip/{OTHER}')
        self.assertEqual((r.status_code, r.json()['error']), (400, '"tony" is justin\'s clip; pick another name'))
        self.assertEqual(self.say(skyler, '/clips forget tony').status_code, 400)
        self.assertEqual(self.say(skyler, '/clips tony').status_code, 201)  # but anyone may post it
        # Re-saved by its owner: later posts get the new clip; what was said stands.
        self.say(justin, f'/clips save tony https://www.yarn.co/yarn-clip/{OTHER}')
        self.say(skyler, '/clips tony')
        clips = [t for t in self.posted() if t.startswith('/clips tony')]
        self.assertEqual([c.rsplit('/', 1)[1] for c in clips], [CLIP, OTHER])

    def test_the_rule_holds_even_for_a_post_that_skipped_the_checks(self):
        Message.objects.create(id='00000000-0000-0000-0000-000000000001', sender=self.justin, mood_id=Mood.objects.get().pk,
                               content=f'/clips save tony https://www.yarn.co/yarn-clip/{CLIP}', source_file='mood-web')
        Message.objects.create(id='00000000-0000-0000-0000-000000000002', sender=self.skyler, mood_id=Mood.objects.get().pk,
                               content=f'/clips save tony https://www.yarn.co/yarn-clip/{OTHER}', source_file='mood-web')
        Message.objects.create(id='00000000-0000-0000-0000-000000000003', sender=self.skyler, mood_id=Mood.objects.get().pk,
                               content='/clips forget tony', source_file='mood-web')
        self.assertEqual(clips.library()['tony']['clip'], CLIP)

    def test_forget(self):
        justin = self.client_for(self.justin)
        self.say(justin, f'/clips save tony https://www.yarn.co/yarn-clip/{CLIP}')
        r = self.say(justin, '/clips forget tony')
        self.assertEqual((r.status_code, r.json()['note']), (201, 'Forgot "tony"'))
        self.assertIn('🎬 forgot <strong>tony</strong>', render_html(self.posted()[-1]))
        r = self.say(justin, '/clips tony')
        self.assertEqual((r.status_code, r.json()['error']),
                         (400, 'No clip called "tony": save one with /clips save tony <yarn link>'))

    def test_what_is_refused(self):
        justin = self.client_for(self.justin)
        for text in ('/clips save tony https://example.com/clip', '/clips save tony', '/clips save save '
                     f'https://www.yarn.co/yarn-clip/{CLIP}', '/clips save "<b>" https://www.yarn.co/yarn-clip/' + CLIP,
                     '/clips tony rice', '/clips nobody'):
            with self.subTest(text=text):
                self.assertEqual(self.say(justin, text).status_code, 400)
        self.assertEqual(self.posted(), [])
        self.assertEqual(self.say(justin, '/yarnish is just text').status_code, 201)

    def test_a_pickipedia_sign_in_can_use_them_too(self):
        self.say(self.client_for(self.justin), f'/clips save tony https://www.yarn.co/yarn-clip/{CLIP}')
        wiki = self.client_for(self.skyler, tier='wiki')
        self.assertEqual(self.say(wiki, '/clips tony').status_code, 201)

    def test_what_was_saved_and_posted_as_yarn_still_counts(self):
        """/clips was /yarn: posts made with it are still the library, still shown, and /yarn still works."""
        mood = Mood.objects.get()
        Message.objects.create(id='00000000-0000-0000-0000-0000000000b1', sender=self.justin, mood=mood,
                               content=f'/yarn save huh https://www.yarn.co/yarn-clip/{CLIP}', source_file='mood-web')
        old_use = f'/yarn huh https://www.yarn.co/yarn-clip/{CLIP}'
        self.assertEqual(clips.library()['huh']['clip'], CLIP)
        self.assertEqual(render_html(f'/yarn save huh https://www.yarn.co/yarn-clip/{CLIP}'), '<p>🎬 saved <strong>huh</strong></p>')
        self.assertIn(f'data-yarn="{CLIP}"', render_html(old_use))
        justin = self.client_for(self.justin)
        self.assertEqual(self.say(justin, '/yarn huh').status_code, 201)
        self.assertEqual(self.posted()[-1], f'/clips huh https://www.yarn.co/yarn-clip/{CLIP}')  # written the new way
