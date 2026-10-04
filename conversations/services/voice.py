"""Voice: memos people speak, and messages read aloud. ElevenLabs does both.

A **memo** is recorded in the browser and posted here (views_auth.api_memo):
its audio is stored like an image (services/media.py), and Scribe turns it
into text the person can correct before sending. What's sent is a link to
the audio and the transcript, so agents read it as text.

**Reading aloud** turns one message into speech, on demand: someone presses
▶ (views_auth.api_speak). Agents can direct their own delivery with a block
anywhere in a message, which the page hides and the voice performs:

    ```voice
    voice: George
    stability: 0.4
    speed: 1.05
    ---
    [warmly] Here's where things stand. [pause] Three things shipped today...
    ```

The lines before `---` are settings (all optional); after it, the script,
with Eleven v4's inline tags ([whispers], [sighs], [long pause], ...). With no
`---`, the whole block is the script. A message without a block is read as
it's written, its markdown taken out.

What's spoken is kept: the same words in the same voice and settings are
never paid for twice. Every use is a system row in its Mood (who, what,
roughly what it cost), and the day's total is capped (voice_usd_per_day).
The key comes from settings.ELEVENLABS_API_KEY; unset, there is no voice.
"""

import hashlib
import json
import re
import time

import requests
from django.conf import settings as django_settings

API = 'https://api.elevenlabs.io/v1'
TTS_MODEL = 'eleven_v4'
STT_MODEL = 'scribe_v2'
TTS_USD_PER_KCHAR = 0.08   # Eleven v4, list price
STT_USD_PER_HOUR = 0.22    # Scribe v2
MAX_SCRIPT_CHARS = 10_000  # one generation's limit
SOURCE = 'voice'           # source_file of the system rows that keep the record
VOICES_FOR = 3600          # seconds the voice list is cached
# The voice when the list can't be read (a key without "Voices: read") and
# none is named by its id: George, ElevenLabs' quickstart voice, offered
# until the end of 2026 -- name another by id with the 'voice' knob.
FALLBACK_VOICE_ID = 'JBFqnCBsd6RMkjVDRZzb'
_VOICE_ID = re.compile(r'^[A-Za-z0-9]{20}$')

# Words a transcription should expect: instruments, the project's names.
# The people, agents and Moods are added to these when a memo is transcribed.
KEYTERMS = ['banjo', 'mandolin', 'dobro', 'fiddle', 'upright bass', 'bluegrass', 'flatpicking', 'Scruggs',
            'Cryptograss', 'PickiPedia', 'magenta', 'magent', 'Mood', 'Moods', 'hunter', 'maybelle',
            'delivery-kid', 'Ethereum', 'blockchain']

_VOICE_BLOCK = re.compile(r'```voice[ \t]*\n(.*?)\n?```[ \t]*\n?', re.S)
SETTING_RANGES = {'stability': (0.0, 1.0), 'similarity': (0.0, 1.0), 'style': (0.0, 1.0), 'speed': (0.7, 1.2)}


class VoiceError(Exception):
    """Something the person should be told: not set up, over budget, or ElevenLabs said no."""

    def __init__(self, message, status=502):
        super().__init__(message)
        self.status = status


def enabled():
    return bool(getattr(django_settings, 'ELEVENLABS_API_KEY', ''))


def _headers():
    if not enabled():
        raise VoiceError("voice isn't set up here (no ElevenLabs key)", status=503)
    return {'xi-api-key': django_settings.ELEVENLABS_API_KEY}


# --- a message's direction ------------------------------------------------------

def split_voice(text):
    """(the text without its voice block, the direction or None).

    The direction is {'script': str, 'voice': str, 'settings': {name: float}};
    settings out of range or unknown are dropped, never guessed at."""
    match = _VOICE_BLOCK.search(text or '')
    if not match:
        return text, None
    body = match.group(1)
    head, sep, script = body.partition('\n---\n') if '\n---\n' in body else ('', '', body)
    if not sep and body.startswith('---\n'):
        head, script = '', body[4:]
    direction = {'script': script.strip(), 'voice': '', 'settings': {}}
    for line in head.splitlines():
        key, _, value = line.partition(':')
        key, value = key.strip().lower(), value.strip()
        if key == 'voice':
            direction['voice'] = value[:100]
        elif key in SETTING_RANGES:
            try:
                number = float(value)
            except ValueError:
                continue
            low, high = SETTING_RANGES[key]
            if low <= number <= high:
                direction['settings'][key] = number
    rest = (text[:match.start()] + text[match.end():]).strip()
    return rest, direction


