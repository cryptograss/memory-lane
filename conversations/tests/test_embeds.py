"""Pictures and recordings shown where they're linked: PickiPedia and Commons files, delivery-kid releases."""

from unittest import mock

from django.core.cache import cache
from django.test import TestCase

from conversations.services import embeds
from conversations.services.mood_view import render_html

V1 = 'bafybeiavz2kvaf4rfv7q46hxse4fk2tcqpzm4mgr67jbdqa2wpdarcie5y'
V0 = 'QmNQdj8Vq1RrPsMsSX5re2EiW3M2fhs9SGsQkYoaFqLkxH'
FILES = {
    'File:Earl Scruggs.jpg': {'url': 'https://upload.wikimedia.org/c/e/Earl_Scruggs.jpg', 'mime': 'image/jpeg',
                              'thumburl': 'https://upload.wikimedia.org/c/thumb/e/800px-Earl_Scruggs.jpg',
                              'descriptionurl': 'https://commons.wikimedia.org/wiki/File:Earl_Scruggs.jpg'},
    'File:Squats.jpg': {'url': 'https://pickipedia.xyz/images/s/Squats.jpg', 'mime': 'image/jpeg',
                        'descriptionurl': 'https://pickipedia.xyz/wiki/File:Squats.jpg'},
    'File:Breakdown.webm': {'url': 'https://upload.wikimedia.org/c/b/Breakdown.webm', 'mime': 'video/webm',
                            'descriptionurl': 'https://commons.wikimedia.org/wiki/File:Breakdown.webm'},
}
RELEASES = [
    {'ipfs_cid': 'B' + V1[1:], 'title': 'Blue Railroad Train (Squats) #3, #4', 'file_type': 'video/webm',
     'page_title': 'B' + V1[1:], 'thumbnail': 'Squats.jpg'},
    {'ipfs_cid': V0, 'title': "Barlow's Jig", 'file_type': 'video/mp4', 'page_title': V0},
    {'ipfs_cid': 'QmHLSstream00000000000000000000000000000000000', 'title': 'Live', 'file_type': 'video/hls'},
    {'ipfs_cid': 'QmFlac000000000000000000000000000000000000000a', 'title': 'Take 2', 'file_type': 'audio/flac'},
]


def pickipedia(params, timeout=4):
    if params['action'] == 'releaselist':
        return {'releases': RELEASES}
    title = params['titles']
    info = FILES.get(title)
    return {'query': {'pages': {'1': {'title': title, **({'imageinfo': [info]} if info else {'missing': ''})}}}}


class EmbedsTest(TestCase):

    def setUp(self):
        cache.clear()
        patcher = mock.patch.object(embeds, '_ask', side_effect=pickipedia)
        self.asked = patcher.start()
        self.addCleanup(patcher.stop)
        # A release page's YAML, for the thumbnail the release list leaves out.
        thumb = mock.patch.object(embeds, '_release_thumbnail', side_effect=lambda page: '')
        thumb.start()
        self.addCleanup(thumb.stop)

    def test_a_file_by_wikilink_page_or_commons_is_the_picture(self):
        picture = ('<a class="img" href="https://commons.wikimedia.org/wiki/File:Earl_Scruggs.jpg" target="_blank" '
                   'rel="noopener"><img src="https://upload.wikimedia.org/c/thumb/e/800px-Earl_Scruggs.jpg" ')
        for text in ('[[File:Earl Scruggs.jpg]]', '[[File:Earl_Scruggs.jpg|thumb|300px|Earl, 1977]]',
                     'https://pickipedia.xyz/wiki/File:Earl_Scruggs.jpg',
                     'https://commons.wikimedia.org/wiki/File:Earl_Scruggs.jpg'):
            with self.subTest(text=text):
                self.assertIn(picture, render_html(f'look: {text}'))
        self.assertIn('alt="Earl, 1977" loading="lazy"></a><span class="caption">Earl, 1977</span>',
                      render_html('[[File:Earl Scruggs.jpg|thumb|300px|Earl, 1977]]'))
        self.assertEqual(self.asked.call_count, 1)  # one question for all four: kept

    def test_a_file_thats_a_video_plays(self):
        self.assertIn('<video controls preload="metadata" playsinline src="https://upload.wikimedia.org/c/b/Breakdown.webm">',
                      render_html('[[File:Breakdown.webm]]'))

    def test_a_file_nobody_has_stays_a_link(self):
        self.assertIn('<a class="wikilink" href="https://pickipedia.xyz/wiki/File:Nope.jpg"', render_html('[[File:Nope.jpg]]'))

    def test_a_release_plays_by_gateway_link_release_page_or_wikilink(self):
        player = (f'<span class="embed release"><video controls preload="none" playsinline '
                  f'src="https://ipfs.delivery-kid.cryptograss.live/ipfs/{V1}" '
                  f'poster="https://pickipedia.xyz/images/s/Squats.jpg"></video>'
                  f'<a class="caption" href="https://pickipedia.xyz/wiki/Release:B{V1[1:]}" target="_blank" rel="noopener">'
                  f'🎬 Blue Railroad Train (Squats) #3, #4</a></span>')
        for text in (f'https://ipfs.delivery-kid.cryptograss.live/ipfs/{V1}', f'[[Release:B{V1[1:]}]]',
                     f'https://pickipedia.xyz/wiki/Release:B{V1[1:]}'):
            with self.subTest(text=text):
                self.assertIn(player, render_html(f'watch {text}'))
        self.assertIn(f'src="https://ipfs.delivery-kid.cryptograss.live/ipfs/{V0}"></video>',  # no thumbnail, no poster
                      render_html(f'https://ipfs.delivery-kid.cryptograss.live/ipfs/{V0}'))
        self.assertIn('<audio controls preload="none" src="https://ipfs.delivery-kid.cryptograss.live/ipfs/qmflac',
                      render_html('[[Release:QmFlac000000000000000000000000000000000000000a]]').lower())

    def test_what_isnt_a_playable_release_stays_a_link(self):
        for text in ('https://ipfs.delivery-kid.cryptograss.live/ipfs/QmHLSstream00000000000000000000000000000000000',
                     'https://ipfs.delivery-kid.cryptograss.live/ipfs/QmUnknown0000000000000000000000000000000000000',
                     f'https://ipfs.delivery-kid.cryptograss.live/ipfs/{V1}/inside.mp4'):
            with self.subTest(text=text):
                out = render_html(text)
                self.assertNotIn('<video', out)
                self.assertIn('<a href="https://ipfs.delivery-kid', out)

    def test_pickipedia_unreachable_everything_stays_a_link(self):
        self.asked.side_effect = OSError('down')
        self.assertNotIn('<img', render_html('[[File:Earl Scruggs.jpg]]'))
        self.assertNotIn('<video', render_html(f'https://ipfs.delivery-kid.cryptograss.live/ipfs/{V1}'))

    def test_embeds_are_only_markup_the_renderer_makes(self):
        from conversations.tests.test_mood_view import RenderHtmlTest
        FILES['File:X".jpg'] = {'url': 'https://upload.wikimedia.org/x"onerror="1.jpg', 'mime': 'image/jpeg'}
        try:
            for text in ('[[File:Earl Scruggs.jpg|<b>caption</b>]]', '[[File:X".jpg]]',
                         f'[[Release:B{V1[1:]}|**x**]] https://commons.wikimedia.org/wiki/File:Earl_Scruggs.jpg"x'):
                with self.subTest(text=text):
                    RenderHtmlTest.assertSafe(self, render_html(text), text)
        finally:
            del FILES['File:X".jpg']
