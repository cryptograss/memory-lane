"""A link's preview card: read from the page's own tags, fetched only from the public internet, only for links said here."""

import socket
import uuid
from unittest import mock

from django.core.cache import cache
from django.test import Client, TestCase

from conversations.models import Message, Mood, ThinkingEntity
from conversations.services import unfurl
from conversations.services.mood_view import plain_links

PAGE = """<html><head><title>Ignored when og:title is there</title>
<meta property="og:title" content="Hillberry Harvest Festival &amp; Jam">
<meta property="og:description" content="Three days of bluegrass in Eureka Springs.">
<meta property="og:image" content="/img/poster.jpg">
<meta property="og:site_name" content="Hillberry">
</head><body>...</body></html>"""


class Answer:
    def __init__(self, status=200, body=b'', headers=None):
        self.status_code, self.body = status, body
        self.headers = {'Content-Type': 'text/html; charset=utf-8', **(headers or {})}
        self.encoding = 'utf-8'

    def iter_content(self, size):
        for i in range(0, len(self.body), size):
            yield self.body[i:i + size]

    def close(self):
        pass


class FakeWeb:
    def __init__(self, pages):
        self.pages, self.asked = pages, []

    def get(self, url, **kw):
        self.asked.append(url)
        return self.pages.get(url) or Answer(404)


def resolving(table):
    """getaddrinfo as if each host had the address the table gives."""
    def fake(host, port, *a, **k):
        if host not in table:
            raise socket.gaierror('no such host')
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (table[host], 0))]
    return mock.patch('conversations.services.unfurl.socket.getaddrinfo', fake)


class ReadTest(TestCase):

    def test_a_pages_own_tags(self):
        found = unfurl.read(PAGE, 'https://hillberry.example/festival')
        self.assertEqual(found, {'url': 'https://hillberry.example/festival', 'title': 'Hillberry Harvest Festival & Jam',
                                 'description': 'Three days of bluegrass in Eureka Springs.',
                                 'image': 'https://hillberry.example/img/poster.jpg', 'site': 'Hillberry'})

    def test_its_title_and_description_when_there_are_no_tags_and_no_plain_http_picture(self):
        page = '<head><title> The   Station Inn </title><meta name="description" content="Nashville, home of bluegrass">' \
               '<meta property="og:image" content="http://insecure.example/a.jpg"></head>'
        found = unfurl.read(page, 'http://stationinn.example/')
        self.assertEqual((found['title'], found['image'], found['site']), ('The Station Inn', '', 'stationinn.example'))
        self.assertIsNone(unfurl.read('<head></head>', 'https://x.example/'))


class FetchTest(TestCase):

    def setUp(self):
        cache.clear()

    def test_only_the_public_internet(self):
        with resolving({'pub.example': '93.184.216.34', 'in.example': '10.0.0.2', 'me.example': '127.0.0.1',
                        'meta.example': '169.254.169.254', 'v6.example': '::1'}):
            self.assertTrue(unfurl.public('pub.example'))
            for host in ('in.example', 'me.example', 'meta.example', 'v6.example', 'nowhere.example'):
                self.assertFalse(unfurl.public(host), host)

    def test_refused_a_private_address_a_redirect_into_one_not_a_page_and_odd_ports(self):
        web = FakeWeb({'https://pub.example/a': Answer(302, headers={'Location': 'http://in.example/admin'}),
                       'https://pub.example/pdf': Answer(200, b'%PDF', {'Content-Type': 'application/pdf'})})
        with resolving({'pub.example': '93.184.216.34', 'in.example': '10.0.0.2'}):
            for url in ('http://in.example/', 'https://pub.example/a', 'https://pub.example/pdf',
                        'https://pub.example:8000/', 'file:///etc/passwd', 'ftp://pub.example/'):
                with self.assertRaises(unfurl.Refused, msg=url):
                    unfurl._fetch(url, web)
        self.assertNotIn('http://in.example/admin', web.asked)  # refused before it was asked

    def test_half_a_megabyte_at_most_and_kept_once_fetched(self):
        web = FakeWeb({'https://pub.example/big': Answer(200, PAGE.encode() + b'x' * (2 * unfurl.MAX_BYTES))})
        with resolving({'pub.example': '93.184.216.34'}):
            first = unfurl.card('https://pub.example/big', web)
            again = unfurl.card('https://pub.example/big', web)
        self.assertEqual((first['title'], again, len(web.asked)), ('Hillberry Harvest Festival & Jam', first, 1))


class WhichLinksTest(TestCase):

    def test_plain_links_only_each_once_none_in_code(self):
        text = ('See https://hillberry.example/fest, https://pickipedia.xyz/wiki/Tony_Rice and '
                'https://www.yarn.co/yarn-clip/ffb40a1a-a936-49ee-962a-ef53e0cb7237 and '
                'https://magenta.cryptograss.live/moods/general/ and https://hillberry.example/fest again.\n'
                '`https://in.code.example/` ```\nhttps://in.block.example/\n```')
        self.assertEqual(plain_links(text), ['https://hillberry.example/fest'])


class EndpointTest(TestCase):

    def setUp(self):
        cache.clear()
        self.justin = ThinkingEntity.objects.create(name='justin', is_biological_human=True)
        self.mood = Mood.objects.create(slug='general', title='general')
        Mood.objects.create(slug='other', title='other')
        self.message = Message.objects.create(id=uuid.uuid4(), sender=self.justin, mood=self.mood, timestamp=1,
                                              content='Tickets: https://hillberry.example/festival', source_file='mood-web')

    def test_cards_for_a_messages_links_and_nothing_else(self):
        web = FakeWeb({'https://hillberry.example/festival': Answer(200, PAGE.encode())})
        with resolving({'hillberry.example': '93.184.216.34'}), mock.patch('requests.get', web.get):
            cards = Client().get(f'/api/moods/general/unfurl/{self.message.id}/').json()['cards']
            elsewhere = Client().get(f'/api/moods/other/unfurl/{self.message.id}/')
        self.assertEqual([c['title'] for c in cards], ['Hillberry Harvest Festival & Jam'])
        self.assertEqual(elsewhere.status_code, 404)
        self.assertEqual(web.asked, ['https://hillberry.example/festival'])
