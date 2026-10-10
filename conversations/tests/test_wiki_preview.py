"""A PickiPedia link in brief: art or picture, the opening, a few infobox lines."""

from unittest import mock

from django.core.cache import cache
from django.test import TestCase

from conversations.services import wiki_preview

SERVER = """{{Infobox resource
| image = <pre style="font-size:5px;">
  .:=@%%@*.
 @= | =#%+
</pre>
| role = the integrator
| type = Server
| hostname = maybelle.cryptograss.live
| services = Jenkins, GlitchTip
| namesake = [[wikipedia:Maybelle Carter|Maybelle Carter]]
}}
<pre>art again</pre>

'''Maybelle''' is the primary build and integration server in the [[Cryptograss]] development ecosystem.

== Services ==
* lots
"""
BAND = """{{BandInfo
|name=Arkansauce
|origin=Arkansas
|genre=Jamgrass
}}

'''Arkansauce''' is a string band from Arkansas.<ref>A poster.</ref> They play [[Jamgrass|jamgrass]].

[[File:Arkansauce_in_spearfish%2C_sd.jpg|200px|right|thumb|on stage]]
"""
MUSICIAN = """{{MusicianInfo
|name=Justin Myles Holmes
|image=Justin Holmes - Tin Whistle and Guitar.jpg
|does1=is probably best known in bluegrass circles as "that throatsinging dude/guy"
|does2=released his debut record [[Vowel Sounds]] using a blockchain-based application called [[Revealer]], which was written entirely by bluegrassers.
|scene=St. Pete
}}

[[File:Justin-seoul-sesame.jpg|thumb|200px|Holmes in Seoul with {{musician|Jake Stargel}}]]
"""
RELEASE = """title: 'Water Tower with slug '
description: 'Rad '
release_type: video
venue: 'Porch '
uploaded_by: wiki:Watertowerband
performers:
- Jesse Tommy Kenny tay tay John
"""


class FromWikitextTest(TestCase):

    def test_a_server_is_its_art_role_and_opening(self):
        brief = wiki_preview.from_wikitext(SERVER)
        self.assertEqual(brief['art'], '  .:=@%%@*.\n @= | =#%+')  # the art whole, its | not a parameter
        self.assertEqual(brief['summary'], 'Maybelle is the primary build and integration server in the Cryptograss '
                                           'development ecosystem.')
        self.assertEqual(brief['facts'][:2], [['role', 'the integrator'], ['type', 'Server']])
        self.assertEqual(len(brief['facts']), 4)
        self.assertEqual(brief['image_file'], '')

    def test_a_band_is_its_picture_lines_and_opening(self):
        brief = wiki_preview.from_wikitext(BAND)
        self.assertEqual(brief['facts'], [['origin', 'Arkansas'], ['genre', 'Jamgrass']])
        self.assertEqual(brief['summary'], 'Arkansauce is a string band from Arkansas. They play jamgrass.')
        self.assertEqual(brief['image_file'], 'Arkansauce_in_spearfish,_sd.jpg')

    def test_a_musician_without_prose_is_said_by_name(self):
        brief = wiki_preview.from_wikitext(MUSICIAN)
        self.assertEqual(brief['summary'], 'Justin Myles Holmes is probably best known in bluegrass circles as '
                                           '"that throatsinging dude/guy"')
        self.assertEqual(brief['facts'], [['scene', 'St. Pete']])
        self.assertEqual(brief['image_file'], 'Justin Holmes - Tin Whistle and Guitar.jpg')

    def test_a_release_is_its_yaml(self):
        brief = wiki_preview.from_release(RELEASE)
        self.assertEqual(brief['name'], 'Water Tower with slug')
        self.assertEqual(brief['summary'], 'Rad')
        self.assertEqual(brief['facts'], [['type', 'video'], ['venue', 'Porch'], ['uploaded by', 'Watertowerband'],
                                          ['with', 'Jesse Tommy Kenny tay tay John']])


class PreviewEndpointTest(TestCase):

    def setUp(self):
        cache.clear()

    def wiki(self, text=None, title='Cryptograss:Maybelle'):
        page = {'title': title, 'revisions': [{'slots': {'main': {'content': text}}}]} if text else {'title': title, 'missing': True}
        return mock.Mock(get=mock.Mock(return_value=mock.Mock(json=lambda: {'query': {'pages': [page]}})))

    def test_asked_once_then_kept(self):
        http = self.wiki(SERVER)
        with mock.patch('requests.get', http.get):
            first = self.client.get('/api/wiki/preview/', {'title': 'Cryptograss:Maybelle'}).json()
            again = self.client.get('/api/wiki/preview/', {'title': ' Cryptograss:Maybelle '}).json()
        self.assertEqual(first, again)
        self.assertEqual(http.get.call_count, 1)
        self.assertEqual(first['url'], 'https://pickipedia.xyz/wiki/Cryptograss:Maybelle')
        self.assertTrue(first['art'])

    def test_no_such_page_or_no_title_is_404(self):
        with mock.patch('requests.get', self.wiki(None, 'Nope').get):
            self.assertEqual(self.client.get('/api/wiki/preview/', {'title': 'Nope'}).status_code, 404)
        self.assertEqual(self.client.get('/api/wiki/preview/', {'title': 'a|b'}).status_code, 404)
        self.assertEqual(self.client.get('/api/wiki/preview/').status_code, 404)
