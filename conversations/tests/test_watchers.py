"""Who reads without signing in: a count for the signed-in, addresses for admins."""

from django.core.cache import cache
from django.test import Client, TestCase

from conversations.models import Mood, ThinkingEntity
from conversations.services import mood_auth, watchers

FIREFOX = 'Mozilla/5.0 (X11; Linux x86_64; rv:131.0) Gecko/20100101 Firefox/131.0'
IPHONE = 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Version/18.0 Mobile/15E148 Safari/604.1'


class WatchersTest(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.justin = ThinkingEntity.objects.create(name='justin', is_biological_human=True)
        cls.skyler = ThinkingEntity.objects.create(name='skyler', is_biological_human=True)
        Mood.objects.create(slug='general', title='general')
        Mood.objects.create(slug='jams', title='jams')

    def setUp(self):
        cache.delete(watchers.KEY)

    def look(self, slug, agent=FIREFOX, ip='203.0.113.9', **headers):
        return Client(HTTP_USER_AGENT=agent, HTTP_X_FORWARDED_FOR=ip, **headers).get(f'/api/moods/{slug}/turns/').json()

    def signed_in(self, who, tier='key'):
        _, token = mood_auth.enrol_device(who, 'laptop', tier=tier)
        client = Client(HTTP_USER_AGENT=FIREFOX)
        client.cookies[mood_auth.COOKIE] = token
        return client

    def test_readers_without_a_sign_in_are_counted_and_ssh_sign_ins_see_where_from(self):
        self.assertEqual(self.look('general')['watchers'], {'here': 1, 'all': 1})  # the count is public, never the addresses
        self.look('general', agent=IPHONE, ip='198.51.100.7')
        self.look('jams', ip='192.0.2.1, 198.51.100.20')  # Caddy's own address is the last
        seen = self.signed_in(self.skyler, tier='wiki').get('/api/moods/general/turns/').json()['watchers']
        self.assertEqual(seen, {'here': 2, 'all': 3})  # a PickiPedia sign-in: the count only
        who = self.signed_in(self.justin).get('/api/moods/general/turns/').json()['watchers']['who']
        self.assertEqual([(w['ip'], w['browser'], w['mood']) for w in who],
                         [('203.0.113.9', 'Firefox on Linux', 'general'), ('198.51.100.7', 'Safari on iPhone', 'general'),
                          ('198.51.100.20', 'Firefox on Linux', 'jams')])

    def test_scripts_bots_and_the_signed_in_arent_counted(self):
        self.look('general', agent='python-requests/2.32.3')
        self.look('general', agent='Mozilla/5.0 (compatible; Googlebot/2.1)')
        self.look('general', HTTP_AUTHORIZATION='Bearer k')
        self.signed_in(self.skyler).get('/api/moods/general/turns/')
        self.assertEqual(self.signed_in(self.justin).get('/api/moods/general/turns/').json()['watchers'],
                         {'here': 0, 'all': 0, 'who': []})

    def test_a_reader_gone_a_while_is_gone(self):
        import time
        self.look('general')
        self.assertEqual(watchers.now_watching('general')['here'], 1)
        self.assertEqual(watchers.now_watching('general', now=time.time() + watchers.WATCHING_FOR + 1)['here'], 0)

    def test_the_mood_list_shows_each_moods_count_and_no_addresses(self):
        cache.delete('moods-live')
        self.look('general')
        self.look('general', agent=IPHONE, ip='198.51.100.7')
        self.look('jams', ip='192.0.2.1')
        live = Client().get('/api/moods/live/').json()['moods']
        self.assertEqual((live['general']['watching'], live['jams']['watching']), (2, 1))
        self.assertNotIn('198.51.100.7', str(live))
