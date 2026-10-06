"""Notifications with magenta closed: Web Push.

The bell on the Moods page tells its person of mentions and answers
(views_moods.notices_for). With a page open, the page does that itself;
with magenta closed, only a push can. Turning the bell on in a browser
that can be pushed to subscribes it at its push service (Google's,
Mozilla's...), and the endpoint and keys it gives are kept as a
PushSubscription of that device.

Every few seconds (nudge: asked by the runner's pulse, and by every open
page) each person with a subscription is checked for new notices; each new
one is sent once -- encrypted for the device, so the push service can't
read it, and signed with our VAPID key -- unless they've read that Mood
since. The service worker (views_moods.SERVICE_WORKER) shows it, unless a
magenta page is in front of them: that page tells them itself.

settings.WEBPUSH_VAPID_PRIVATE_KEY unset: no push, and the bell works only
while a page is open, as before.
"""

import base64
import json
import logging
import threading
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone
from django.utils.dateparse import parse_datetime

EVERY = 10          # seconds between looks
OVERLAP = 120       # each look reaches this far before the last: a slow write isn't missed (sent ones are remembered)
TTL = 3600          # an undelivered push is dropped after this, and a look never reaches further back
SENT_FOR = 2 * 86400  # how long a sent notice is remembered

log = logging.getLogger(__name__)


def _configured():
    """The key as set, without what a vault or an .env may wrap it in: spaces, quotes."""
    return (getattr(settings, 'WEBPUSH_VAPID_PRIVATE_KEY', '') or '').strip().strip('"\'').strip()


def _decoded(text):
    return base64.urlsafe_b64decode(text + '=' * (-len(text) % 4))


def _der_length(data, start):
    """How many bytes the DER element at `start` says it takes, header included (0 if it can't say)."""
    if start + 2 > len(data):
        return 0
    first = data[start + 1]
    if first < 0x80:
        return 2 + first
    count = first & 0x7f
    if not 1 <= count <= 4 or start + 2 + count > len(data):
        return 0
    return 2 + count + int.from_bytes(data[start + 2:start + 2 + count], 'big')


def _key():
    """The configured key as the push library reads it best -- base64url of the
    raw private number -- from whichever shape it was made in: that already;
    DER (`openssl ... -outform DER`), with or without the EC parameters
    openssl puts first unless told -noout; PEM; in base64 or base64url."""
    from cryptography.hazmat.primitives import serialization
    key = _configured()
    if not key:
        return ''
    try:
        if '-----BEGIN' in key:
            loaded = serialization.load_pem_private_key(key.replace('\\n', '\n').encode(), password=None)
        else:
            key = key.replace('+', '-').replace('/', '_').rstrip('=')  # plain base64, as `base64` writes it
            data = _decoded(key)
            if len(data) == 32:
                return key
            loaded = None
            for start in [0] + [i for i in range(1, len(data)) if data[i] == 0x30]:  # a key after the parameters
                # Exactly as long as it says it is: a stray character after the key (a copied
                # prompt mark, a literal \\n) decodes to a byte or two DER won't take.
                for piece in (data[start:start + _der_length(data, start)], data[start:]):
                    try:
                        loaded = serialization.load_der_private_key(piece, password=None)
                        break
                    except ValueError:
                        continue
                if loaded is not None:
                    break
            if loaded is None:
                return key
        return _b64url(loaded.private_numbers().private_value.to_bytes(32, 'big'))
    except Exception:
        return key  # as it is: problem() says what's wrong with it


def problem():
    """Why push is off though a key is set ('' if it's on, or no key is set):
    the library missing, or a key it can't read. Never the key itself --
    only what shape it seems to be."""
    raw = _configured()
    if not raw:
        return ''
    key = _key()
    try:
        from py_vapid import Vapid
        Vapid.from_string(key)
    except Exception as e:
        why = f'{type(e).__name__}: {e}'.replace(raw, '<the key>').replace(key, '<the key>')[:160]
        try:
            data = _decoded(key)
            shape = f'{len(data)} bytes once decoded'
            if len(data) == 65 and data[0] == 4:
                shape += ": that's a public key -- the private one goes in the vault"
        except Exception:
            shape = 'not base64'
        return f"WEBPUSH_VAPID_PRIVATE_KEY is set but can't be used: {shape} ({why})"
    return ''


