"""Pattern redaction catches secrets by shape and leaves the rest of the record alone."""

import json

from django.test import SimpleTestCase

from conversations.services.redaction import MARK, redact, redact_line

# Built at runtime so no scanner mistakes this file for a leak.
GH = 'ghp_' + 'a1B2' * 9
ANT = 'sk-ant-' + 'api03-' + 'Zz9_' * 8
AWS = 'AKIA' + 'ABCDEFGHIJKLMNOP'
JWT = 'eyJ' + 'hbGciOiJIUzI1NiJ9' + '.eyJ' + 'zdWIiOiIxMjM0NSJ9' + '.' + 'c2lnbmF0dXJlLXZhbHVl'


class RedactTest(SimpleTestCase):

    def assertRedacted(self, text, keeps=()):
        out, n = redact(text)
        self.assertGreater(n, 0, text)
        self.assertIn(MARK, out)
        for kept in keeps:
            self.assertIn(kept, out)
        return out

    def assertUntouched(self, text):
        self.assertEqual(redact(text), (text, 0))

    def test_token_formats(self):
        for token in (GH, ANT, AWS, JWT, 'xoxb-' + '1234567890-abcdefghij'):
            with self.subTest(token=token[:6]):
                out = self.assertRedacted(f'use {token} now', keeps=['use ', ' now'])
                self.assertNotIn(token, out)

    def test_private_key_block(self):
        pem = '-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAA\n-----END OPENSSH PRIVATE KEY-----'
        out = self.assertRedacted(f'key:\n{pem}\ndone', keeps=['done'])
        self.assertNotIn('b3BlbnNzaC1rZXktdjEAAAA', out)

    def test_assignments_keep_the_name(self):
        self.assertEqual(redact('POSTGRES_PASSWORD=hunter22 next')[0], f'POSTGRES_PASSWORD={MARK} next')
        self.assertEqual(redact('"api_key": "abcd1234efgh"')[0], f'"api_key": "{MARK}"')
        self.assertEqual(redact('export GITHUB_TOKEN="zq8Fh2kLm0"')[0], f'export GITHUB_TOKEN="{MARK}"')
        # The price of not redacting code: a short letters-only value is kept.
        self.assertEqual(redact('password=hunter')[0], 'password=hunter')
        self.assertEqual(redact('password=Correct-Horse-Battery')[0], f'password={MARK}')
        self.assertEqual(redact('PRIVATE_KEY=0x' + 'ab' * 32)[0], f'PRIVATE_KEY={MARK}')

    def test_value_stops_at_an_escaped_newline(self):
        # Inside a raw JSON line, a newline is backslash-n: don't eat past it.
        self.assertEqual(redact(r'DB_PASSWORD=hunter22\nDB_HOST=db')[0], rf'DB_PASSWORD={MARK}\nDB_HOST=db')

    def test_url_credentials_and_auth_headers(self):
        self.assertEqual(redact('postgres://magent:s3cretpw@10.0.0.2:5432/db')[0],
                         f'postgres://magent:{MARK}@10.0.0.2:5432/db')
        self.assertEqual(redact('-H "Authorization: Bearer abcdef123456"')[0],
                         f'-H "Authorization: Bearer {MARK}"')

    def test_placeholders_and_templates_are_left_alone(self):
        for text in ('password: {{ vault_db_password }}', 'TOKEN=${GITHUB_TOKEN}', 'API_KEY=<your-key>',
                     'password=changeme', 'secret: ****', 'SECRET_KEY=[REDACTED]'):
            with self.subTest(text=text):
                self.assertUntouched(text)

    def test_the_rest_of_the_record_is_left_alone(self):
        for text in (
            'tx 0x' + 'ab' * 32 + ' at block 26084996',          # a transaction hash
            'commit 37c93b9 and 5bf305a1c0ffee',                 # SHAs
            'session 6e870f91-553e-4ad6-8e86-fa2b41e4011e',       # uuids
            'the token count was high; password policy is strict',  # words, no value
            'https://pickipedia.xyz/wiki/Tony_Rice',
            'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAINKHaQ2Nt6I jmyles@lukowala',  # a public key
            '"cache_read_input_tokens":12345,"output_tokens":249',  # usage counts
            'output_tokens = models.IntegerField(null=True, blank=True)',  # code
            "password_file = os.environ.get('ANSIBLE_VAULT_PASSWORD_FILE')",
            'uint16 tokenId = _nextTokenId;',                      # tokenId is not a token
            'const csrfToken = await mwn.getCsrfToken();',          # code
            'ansible_ssh_private_key_file: ~/.ssh/id_ed25519',      # a path
            'docker run -v $PWD:/app image',
            'at (__tests__/seed_phrase_test.js:49:5)',
            '"lgpassword": self.password,',
            '"token": csrf_token,',
            "print('export ANSIBLE_VAULT_PASSWORD=your-password')",
        ):
            with self.subTest(text=text[:30]):
                self.assertUntouched(text)


class RedactLineTest(SimpleTestCase):

    def test_every_string_value_in_a_line_is_redacted_keys_kept(self):
        line = json.dumps({'type': 'user', 'uuid': 'u1', 'message': {'role': 'user', 'content': [
            {'type': 'tool_result', 'tool_use_id': 'toolu_1', 'content': f'GITHUB_TOKEN={GH}'}]},
            'toolUseResult': {'stdout': f'export GITHUB_TOKEN={GH}'}})
        out = json.loads(redact_line(line))
        self.assertEqual(out['message']['content'][0]['content'], f'GITHUB_TOKEN={MARK}')
        self.assertEqual(out['toolUseResult']['stdout'], f'export GITHUB_TOKEN={MARK}')
        self.assertEqual(out['uuid'], 'u1')

    def test_a_clean_line_is_returned_byte_for_byte(self):
        line = '{"type": "user", "message": {"content": "hello"}}'
        self.assertIs(redact_line(line), line)

    def test_a_megabyte_of_base64_is_linear_not_quadratic(self):
        import base64, os, time
        blob = base64.b64encode(os.urandom(750_000)).decode()
        start = time.time()
        redact(blob)
        self.assertLess(time.time() - start, 2.0)