def plain(text):
    """A message as it would be read out: its markdown taken away, code and links named, not spelled."""
    text = re.sub(r'```.*?```', ' (code) ', text or '', flags=re.S)
    text = re.sub(r'!\[[^\]]*\]\([^)]*\)', ' ', text)               # images
    text = re.sub(r'\[([^\]]+)\]\([^)]*\)', r'\1', text)             # links: their words
    text = re.sub(r'https?://\S+', ' (a link) ', text)
    text = re.sub(r'`([^`]*)`', r'\1', text)
    text = re.sub(r'^\s{0,3}#{1,6}\s*', '', text, flags=re.M)        # headings
    text = re.sub(r'^\s*[-*+]\s+', '', text, flags=re.M)             # bullets
    text = re.sub(r'(\*\*|__|\*|_|~~)(?=\S)(.+?)(?<=\S)\1', r'\2', text)
    text = re.sub(r'\[\[([^\]|]+)(\|([^\]]+))?\]\]', lambda m: m.group(3) or m.group(1), text)
    return re.sub(r'[ \t]+', ' ', re.sub(r'\n{3,}', '\n\n', text)).strip()


# --- the day's budget -------------------------------------------------------------

def spent_today():
    from datetime import datetime, timezone
    from conversations.models import Message
    start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    rows = Message.objects.filter(source_file=SOURCE, created_at__gte=start).values_list('content', flat=True)
    return round(sum(float((c or {}).get('usd') or 0) for c in rows if isinstance(c, dict)), 4)


def check_budget(usd):
    from conversations.services import settings as knobs
    cap = float(knobs.global_value('voice_usd_per_day'))
    if spent_today() + usd > cap:
        raise VoiceError(f'voice has spent its ${cap:.2f} for today; it starts again at midnight UTC', status=429)


def record(motion, kind, by, **details):
    """A system row in the Mood: who used voice, on what, for about how much."""
    from conversations.models import ConversationParticipant, Message
    import uuid
    system, _ = ConversationParticipant.objects.get_or_create(name='system', defaults={'participant_type': 'system'})
    return Message.objects.create(id=uuid.uuid4(), sender=system, motion=motion, source_file=SOURCE,
                                  content={'type': kind, 'by': by, **details}, timestamp=int(time.time() * 1000))


# --- voices -----------------------------------------------------------------------

def voices(http=requests):
    """[{'name', 'voice_id', 'description', 'labels'}], ElevenLabs' list, cached an hour."""
    from django.core.cache import cache
    cached = cache.get('voice:voices')
    if cached is not None:
        return cached
    response = http.get(f'{API}/voices', headers=_headers(), timeout=30)
    if response.status_code != 200:
        raise VoiceError(f'ElevenLabs answered {response.status_code} for the voice list')
    found = [{'name': v.get('name', ''), 'voice_id': v.get('voice_id', ''),
              'description': v.get('description') or '', 'labels': v.get('labels') or {}}
             for v in response.json().get('voices', [])]
    cache.set('voice:voices', found, VOICES_FOR)
    return found


def voice_id_for(name, http=requests):
    """The voice called `name` (or with that id); else the house voice (the
    'voice' knob); else the first there is.

    A voice given by its id is used as it is, list or no list. A key that may
    not read the list (403) still speaks: named voices it can't resolve fall
    back to the house voice's id, or George."""
    from conversations.services import settings as knobs
    house = (knobs.global_value('voice') or '').strip()
    if _VOICE_ID.match((name or '').strip()):
        return name.strip()
    try:
        listed = voices(http)
    except VoiceError as e:
        if 'answered 401' in str(e) or 'answered 403' in str(e):
            return house if _VOICE_ID.match(house) else FALLBACK_VOICE_ID
        raise
    if not listed:
        return house if _VOICE_ID.match(house) else FALLBACK_VOICE_ID
    for wanted in (name, house):
        wanted = (wanted or '').strip().lower()
        for v in listed if wanted else ():
            if wanted in (v['name'].lower(), v['voice_id'].lower()) or v['name'].lower().startswith(wanted + ' '):
                return v['voice_id']
    return listed[0]['voice_id']


