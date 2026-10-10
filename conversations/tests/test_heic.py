"""An iPhone photo (HEIC) attached in a Mood is stored as a JPEG.

Most browsers can't show HEIC, so it's converted on the way in: still the
picture it was, the right way up, no bigger than MAX_EDGE, and without the
metadata a phone puts in, its position above all.
"""

import io
import json

import pillow_heif
from PIL import Image
from django.test import Client, TestCase

from conversations.models import Media, Mood, ThinkingEntity
from conversations.services import media, mood_auth

pillow_heif.register_heif_opener()


def heic(size=(640, 480), colour=(200, 40, 40), exif=None):
    picture = Image.new('RGB', size, colour)
    out = io.BytesIO()
    picture.save(out, format='HEIF', quality=90, **({'exif': exif} if exif else {}))
    return out.getvalue()


def gps_exif():
    """EXIF saying where the photo was taken, as a phone writes it."""
    exif = Image.Exif()
    exif[0x8825] = {1: 'N', 2: (40.0, 26.0, 46.0), 3: 'W', 4: (79.0, 58.0, 56.0)}  # GPSInfo
    exif[0x010F] = 'Apple'  # Make
    return exif.tobytes()


class HeicTest(TestCase):

    def test_heic_is_recognised_by_its_bytes(self):
        data = heic()
        self.assertTrue(media.is_heif(data))
        self.assertFalse(media.is_heif(b'\x89PNG\r\n\x1a\n' + b'\0' * 32))
        self.assertIsNone(media.sniff(data), 'HEIC must never be stored as itself')

    def test_stored_as_a_jpeg_the_same_picture(self):
        stored = media.store(heic(colour=(200, 40, 40)))
        self.assertEqual(stored.mime, 'image/jpeg')
        self.assertTrue(stored.url.endswith('.jpg'))
        with Image.open(io.BytesIO(bytes(stored.data))) as back:
            self.assertEqual(back.size, (640, 480))
            r, g, b = back.convert('RGB').getpixel((320, 240))
            self.assertTrue(abs(r - 200) < 12 and abs(g - 40) < 12 and abs(b - 40) < 12)

    def test_where_it_was_taken_does_not_come_with_it(self):
        stored = media.store(heic(exif=gps_exif()))
        with Image.open(io.BytesIO(bytes(stored.data))) as back:
            exif = back.getexif()
            self.assertNotIn(0x8825, exif)  # no GPS
            self.assertNotIn(0x010F, exif)
        self.assertNotIn(b'Exif', bytes(stored.data)[:4096])

    def test_a_huge_photo_is_brought_down_to_size(self):
        stored = media.store(heic(size=(6000, 4000)))
        with Image.open(io.BytesIO(bytes(stored.data))) as back:
            self.assertEqual(max(back.size), media.MAX_EDGE)
            self.assertEqual(back.size, (4096, 2731))

    def test_the_same_photo_twice_is_stored_once(self):
        data = heic()
        self.assertEqual(media.store(data).pk, media.store(data).pk)
        self.assertEqual(Media.objects.count(), 1)

    def test_a_broken_heic_is_refused_not_stored(self):
        broken = heic()[:200]
        self.assertTrue(media.is_heif(broken))
        self.assertIsNone(media.store(broken))
        self.assertEqual(Media.objects.count(), 0)

    def test_attached_in_the_composer(self):
        justin = ThinkingEntity.objects.create(name='justin', is_biological_human=True)
        Mood.objects.create(slug='uploads-and-embeds', title='uploads-and-embeds')
        _, token = mood_auth.enrol_device(justin, 'phone')
        client = Client()
        client.cookies[mood_auth.COOKIE] = token
        answer = client.post('/api/moods/uploads-and-embeds/media/', heic(), content_type='application/octet-stream')
        self.assertEqual(answer.status_code, 201, answer.content)
        self.assertTrue(json.loads(answer.content)['markdown'].endswith('.jpg)'))
        refused = client.post('/api/moods/uploads-and-embeds/media/', b'not a picture at all',
                              content_type='application/octet-stream')
        self.assertIn('HEIC', json.loads(refused.content)['error'])
