"""Images in Moods: uploads, serving, rendering, and images lifted out of transcripts."""

import base64
import json
import uuid
from unittest import mock

from django.core.cache import cache
from django.test import TestCase, override_settings

from conversations.models import Era, Media, Message, Mood, ThinkingEntity, ToolResult, ToolUse
from conversations.services import media
from conversations.services.mood_view import render_html
from conversations.services.redaction import redact_line
from importers_and_parsers.claude_code_v2 import import_line_from_claude_code_v2

PNG = b'\x89PNG\r\n\x1a\n' + b'\x00' * 32 + b'pixels'
JPEG = b'\xff\xd8\xff\xe0' + b'jpeg bytes'


class SniffAndStoreTest(TestCase):

    def test_only_raster_images_by_their_bytes(self):
        self.assertEqual(media.sniff(PNG), 'image/png')
        self.assertEqual(media.sniff(JPEG), 'image/jpeg')
        self.assertEqual(media.sniff(b'GIF89a...'), 'image/gif')
        self.assertEqual(media.sniff(b'RIFF\x00\x00\x00\x00WEBPVP8 '), 'image/webp')
        self.assertIsNone(media.sniff(b'<svg onload="alert(1)">'))
        self.assertIsNone(media.sniff(b'hello'))

    def test_the_same_image_is_stored_once(self):
        first, again = media.store(PNG), media.store(PNG)
        self.assertEqual(first.pk, again.pk)
        self.assertEqual(Media.objects.count(), 1)
        self.assertRegex(first.url, r'^/moods/media/[0-9a-f]{64}\.png$')

    def test_too_big_or_not_an_image_is_refused(self):
        with mock.patch.object(media, 'MAX_BYTES', 10):
            self.assertIsNone(media.store(PNG))
        self.assertIsNone(media.store(b'not an image at all'))