# --- reading a message aloud --------------------------------------------------------

def script_for(text):
    """(what to say, voice name, settings) for a message's text."""
    rest, direction = split_voice(text)
    if direction and direction['script']:
        return direction['script'][:MAX_SCRIPT_CHARS], direction['voice'], direction['settings']
    return plain(rest)[:MAX_SCRIPT_CHARS], '', {}


def speak(message, by, http=requests):
    """The URL of `message` read aloud: made once, kept. VoiceError if it can't be."""
    from conversations.models import Media, Message
    from conversations.services import media as media_store
    from conversations.services.motion_view import prose
    script, voice_name, voice_settings = script_for(prose(message.content))
    if not script.strip():
        raise VoiceError('nothing in that message to read aloud', status=400)
    voice_id = voice_id_for(voice_name, http)
    body = {'text': script, 'model_id': TTS_MODEL}
    if voice_settings:
        names = {'similarity': 'similarity_boost'}
        body['voice_settings'] = {names.get(k, k): v for k, v in voice_settings.items()}
    key = hashlib.sha256(json.dumps([voice_id, body], sort_keys=True).encode()).hexdigest()
    done = (Message.objects.filter(source_file=SOURCE, content__type='spoken', content__key=key)
            .values_list('content', flat=True).first())
    if done and Media.objects.filter(sha256=done.get('media')).exists():
        return Media.objects.get(sha256=done['media']).url
    usd = round(len(script) / 1000 * TTS_USD_PER_KCHAR, 4)
    check_budget(usd)
    response = http.post(f'{API}/text-to-speech/{voice_id}', params={'output_format': 'mp3_44100_128'},
                         headers=_headers(), json=body, timeout=110)
    if response.status_code != 200:
        raise VoiceError(f'ElevenLabs answered {response.status_code}: {_why(response)}')
    stored = media_store.store(response.content, audio=True)
    if stored is None:
        raise VoiceError('ElevenLabs sent back something that is not audio')
    record(message.motion, 'spoken', by, message=str(message.id), key=key, media=stored.sha256,
           voice=voice_id, chars=len(script), usd=usd)
    return stored.url


# --- a memo, transcribed --------------------------------------------------------------

def keyterms():
    from conversations.models import Motion, ThinkingEntity
    names = list(ThinkingEntity.objects.values_list('name', flat=True))
    moods = [t for t in Motion.objects.values_list('title', flat=True) if t and len(t) <= 50]
    seen, out = set(), []
    for term in KEYTERMS + names + moods:
        if term and term.lower() not in seen and len(term) <= 50:
            seen.add(term.lower())
            out.append(term)
    return out[:1000]


def transcribe(media, motion, by, http=requests):
    """{'text', 'seconds', 'language'} for a stored memo; VoiceError if it can't be."""
    check_budget(STT_USD_PER_HOUR * 10 / 60)  # room for ten minutes, at least
    ext = media.EXTENSIONS[media.mime]

    def ask(terms):
        form = {'model_id': STT_MODEL, 'tag_audio_events': 'true', **({'keyterms': terms} if terms else {})}
        return http.post(f'{API}/speech-to-text', headers=_headers(), timeout=110, data=form,
                         files={'file': (f'memo.{ext}', bytes(media.data), media.mime)})
    response = ask(keyterms())
    if response.status_code in (400, 422):  # the words it should expect, refused: ask plainly
        response = ask(None)
    if response.status_code != 200:
        raise VoiceError(f'ElevenLabs answered {response.status_code}: {_why(response)}')
    result = response.json()
    seconds = float(result.get('audio_duration_secs') or 0)
    usd = round(seconds / 3600 * STT_USD_PER_HOUR, 4)
    record(motion, 'transcribed', by, media=media.sha256, seconds=seconds, usd=usd)
    return {'text': (result.get('text') or '').strip(), 'seconds': seconds,
            'language': result.get('language_code') or ''}


def _why(response):
    try:
        detail = response.json().get('detail')
    except ValueError:
        return response.text[:200]
    if isinstance(detail, dict):
        return str(detail.get('message') or detail.get('status') or detail)[:200]
    return str(detail)[:200]
