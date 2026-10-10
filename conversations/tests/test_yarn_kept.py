"""Yarn clips kept here: fetched once by the server, played from magenta after."""

from unittest import mock

from django.core.cache import cache
from django.test import Client, TestCase

from conversations.models import Media, ThinkingEntity, YarnClip
from conversations.services import mood_auth, yarn_kept
from conversations.services.mood_view import render_html

CLIP = 'ffb40a1a-a936-49ee-962a-ef53e0cb7237'
MP4 = b'\x00\x00\x00\x20ftypisom\x00\x00\x02\x00isomiso2avc1mp41' + b'\x00' * 200
CHALLENGE = b'<!DOCTYPE html><html><head><title>Just a moment...</title>'


class Yarn:
    """Yarn's two addresses, each answering as told; what was asked, kept."""

    def __init__(self, getyarn=MP4, yarnco=MP4):
        self.answers = {'y.getyarn.io': getyarn, 'y.yarn.co': yarnco}
        self.asked = []

    def get(self, url, **kwargs):
        host = url.split('/')[2]
        self.asked.append(host)
        body = self.answers[host]
        return mock.Mock(status_code=403 if body is None else 200,
                         iter_content=lambda n: [body or b''])


class KeepTest(TestCase):

    def setUp(self):
        cache.clear()

    def test_fetched_once_from_yarns_own_address_then_kept(self):
        yarn = Yarn()
        url = yarn_kept.keep(CLIP, http=yarn)
        self.assertRegex(url, r'^/moods/media/[0-9a-f]{64}\.mp4$')
        self.assertEqual(yarn.asked, ['y.getyarn.io'])
        self.assertEqual(yarn_kept.keep(CLIP, http=yarn), url)
        self.assertEqual(yarn.asked, ['y.getyarn.io'])  # kept: Yarn not asked again
        media = YarnClip.objects.get(clip=CLIP).media
        self.assertEqual((media.mime, media.license), ('video/mp4', ''))

    def test_a_challenge_page_is_not_a_video(self):
        yarn = Yarn(getyarn=CHALLENGE)
        self.assertIsNotNone(yarn_kept.keep(CLIP, http=yarn))  # the older address gave it
        self.assertEqual(yarn.asked, ['y.getyarn.io', 'y.yarn.co'])

    def test_refused_everywhere_is_not_asked_again_for_an_hour(self):
        yarn = Yarn(getyarn=CHALLENGE, yarnco=None)
        self.assertIsNone(yarn_kept.keep(CLIP, http=yarn))
        self.assertIsNone(yarn_kept.keep(CLIP, http=yarn))
        self.assertEqual(yarn.asked, ['y.getyarn.io', 'y.yarn.co'])
        self.assertFalse(Media.objects.exists())

    def test_the_card_plays_our_copy_once_kept(self):
        link = f'https://getyarn.io/yarn-clip/{CLIP}'
        self.assertNotIn('data-kept', render_html(link))
        url = yarn_kept.keep(CLIP, http=Yarn())
        self.assertIn(f'data-yarn="{CLIP}" data-kept="{url}"', render_html(link))

    def test_clips_in_what_was_said(self):
        text = f'ha https://getyarn.io/yarn-clip/{CLIP}. and https://example.com/x'
        self.assertEqual(yarn_kept.clips_in(text), [CLIP])


class ServedTest(TestCase):

    def setUp(self):
        cache.clear()
        self.justin = ThinkingEntity.objects.create(name='justin', is_biological_human=True)

    def signed_in(self):
        _, token = mood_auth.enrol_device(self.justin, 'test', tier='key')
        client = Client()
        client.cookies[mood_auth.COOKIE] = token
        return client

    def test_a_signed_in_press_has_it_fetched_an_anonymous_one_does_not(self):
        yarn = Yarn()
        with mock.patch('requests.get', yarn.get):
            self.assertEqual(Client().get(f'/api/yarn/{CLIP}/').status_code, 404)
            self.assertEqual(yarn.asked, [])
            got = self.signed_in().get(f'/api/yarn/{CLIP}/').json()
            self.assertEqual(Client().get(f'/api/yarn/{CLIP}/').json(), got)  # kept: anyone may have it
        self.assertEqual(yarn.asked, ['y.getyarn.io'])

    def test_a_kept_clip_answers_byte_ranges(self):
        url = yarn_kept.keep(CLIP, http=Yarn())
        whole = Client().get(url)
        self.assertEqual((whole.status_code, whole['Accept-Ranges'], whole.content), (200, 'bytes', MP4))
        part = Client().get(url, HTTP_RANGE='bytes=0-1')
        self.assertEqual((part.status_code, part.content, part['Content-Range']), (206, MP4[:2], f'bytes 0-1/{len(MP4)}'))
        tail = Client().get(url, HTTP_RANGE='bytes=-4')
        self.assertEqual(tail.content, MP4[-4:])
        self.assertEqual(Client().get(url, HTTP_RANGE=f'bytes={len(MP4)}-').status_code, 416)