def enabled():
    """A key is set, and the push library can read it. A key it can't read
    turns push off -- and says why (problem(), /api/push/) -- never the page."""
    if not _configured():
        return False
    from django.core.cache import cache as shared
    why = shared.get('push:problem')
    if why is None:
        why = problem()
        shared.set('push:problem', why, 300)
        if why:
            log.error('push is off: %s', why)
    return not why


def _b64url(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b'=').decode()


def new_private_key():
    """A fresh VAPID private key, base64url: for the vault (manage.py vapid_key)."""
    from cryptography.hazmat.primitives.asymmetric import ec
    key = ec.generate_private_key(ec.SECP256R1())
    return _b64url(key.private_numbers().private_value.to_bytes(32, 'big'))


def public_key():
    """The applicationServerKey browsers subscribe with: our key's public half,
    base64url, as an uncompressed P-256 point. '' if push is off."""
    if not enabled():
        return ''
    from cryptography.hazmat.primitives import serialization
    from py_vapid import Vapid
    point = Vapid.from_string(_key()).public_key.public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return _b64url(point)


def notification(notice):
    """What a notice says on a lock screen: as the page's own notification does."""
    from conversations.models import Mood
    turn = notice['turn']
    mood = Mood.objects.filter(slug=notice['mood']).first()
    title = f"{turn['sender']} answered you" if notice['kind'] == 'answer' else f"{turn['sender']} mentioned you"
    where = f"{(mood.title or mood.slug)}: " if mood else ''
    return {'title': title, 'body': (where + (turn.get('text') or ''))[:200], 'tag': turn['id'],
            'data': {'slug': notice['mood'], 'id': turn['id']}}


def send(subscription, payload, now=None):
    """'sent', 'gone' (the push service has forgotten it: so do we) or 'failed: <why>'."""
    from pywebpush import WebPushException, webpush
    try:
        webpush({'endpoint': subscription.endpoint, 'keys': {'p256dh': subscription.p256dh, 'auth': subscription.auth}},
                data=json.dumps(payload), vapid_private_key=_key(),
                vapid_claims={'sub': settings.WEBPUSH_CONTACT}, ttl=TTL, timeout=10)
    except WebPushException as e:
        status = getattr(e.response, 'status_code', None)
        if status in (404, 410):
            subscription.delete()
            return 'gone'
        return f'failed: {status or e}'
    except Exception as e:  # the network: this one is missed, the next look goes on
        return f'failed: {type(e).__name__}'
    type(subscription).objects.filter(pk=subscription.pk).update(last_sent_at=now or timezone.now())
    return 'sent'


def sweep(now=None):
    """Send what's new since the last look to everyone subscribed. How many notices were sent."""
    from conversations.models import PushSubscription, ReadMark
    from conversations.services.mood_auth import device_state
    from conversations.views_moods import notices_for
    now = now or timezone.now()
    last = cache.get('push:cursor')
    since = max(min(last or now, now) - timedelta(seconds=OVERLAP), now - timedelta(seconds=TTL))
    theirs = {}
    for sub in PushSubscription.objects.select_related('device'):
        if device_state(sub.device) == 'live':
            theirs.setdefault(sub.device.entity_id, []).append(sub)
    told = 0
    for name, subs in theirs.items():
        seen = dict(ReadMark.objects.filter(entity_id=name).values_list('mood__slug', 'seen_at'))
        for notice in reversed(notices_for(name, since)):  # oldest first
            at = parse_datetime(notice['turn']['created_at'])
            if seen.get(notice['mood']) and at and seen[notice['mood']] >= at:
                continue  # read there since
            if not cache.add(f"push:sent:{name}:{notice['turn']['id']}", 1, SENT_FOR):
                continue  # told already
            payload = notification(notice)
            for sub in subs:
                send(sub, payload, now)
            told += 1
    cache.set('push:cursor', now, None)
    return told


def nudge():
    """Look, in the background, if it's been EVERY seconds. Cheap to call often."""
    if not enabled() or not cache.add('push:lock', 1, EVERY):
        return False
    threading.Thread(target=_sweep_and_close, daemon=True).start()
    return True


def _sweep_and_close():
    from django.db import connection
    try:
        sweep()
    except Exception:
        pass  # the next look tries again
    finally:
        connection.close()
