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


def enabled():
    return bool(getattr(settings, 'WEBPUSH_VAPID_PRIVATE_KEY', ''))


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
    point = Vapid.from_string(settings.WEBPUSH_VAPID_PRIVATE_KEY).public_key.public_bytes(
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
                data=json.dumps(payload), vapid_private_key=settings.WEBPUSH_VAPID_PRIVATE_KEY,
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
