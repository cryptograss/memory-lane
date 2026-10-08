"""Taking back what was said in a Mood: editing it, or deleting it.

Something pasted by mistake -- a key, a password, a thing not meant for
everyone -- has to be able to go, for real: not hidden, removed.

- **Deleting** a message (whoever wrote it; or an admin, signed in with
  their SSH key, anyone's -- an agent's too) takes its words out of the
  record. The thread keeps a line where it was: "deleted by justin".
  Pictures and a memo's recording that came with it go too, when nothing
  else uses them, and so does any reading of it aloud.
- **Editing** one (whoever wrote it, from the web; never a signed
  statement) replaces its words, marked "(edited)". No history is kept:
  history is where a secret would survive.

Either way the record's own copies go the same way: the wakes that carried
it to an agent (the poller quotes what was said, and keeps the wake) are
scrubbed of it. What can't be called back is said, not pretended away: an
agent session a wake already reached has seen it (MessageChange.reached),
and a notification already sent was sent.

**Taken back, never lost.** The same power in the wrong hands -- a captured
account, a captured key -- would erase history. So nothing is taken back
without being sealed first: the words, and the pictures and recordings that
go with them, encrypted to the recovery key (services/sealing.py), whose
private half the server never holds. Sealed, they're unreadable here; with
that key they're read and put back (manage.py unseal; or an admin's page,
opening them in the browser -- see put_back). With no recovery key set,
nothing can be taken back at all. And how much can be is bounded:

- your own messages, deleted within a week of saying them, edited within a
  day; older, only an admin;
- each person (an admin too) at most PER_HOUR in an hour and PER_DAY in a
  day;
- an admin taking back someone else's words, or anyone taking back a burst
  of them, is said in #general (services/access.py) for everyone to see.
"""

from datetime import timedelta

DELETED = '[deleted]'
WAKE_SENDER = 'mood-poller'
_MIN_LINE = 4  # a line shorter than this is too common to scrub by
DELETE_OWN_WITHIN = timedelta(days=7)
EDIT_OWN_WITHIN = timedelta(days=1)
PER_HOUR, PER_DAY = 10, 30
BURST, BURST_WITHIN = 5, timedelta(minutes=10)


class Refused(Exception):
    def __init__(self, message, status=403):
        super().__init__(message)
        self.status = status


def is_admin(device):
    from django.conf import settings
    return device is not None and device.tier == 'key' and device.entity_id in getattr(settings, 'MOOD_ADMINS', ())


def _age(message):
    from django.utils import timezone
    return timezone.now() - message.created_at


def may_delete(message, device):
    """Yours, said within a week; or anyone's, as an admin."""
    if device is None:
        return False
    return is_admin(device) or (message.sender_id == device.entity_id and _age(message) <= DELETE_OWN_WITHIN)


def may_edit(message, device):
    """Yours, posted from the web within a day; never a signed statement."""
    from .mood_view import POSTED
    return (device is not None and message.sender_id == device.entity_id and message.source_file in POSTED
            and message.source_file != 'mood-attest' and _age(message) <= EDIT_OWN_WITHIN)


def _ready(by):
    """The recovery key, if taking back may go ahead for `by` now; Refused with why if not."""
    from django.conf import settings
    from django.utils import timezone
    from conversations.models import SealedCopy
    key = getattr(settings, 'MOOD_RECOVERY_PUBLIC_KEY', '')
    if not key:
        raise Refused("taking back isn't set up here yet: there's no recovery key to keep what's taken back",
                      status=503)
    now = timezone.now()
    mine = SealedCopy.objects.filter(by=by).exclude(kind='replaced')  # putting back isn't taking back
    if mine.filter(at__gte=now - timedelta(hours=1)).count() >= PER_HOUR:
        raise Refused(f'that is {PER_HOUR} taken back in the last hour: wait a while, or ask an admin', status=429)
    if mine.filter(at__gte=now - timedelta(days=1)).count() >= PER_DAY:
        raise Refused(f'that is {PER_DAY} taken back today: ask an admin', status=429)
    return key


