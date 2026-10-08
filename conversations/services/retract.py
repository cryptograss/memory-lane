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
"""

DELETED = '[deleted]'
WAKE_SENDER = 'mood-poller'
_MIN_LINE = 4  # a line shorter than this is too common to scrub by


def is_admin(device):
    from django.conf import settings
    return device is not None and device.tier == 'key' and device.entity_id in getattr(settings, 'MOOD_ADMINS', ())


def may_delete(message, device):
    return device is not None and (message.sender_id == device.entity_id or is_admin(device))


def may_edit(message, device):
    from .mood_view import POSTED
    return (device is not None and message.sender_id == device.entity_id and message.source_file in POSTED
            and message.source_file != 'mood-attest')


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
    old = prose(message.content) or ''
    content = DELETED if isinstance(message.content, str) else [{'type': 'text', 'text': DELETED}]
    type(message).objects.filter(pk=message.pk).update(content=content)
    _forget_media(message, old, '')
    _forget_readings(message)
    scrubbed, reached = _scrub_wakes(message, old, '')
    MessageChange.objects.update_or_create(message=message, defaults={
        'mood': message.mood, 'kind': 'deleted', 'by': by, 'reached': reached})
    _forget_cached(message)
    return {'scrubbed': scrubbed, 'reached': reached}


def edit(message, text, by):
    """Replace a message's words. {'scrubbed', 'reached'}."""
    from conversations.models import MessageChange
    from .mood_view import prose
    from .redaction import redact
    old = prose(message.content) or ''
    text, _ = redact(text)
    type(message).objects.filter(pk=message.pk).update(content=text)
    _forget_media(message, old, text)
    _forget_readings(message)
    scrubbed, reached = _scrub_wakes(message, old, text)
    MessageChange.objects.update_or_create(message=message, defaults={
        'mood': message.mood, 'kind': 'edited', 'by': by, 'reached': reached})
    _forget_cached(message)
    return {'scrubbed': scrubbed, 'reached': reached}


def mark(payload, change):
    """A turn's payload as it now stands: "(edited)", or the line a deletion leaves."""
    if change is None:
        return payload
    if change.kind == 'edited':
        return {**payload, 'edited': change.at.isoformat()}
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


def _forget_media(message, old, new):
    """Pictures and recordings in what went, gone too -- unless something else still uses them."""
    from django.db.models import TextField
    from django.db.models.functions import Cast
    from conversations.models import Media, Message
    from .media import AUDIO_PATH, MEDIA_PATH
    kept = set(m.group(1) for path in (MEDIA_PATH, AUDIO_PATH) for m in path.finditer(new or ''))
    for sha in set(m.group(1) for path in (MEDIA_PATH, AUDIO_PATH) for m in path.finditer(old or '')) - kept:
        elsewhere = (Message.objects.exclude(pk=message.pk).exclude(source_file='voice')
                     .annotate(as_text=Cast('content', TextField())).filter(as_text__contains=sha).exists())
        if not elsewhere:
            Media.objects.filter(sha256=sha).delete()


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


def changes_since(mood, since):
    """[{'id', 'kind', 'by', 'at'}] for what was edited or deleted in a Mood after `since`."""
    from conversations.models import MessageChange
    return [{'id': str(c.message_id), 'kind': c.kind, 'by': c.by, 'at': c.at.isoformat()}
            for c in MessageChange.objects.filter(mood=mood, at__gt=since).order_by('at')]


