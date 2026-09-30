"""Pattern redaction: secrets nobody told the scrubber about.

The scrubber service replaces secrets it was given in advance. It cannot
know a token pasted today, and when it is unreachable, lines pass through
unscrubbed. This layer runs inside memory-lane, on every string value of
every line before it is stored, so it is never down. The record is public;
anything that reaches it should be safe to read by anyone, for a long time.

It looks for shapes, not values:
  - token formats with a recognisable prefix (GitHub, Anthropic, OpenAI,
    AWS, Slack, Stripe, Google, npm, JWTs, PEM private keys);
  - NAME=value / "name": "value" where the name says secret, token,
    password or key -- keeping the name, so the record still says what
    was there;
  - credentials inside URLs, and Authorization headers.

Deliberately not matched: bare 64-hex strings. Here they are mostly
transaction and block hashes, and an Ethereum private key is caught by
the name it is assigned to. Placeholders ({{ ansible_var }}, ${VAR},
<your-token>, changeme) are left alone.
"""

import json
import re

MARK = '[REDACTED]'

_TOKENS = [
    r'-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----',
    r'\bgh[pousr]_[A-Za-z0-9]{36,}\b',
    r'\bgithub_pat_[A-Za-z0-9_]{50,}\b',
    r'\bsk-ant-[A-Za-z0-9_-]{20,}',
    r'\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{32,}',
    r'\b(?:AKIA|ASIA)[0-9A-Z]{16}\b',
    r'\bxox[abposr]-[A-Za-z0-9-]{10,}',
    r'\b(?:sk|rk)_live_[A-Za-z0-9]{16,}',
    r'\bAIza[0-9A-Za-z_-]{35}\b',
    r'\bnpm_[A-Za-z0-9]{36}\b',
    r'\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}',
]
TOKEN = re.compile('|'.join(f'(?:{t})' for t in _TOKENS), re.S)

# Bounded on both sides: an unbounded [\w.-]* around the alternation is
# quadratic on a long run of word characters, and a base64 screenshot is a
# megabyte of exactly that.
# The keyword must end a word: `github_token`, `TOKEN_2`, `password` are
# names for secrets; `tokenId`, `input_tokens`, `passwordFile` are not.
_SECRET_NAME = r'(?<![A-Za-z0-9_.-])[A-Za-z0-9_.-]{0,40}?(?:secret|token|passw(?:or)?d|passwd|api[_-]?key|private[_-]?key|access[_-]?key|credentials?|mnemonic|seed[_-]?phrase)(?![a-z])(?:[_.-][A-Za-z0-9_.-]{0,40})?'
# NAME=value, NAME: value, "name": "value". The value stops at whitespace,
# quotes, and a backslash (a JSON-escaped newline inside a string), and must
# end where a value ends, so code like `output_tokens = models.Field(...)`
# is not taken for a secret. (A password containing brackets is missed.)
ASSIGNMENT = re.compile(
    rf'(?i)(?P<name>["\']?{_SECRET_NAME}["\']?\s*[:=]\s*["\']?)(?P<value>[^\s"\'\\,;(){{}}\[\]]{{4,}})(?=$|[\s"\'\\,;)}}\]])')
URL_CREDENTIALS = re.compile(r'(?P<head>\b[a-z][a-z0-9+.-]*://[^\s:/@"\']+:)(?P<value>[^\s@/"\']+)(?=@)')
AUTH_HEADER = re.compile(r'(?i)(?P<head>\bauthorization\s*[:=]\s*["\']?(?:bearer|basic|token)\s+)(?P<value>[A-Za-z0-9._~+/=-]{8,})')

# Not secrets: placeholders, templates, numbers (token counts, file:line),
# paths, and code -- a word, a snake_case or dotted identifier. A generated
# secret has a digit or a symbol in it, or mixes cases at length.
_LOOKS_SECRET = re.compile(r'[0-9+/=@#$%^&*!-]|^(?=.*[a-z])(?=.*[A-Z]).{16,}$')
_CODE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$')
_PLACEHOLDER = re.compile(r'^(?:[0-9.:]+|[/~].*|\./.*|your[-_].*|.*example.*|.*placeholder.*|\{\{.*|\$\{?.*|<.*|%.*|\*+|x+|\.\.\.|changeme|password|secret|token|none|null|true|false|redacted.*|\[redacted\].*)$', re.I)


def redact(text):
    """(text with secrets replaced by [REDACTED], how many were replaced)."""
    count = 0

    def token(match):
        nonlocal count
        count += 1
        return MARK

    def keep(match):
        nonlocal count
        value = match.group('value')
        if (_PLACEHOLDER.match(value) or value.startswith(MARK) or not _LOOKS_SECRET.search(value)
                or (_CODE.match(value) and not re.search(r'[0-9]', value))):
            return match.group(0)
        count += 1
        return match.group(1) + MARK

    text = TOKEN.sub(token, text)
    text = AUTH_HEADER.sub(keep, text)
    text = URL_CREDENTIALS.sub(keep, text)
    text = ASSIGNMENT.sub(keep, text)
    return text, count


def redact_value(value):
    """Redact every string inside a parsed JSON value. Keys are left alone."""
    count = 0
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, list):
        out = []
        for item in value:
            item, n = redact_value(item)
            out.append(item)
            count += n
        return out, count
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            item, n = redact_value(item)
            out[key] = item
            count += n
        return out, count
    return value, 0


def redact_line(line):
    """A JSONL line with its string values redacted; unparseable lines are redacted as text."""
    try:
        event = json.loads(line)
    except (json.JSONDecodeError, TypeError):
        return redact(line)[0]
    event, count = redact_value(event)
    return json.dumps(event) if count else line