class UploadAndServeTest(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.sky = ThinkingEntity.objects.create(name='skyler', is_biological_human=True)
        Mood.objects.create(slug='m26')

    def setUp(self):
        cache.clear()

    def post(self, body):
        return self.client.post('/api/moods/m26/media/', body, content_type='application/octet-stream')

    def as_sky(self):
        return mock.patch('conversations.services.mood_auth.device_for',
                          return_value=mock.Mock(entity=self.sky, pk=1, entity_id='skyler'))

    def test_writing_an_image_needs_a_device(self):
        self.assertEqual(self.post(PNG).status_code, 401)

    def test_an_image_is_stored_and_served_back_safely(self):
        with self.as_sky():
            response = self.post(PNG)
        self.assertEqual(response.status_code, 201)
        url = response.json()['url']
        self.assertEqual(response.json()['markdown'], f'![image]({url})')
        self.assertEqual(Media.objects.get().added_by, self.sky)

        served = self.client.get(url)
        self.assertEqual(served.status_code, 200)
        self.assertEqual(served['Content-Type'], 'image/png')
        self.assertEqual(served['X-Content-Type-Options'], 'nosniff')
        self.assertIn('sandbox', served['Content-Security-Policy'])
        self.assertEqual(served.content, PNG)
        self.assertEqual(self.client.get(url.replace('.png', '.gif')).status_code, 404)
        self.assertEqual(self.client.get('/moods/media/' + '0' * 64 + '.png').status_code, 404)

    def test_what_it_claims_to_be_does_not_matter(self):
        with self.as_sky():
            response = self.client.post('/api/moods/m26/media/', b'<svg onload="x">', content_type='image/png')
        self.assertEqual(response.status_code, 400)


class RenderImagesTest(TestCase):

    def test_stored_media_and_trusted_hosts_embed_others_link(self):
        sha = 'a' * 64
        out = render_html(f'look ![the stage](/moods/media/{sha}.png) here')
        self.assertIn(f'<img src="/moods/media/{sha}.png" alt="the stage" loading="lazy">', out)
        with override_settings(MOOD_IMAGE_HOSTS={'pickipedia.xyz'}):
            self.assertIn('<img src="https://pickipedia.xyz/images/a/ab/Banjo.jpg"',
                          render_html('![banjo](https://pickipedia.xyz/images/a/ab/Banjo.jpg)'))
            other = render_html('![pixel](https://tracker.example/p.gif)')
        self.assertNotIn('<img', other)
        self.assertIn('<a href="https://tracker.example/p.gif" target="_blank" rel="noopener">pixel</a>', other)

    def test_an_image_cannot_carry_markup(self):
        from conversations.tests.test_mood_view import RenderHtmlTest
        check = RenderHtmlTest()
        sha = 'b' * 64
        for attack in (f'![x" onerror="alert(1)](/moods/media/{sha}.png)',
                       f'![a](/moods/media/{sha}.png" onerror="alert(1))',
                       '![a](https://pickipedia.xyz/x.png" onerror="alert(1))',
                       '![[[Page]]](https://pickipedia.xyz/a.png)'):
            with self.subTest(attack=attack):
                check.assertSafe(render_html(attack))


def user_line(session, content, role='user', **extra):
    return json.dumps({'type': role, 'userType': 'external', 'uuid': str(uuid.uuid4()), 'parentUuid': None,
                       'sessionId': str(session),
                       'timestamp': '2026-10-01T18:00:00.000Z', 'message': {'role': role, 'content': content},
                       **extra})


class LiftImagesTest(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.era = Era.objects.create(name='Test Era')
        cls.justin = ThinkingEntity.objects.create(name='justin', is_biological_human=True)
        ThinkingEntity.objects.create(name='magent', is_biological_human=False)
        cls.mood = Mood.objects.create(slug='m26')
        cls.session = uuid.uuid4()
        cls.mood.claim(cls.session)

    def image_block(self, data=PNG):
        return {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/png',
                                            'data': base64.b64encode(data).decode()}}

    def test_a_pasted_image_becomes_stored_media_in_the_message(self):
        line = user_line(self.session, [{'type': 'text', 'text': 'look at this'}, self.image_block()])
        msg, _ = import_line_from_claude_code_v2(line, self.era, 'a.jsonl', 'justin')
        stored = Media.objects.get()
        self.assertEqual(stored.added_by, self.justin)
        text = json.dumps(Message.objects.get(id=msg.id).content)
        self.assertIn(stored.url, text)
        self.assertNotIn(base64.b64encode(PNG).decode()[:20], text)

    @override_settings(TOOL_RESULT_CONTENT_CHARS=20000)  # production's; the default keeps no tool output
    def test_a_screenshot_a_tool_returned_shows_on_its_step(self):
        use = json.dumps({'type': 'assistant', 'userType': 'external', 'uuid': str(uuid.uuid4()), 'parentUuid': None,
                          'sessionId': str(self.session), 'timestamp': '2026-10-01T18:00:00.000Z',
                          'message': {'role': 'assistant', 'content': [
                              {'type': 'tool_use', 'id': 'toolu_shot', 'name': 'mcp__playwright__browser_take_screenshot',
                               'input': {'filename': 'x.png'}}]}})
        result = user_line(self.session, [{'type': 'tool_result', 'tool_use_id': 'toolu_shot', 'content': [
            {'type': 'text', 'text': 'Took the screenshot'}, {'type': 'image', 'data': base64.b64encode(PNG).decode(),
                                                               'mimeType': 'image/png'}]}])
        import_line_from_claude_code_v2(use, self.era, 'a.jsonl', 'justin')
        import_line_from_claude_code_v2(result, self.era, 'a.jsonl', 'justin')
        stored = Media.objects.get()
        self.assertIsNone(stored.added_by)
        steps = self.client.get('/api/moods/m26/turns/').json()['steps']
        self.assertEqual(steps[0]['images'], [stored.url])

    def test_redaction_leaves_image_bytes_alone(self):
        data = base64.b64encode(PNG).decode() + 'AKIAABCDEFGHIJKLMNOP'  # a token shape inside the bytes
        line = user_line(self.session, [{'type': 'image', 'source': {'type': 'base64', 'data': data}}])
        self.assertIn('AKIAABCDEFGHIJKLMNOP', redact_line(line))