def _seal(message, kind, by, key, media):
    """Keep what's going, sealed to the recovery key, before anything goes.

    With it, the SHA-256 of exactly what was sealed (SealedCopy.digest): a
    random salt inside makes it no help guessing the words, and it lets
    put_back know the very words sealed when it's handed them.
    """
    import base64
    import hashlib
    import json
    import secrets
    from conversations.models import SealedCopy
    from .sealing import seal
    payload = {'content': message.content, 'source_file': message.source_file, 'sender': message.sender_id,
               'media': [{'sha': m.sha256, 'mime': m.mime, 'license': getattr(m, 'license', ''),
                          'added_by': m.added_by_id, 'data': base64.b64encode(bytes(m.data)).decode()} for m in media],
               'salt': secrets.token_urlsafe(32)}
    data = json.dumps(payload).encode()
    SealedCopy.objects.create(message_id=message.pk, mood_slug=message.mood.slug if message.mood_id else '',
                              kind=kind, by=by, sealed=seal(data, key), digest=hashlib.sha256(data).hexdigest())


def _say_so(message, by, kind):
    """An admin taking back someone else's words, or anyone a burst of them: said in #general."""
    from django.core.cache import cache
    from django.utils import timezone
    from conversations.models import SealedCopy
    from .access import announce
    where = message.mood.slug if message.mood_id else ''
    if message.sender_id != by:
        announce('took-back', message.sender_id, by=by, label=f'{kind} in #{where}')
    recent = SealedCopy.objects.filter(by=by, at__gte=timezone.now() - BURST_WITHIN).exclude(kind='replaced').count()
    if recent >= BURST and cache.add(f'retract:burst:{by}', 1, int(BURST_WITHIN.total_seconds())):
        announce('taking-back-a-lot', by, label=f'{recent} in {int(BURST_WITHIN.total_seconds() // 60)} minutes')


def change_of(message):
    from conversations.models import MessageChange
    return MessageChange.objects.filter(message_id=message.pk).first()


def delete(message, by):
    """Take a message's words out of the record. {'scrubbed', 'reached'}."""
    from conversations.models import MessageChange
    from .mood_view import prose
    change = change_of(message)
    if change and change.kind == 'deleted':
        return {'scrubbed': 0, 'reached': change.reached}
    key = _ready(by)
    old = prose(message.content) or ''
    going = _media_going(message, old, '')
    _seal(message, 'deleted', by, key, going)
    content = DELETED if isinstance(message.content, str) else [{'type': 'text', 'text': DELETED}]
    type(message).objects.filter(pk=message.pk).update(content=content)
    message.content = content  # a caller holding it seals what it says now, next time
    for media in going:
        media.delete()
    _forget_readings(message)
    scrubbed, reached = _scrub_wakes(message, old, '')
    MessageChange.objects.update_or_create(message=message, defaults={
        'mood': message.mood, 'kind': 'deleted', 'by': by, 'reached': reached})
    _forget_cached(message)
    _say_so(message, by, 'a message deleted')
    return {'scrubbed': scrubbed, 'reached': reached}


def edit(message, text, by):
    """Replace a message's words. {'scrubbed', 'reached'}."""
    from conversations.models import MessageChange
    from .mood_view import prose
    from .redaction import redact
    key = _ready(by)
    old = prose(message.content) or ''
    text, _ = redact(text)
    going = _media_going(message, old, text)
    _seal(message, 'edited', by, key, going)
    type(message).objects.filter(pk=message.pk).update(content=text)
    message.content = text
    for media in going:
        media.delete()
    _forget_readings(message)
    scrubbed, reached = _scrub_wakes(message, old, text)
    MessageChange.objects.update_or_create(message=message, defaults={
        'mood': message.mood, 'kind': 'edited', 'by': by, 'reached': reached})
    _forget_cached(message)
    _say_so(message, by, 'a message edited')
    return {'scrubbed': scrubbed, 'reached': reached}


