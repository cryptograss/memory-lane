"""The login tool draws its link as a QR code without anything installed.

The encoder (tools/mood_login.py) was checked against a real decoder (jsQR)
for versions 1-24, Unicode included, on 2026-10-03; these fingerprints pin
two of those outputs, so a change that breaks it shows here.
"""

import hashlib
import importlib.util
from pathlib import Path
from unittest import TestCase

_spec = importlib.util.spec_from_file_location(
    'mood_login', Path(__file__).resolve().parents[2] / 'tools' / 'mood_login.py')
mood_login = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mood_login)


def fingerprint(modules):
    return hashlib.sha256(''.join('1' if m else '0' for row in modules for m in row).encode()).hexdigest()[:16]


class QRTest(TestCase):

    def test_a_login_link_is_the_code_a_decoder_read(self):
        link = 'https://memory-lane.maybelle.cryptograss.live/motions/auth/' + 'aB3-_' * 12  # the link the decoder read
        modules = mood_login.qr_matrix(link)
        self.assertEqual(len(modules), 45)  # version 7: the first with version blocks
        self.assertEqual(fingerprint(modules), 'cc292c99e3dd6a8f')

    def test_a_longer_text_takes_a_larger_version(self):
        modules = mood_login.qr_matrix('w' * 300)
        self.assertEqual(len(modules), 69)  # version 13
        self.assertEqual(fingerprint(modules), 'c1ae359df049961b')

    def test_finders_sit_in_three_corners(self):
        modules = mood_login.qr_matrix('hi')
        n = len(modules)
        for x, y in ((0, 0), (n - 7, 0), (0, n - 7)):
            self.assertTrue(all(modules[y][x + i] and modules[y + 6][x + i] for i in range(7)))
            self.assertTrue(modules[y + 3][x + 3])

    def test_the_terminal_drawing_is_dark_on_light_whatever_the_theme(self):
        art = mood_login.qr_terminal('hi')
        self.assertIn('\x1b[97;107m▀', art)  # light on light: the quiet zone
        self.assertTrue(all(line.endswith('\x1b[0m') for line in art.split('\n')))