def opened(copy, data):
    """What a sealed copy holds, from whoever opened it (bytes): only if they're the very bytes sealed.

    The page opens seals in the browser, the key never leaving it, and hands
    back what it read to have it put back. The digest says whether that's
    what was sealed: words nobody ever said can't be "put back" into anyone's
    mouth, by a captured admin session or anyone else.
    """
    import hashlib
    import hmac
    import json
    if not copy.digest or not hmac.compare_digest(hashlib.sha256(data).hexdigest(), copy.digest):
        raise Refused("that isn't what was sealed there", status=400)
    return json.loads(data)


def put_back(copy, payload, by):
    """A message as a sealed copy has it, pictures and all. What it says now is sealed first, as any takeback is."""
    import base64
    from django.conf import settings
    from conversations.models import Media, Message, MessageChange, ThinkingEntity
    from .mood_view import prose
    message = Message.objects.filter(id=copy.message_id).first()
    if message is None:
        raise Refused(f'{copy.message_id}: the message itself is gone from the record', status=404)
    now = prose(message.content) or ''
    back = prose(payload['content']) or ''
    key = getattr(settings, 'MOOD_RECOVERY_PUBLIC_KEY', '')
    going = []
    if key and now != DELETED:  # a putting back that was a mistake can itself be put back
        going = _media_going(message, now, back)
        _seal(message, 'replaced', by, key, going)
    for m in payload['media']:
        if not Media.objects.filter(sha256=m['sha']).exists():
            data = base64.b64decode(m['data'])
            Media.objects.create(sha256=m['sha'], mime=m['mime'], data=data, size=len(data),
                                 added_by=ThinkingEntity.objects.filter(name=m.get('added_by')).first(),
                                 **({'license': m['license']} if m.get('license') else {}))
    type(message).objects.filter(pk=message.pk).update(content=payload['content'])
    for media in going:
        media.delete()
    _forget_readings(message)
    MessageChange.objects.update_or_create(message=message, defaults={
        'mood': message.mood, 'kind': 'restored', 'by': by, 'reached': []})
    _forget_cached(message)
    return message


def mark(payload, change):
    """A turn's payload as it now stands: "(edited)", "(restored)", or the line a deletion leaves."""
    if change is None:
        return payload
    if change.kind == 'edited':
        return {**payload, 'edited': change.at.isoformat()}
    if change.kind == 'restored':
        return {**payload, 'restored': change.at.isoformat()}
    return {**payload, 'deleted': {'by': change.by, 'at': change.at.isoformat()}, 'text': '', 'html': '',
            'mentions': [], 'reply': None, 'voiced': False, 'voices': []}


# --- the copies -------------------------------------------------------------------

def _gone_lines(old, new):
    """The lines of what was said that aren't in what's said now: what to scrub."""
    kept = set(line.strip() for line in (new or '').splitlines())
    return [line.strip() for line in (old or '').splitlines()
            if len(line.strip()) >= _MIN_LINE and line.strip() not in kept]


def _scrub(value, lines, mark):
    if isinstance(value, str):
        for line in lines:
            value = value.replace(line, mark)
        return value
    if isinstance(value, list):
        return [_scrub(v, lines, mark) for v in value]
    if isinstance(value, dict):
        return {k: _scrub(v, lines, mark) for k, v in value.items()}
    return value


def _scrub_wakes(message, old, new):
    """Scrub what went from the wakes since that quoted it. (how many, [agents they reached])."""
    from django.db.models import Q, TextField
    from django.db.models.functions import Cast
    from conversations.models import Message, ThinkingEntity
    lines = sorted(_gone_lines(old, new), key=len, reverse=True)  # longest first: a line inside another
    if not lines:
        return 0, []
    mark = DELETED if not new else '[edited]'
    probe = Q()
    for line in lines[:20]:
        probe |= Q(as_text__contains=line.replace('\\', '\\\\').replace('"', '\\"'))
    rows = (Message.objects.filter(sender_id=WAKE_SENDER, created_at__gte=message.created_at)
            .annotate(as_text=Cast('content', TextField())).filter(probe))
    scrubbed, sessions = 0, set()
    for row in rows:
        content = _scrub(row.content, lines, mark)
        if content != row.content:
            type(row).objects.filter(pk=row.pk).update(content=content)
            scrubbed += 1
            if row.session_id:
                sessions.add(row.session_id)
    agents = set(ThinkingEntity.objects.filter(is_biological_human=False).values_list('name', flat=True)) - {WAKE_SENDER}
    reached = sorted(set(Message.objects.filter(session_id__in=sessions, sender_id__in=agents)
                         .values_list('sender_id', flat=True))) if sessions else []
    return scrubbed, reached


def _media_going(message, old, new):
    """The pictures and recordings in what goes that nothing else uses: to seal, then remove."""
    from django.db.models import TextField
    from django.db.models.functions import Cast
    from conversations.models import Media, Message
    from .media import AUDIO_PATH, MEDIA_PATH
    kept = set(m.group(1) for path in (MEDIA_PATH, AUDIO_PATH) for m in path.finditer(new or ''))
    going = []
    for sha in set(m.group(1) for path in (MEDIA_PATH, AUDIO_PATH) for m in path.finditer(old or '')) - kept:
        elsewhere = (Message.objects.exclude(pk=message.pk).exclude(source_file='voice')
                     .annotate(as_text=Cast('content', TextField())).filter(as_text__contains=sha).exists())
        if not elsewhere:
            going.extend(Media.objects.filter(sha256=sha))
    return going


def _forget_readings(message):
    """Any reading of it aloud (services/voice.py keeps them): gone, audio and all."""
    from conversations.models import Media, Message
    readings = Message.objects.filter(source_file='voice', content__message=str(message.id))
    shas = [c.get('media') for c in readings.values_list('content', flat=True) if isinstance(c, dict)]
    Media.objects.filter(sha256__in=[s for s in shas if s]).delete()
    readings.delete()


def _forget_cached(message):
    from django.core.cache import cache
    cache.delete(f'links:card:{message.id}')
    cache.delete_many([f'links:ref:{str(message.id)[:8]}'])


def copies_of(message_id):
    """[{'copy', 'kind', 'by', 'at', 'sealed'}] kept of a message, oldest first: each, what it said before that change."""
    from conversations.models import SealedCopy
    return [{'copy': c.id, 'kind': c.kind, 'by': c.by, 'at': c.at.isoformat(), 'sealed': c.sealed}
            for c in SealedCopy.objects.filter(message_id=message_id).order_by('at', 'id')]


def taken_back_in(mood, limit=200):
    """[{'copy', 'message', 'sender', 'kind', 'by', 'at'}], newest first: who took back what in a Mood, and when."""
    from conversations.models import Message, SealedCopy
    copies = list(SealedCopy.objects.filter(mood_slug=mood.slug).order_by('-at', '-id')[:limit])
    senders = dict(Message.objects.filter(id__in=[c.message_id for c in copies]).values_list('id', 'sender_id'))
    return [{'copy': c.id, 'message': str(c.message_id), 'sender': senders.get(c.message_id), 'kind': c.kind,
             'by': c.by, 'at': c.at.isoformat()} for c in copies]


def changes_since(mood, since):
    """[{'id', 'kind', 'by', 'at'}] for what was edited or deleted in a Mood after `since`."""
    from conversations.models import MessageChange
    return [{'id': str(c.message_id), 'kind': c.kind, 'by': c.by, 'at': c.at.isoformat()}
            for c in MessageChange.objects.filter(mood=mood, at__gt=since).order_by('at')]


