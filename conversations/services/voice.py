"""Voice: memos people speak, and messages read aloud. ElevenLabs does both.

A **memo** is recorded in the browser and posted here (views_auth.api_memo)
the moment it's stopped: its audio is stored like an image (services/media.py)
and Scribe starts on it at once, in the background (hear_later). The box
holds the recording, ready to send; sent, it's posted as soon as its words
are in -- usually already -- with the transcript after the link
(views_auth.api_say, memo_in / with_transcript), so agents read it as text
and a spoken "Magent, ..." wakes as a typed one would.

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

**Each speaker has a voice**, so a Mood heard and not seen still makes
sense: the one they chose, or else one given at their first reading --
unlike the narrator's and everyone else's -- and kept. magent reads in the
house voice unless it has chosen another. **The narrator** has a voice of
its own (the 'narrator_voice' setting; given at its first word, nobody
else's, changeable in the voices list): when messages are read one after
another it says who speaks next ("Justin says:"), and when an agent starts
work, that it's thinking ("Magent is thinking, in magenta interface").

**It starts at once.** A message is read in pieces (pieces()): its first
paragraph alone -- cut at a sentence if it runs long -- so it's spoken in a
few seconds and plays while the rest is made, a piece at a time, each asked
for as the one before starts playing. Each piece is made knowing the ones
before it (ElevenLabs' request stitching, previous_request_ids), so the
pieces sound like one reading, not several.

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
FIRST_PIECE_MIN = 150      # the first piece read aloud: long enough to cover the making of the next,
FIRST_PIECE_CHARS = 400    # short enough to start at once
SECOND_PIECE_CHARS = 800   # the second: made while the first plays
PIECE_CHARS = 1500         # every piece after them, at most
STITCH_WITHIN = 7000       # seconds a piece's request id may condition the next (ElevenLabs: two hours)
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
    """(the text without its voice blocks, the first block's direction or None)."""
    rest, directions = split_voices(text)
    return rest, (directions[0] if directions else None)


def split_voices(text):
    """(the text without its voice blocks, a direction for each, in order).

    A message may carry several -- auditions, a dialogue -- each with its own
    ▶. A direction is {'script': str, 'voice': str, 'settings': {name: float}};
    settings out of range or unknown are dropped, never guessed at."""
    directions = [_direction(m.group(1)) for m in _VOICE_BLOCK.finditer(text or '')]
    if not directions:
        return text, []
    return re.sub(r'\n{3,}', '\n\n', _VOICE_BLOCK.sub('\n\n', text)).strip(), directions


def _direction(body):
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
    return direction


# What a person reading aloud would say for the page's own symbols.
SPOKEN_SYMBOLS = {'▶': 'play', '■': 'stop', '✎': 'the pencil', '⚙': 'the gear', '🎙': 'the microphone',
                  '📌': 'the pin', '⌕': 'search', '＋': 'plus', '✓': '', '✗': '', '✦': '', '⟲': '', '→': 'to',
                  '←': 'from', '≥': 'at least', '≤': 'at most', '≈': 'about', '×': 'times', '…': '...'}
_MOOD_LINK = re.compile(r'(?:https?://\S+?)?/moods/([\w-]+)/#m-[0-9a-f-]{36}', re.I)
_UUID = re.compile(r'(?:#m-)?\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b', re.I)
_HASH = re.compile(r'\b(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{7,64}\b', re.I)  # commits, digests
_LONG_TOKEN = re.compile(r'(?<!\S)[^\s]{33,}(?!\S)')  # keys, base64, anything nobody would read out
_PATH = re.compile(r'(?<![\w/.:])(?:~|\.{1,2})?(?:/?[\w.-]+/){1,}([\w.-]+)')  # a path: its last part


def _spoken_link(match):
    from urllib.parse import urlparse
    host = urlparse(match.group(0)).netloc.lower().removeprefix('www.')
    return f' a link to {host} ' if host else ' a link '


def plain(text):
    """A message as it would be read out: markdown taken away; code, links, ids,
    hashes and paths named rather than spelled; the page's symbols said in words."""
    text = re.sub(r'```.*?```', ' (some code) ', text or '', flags=re.S)
    text = re.sub(r'!\[[^\]]*\]\([^)]*\)', ' ', text)               # images
    text = re.sub(r'\[([^\]]+)\]\([^)]*\)', r'\1', text)             # links: their words
    text = _MOOD_LINK.sub(r' a message in \1 ', text)
    text = re.sub(r'https?://[^\s)>\]]+', _spoken_link, text)
    text = _UUID.sub(lambda m: ' a message link ' if m.group(0).startswith('#m-') else ' an ID ', text)
    text = re.sub(r'`([^`]*)`', r'\1', text)
    text = _HASH.sub(' a hash ', text)
    text = _PATH.sub(lambda m: m.group(1), text)
    text = _LONG_TOKEN.sub(' a long string ', text)
    text = re.sub(r'(?<![\w&])#(\d+)\b', r'number \1', text)             # #69: "number 69", not "hashtag"
    for symbol, words in SPOKEN_SYMBOLS.items():
        text = text.replace(symbol, f' {words} ' if words else ' ')
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


def record(mood, kind, by, **details):
    """A system row in the Mood: who used voice, on what, for about how much."""
    from conversations.models import ConversationParticipant, Message
    import uuid
    system, _ = ConversationParticipant.objects.get_or_create(name='system', defaults={'participant_type': 'system'})
    return Message.objects.create(id=uuid.uuid4(), sender=system, mood=mood, source_file=SOURCE,
                                  content={'type': kind, 'by': by, **details}, timestamp=int(time.time() * 1000))


# --- voices -----------------------------------------------------------------------

def voices(http=requests):
    """[{'name', 'voice_id', 'description', 'labels'}], ElevenLabs' list, cached an hour."""
    from django.core.cache import cache
    cached = cache.get('voice:voices')
    if cached is not None:
        return cached
    response = http.get(f'{API}/voices', headers=_headers(), timeout=30)
    refused = None
    if response.status_code in (401, 403):
        # Kept: ElevenLabs' own words for why (a permission, an IP allowlist...).
        refused = f'ElevenLabs answered {response.status_code} for the voice list: {_why(response)}'
        cache.set('voice:refused', refused, VOICES_FOR)
        # Its standard voices are listed to anyone, key or none: names still work.
        response = http.get(f'{API}/voices', timeout=30)
    if response.status_code != 200:
        raise VoiceError(refused or f'ElevenLabs answered {response.status_code} for the voice list: {_why(response)}')
    found = [{'name': v.get('name', ''), 'voice_id': v.get('voice_id', ''),
              'description': v.get('description') or '', 'labels': v.get('labels') or {},
              'preview_url': v.get('preview_url') or ''}  # a sample, free to play: choosing by ear
             for v in response.json().get('voices', [])]
    cache.set('voice:voices', found, VOICES_FOR)
    if not refused:
        cache.delete('voice:refused')
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


# --- who speaks in which voice ------------------------------------------------------

NARRATOR = 'narrator'  # says who speaks next, and who's thinking: in a voice of its own ('narrator_voice')
HOUSE_SPEAKER = 'magent'  # reads in the house voice, unless it has chosen another


def chosen_voices():
    """{name: voice (an id, or a name)}: each speaker's voice, chosen or given (the
    'speaker_voice' setting), and the narrator's ('narrator_voice')."""
    from conversations.models import Setting
    from conversations.services import settings as knobs
    rows = Setting.objects.filter(key='speaker_voice', mood=None, agent__isnull=False)
    chosen = {agent: row.value for (_, agent, _), row in knobs.latest(rows).items() if row.value}
    if knobs.global_value('narrator_voice'):
        chosen[NARRATOR] = knobs.global_value('narrator_voice')
    return chosen


def voice_of(name, http=requests):
    """The voice id `name`'s messages are read in.

    Theirs, if chosen (or given before). The narrator's is the house voice.
    Anyone else is given one the first time they're read -- one nobody else
    has, if there's one left -- and it's kept as their setting, so they
    sound the same tomorrow and can change it."""
    from conversations.models import ThinkingEntity
    from conversations.services import settings as knobs
    chosen = chosen_voices()
    if chosen.get(name):
        return voice_id_for(chosen[name], http)
    house = voice_id_for('', http)
    if name == HOUSE_SPEAKER:
        return house
    try:
        listed = [v['voice_id'] for v in voices(http) if v.get('voice_id')]
    except VoiceError:
        return house  # no list to choose from: the house voice, and nothing kept
    taken = {house} | {voice_id_for(v, http) for v in chosen.values()}
    free = sorted(set(listed) - taken) or sorted(set(listed) - {house})
    if not free:
        return house
    pick = free[int(hashlib.sha256(name.encode()).hexdigest(), 16) % len(free)]
    if name == NARRATOR:
        knobs.change('narrator_voice', pick, note='given at its first word')
        return pick
    entity = ThinkingEntity.objects.filter(name=name).first()
    if entity is not None:
        knobs.change('speaker_voice', pick, agent=entity, note='given at their first reading aloud')
    return pick


def spoken_name(name):
    return (name or 'someone')[:1].upper() + (name or 'someone')[1:]


def intro(message, by, where=False, http=requests):
    """The URL of the narrator saying who speaks next -- and, with `where`, in
    which Mood ("In general, Justin says:"). Made once per wording, and kept."""
    who = spoken_name(message.sender_id)
    if where:
        title = (message.mood.title or message.mood.slug).replace('-', ' ')
        script = f'In {title}, {who} says:'
    else:
        script = f'{who} says:'
    return _spoken(message.mood, by, voice_of(NARRATOR, http), {'text': script, 'model_id': TTS_MODEL}, script,
                   http, intro=message.sender_id)


def narrate_thinking(mood, agent, by, where=False, http=requests):
    """The URL of the narrator saying `agent` has started work ("Magent is
    thinking" -- with `where`, ", in magenta interface"). Made once per wording, and kept."""
    script = f'{spoken_name(agent)} is thinking'
    if where:
        script += f", in {(mood.title or mood.slug).replace('-', ' ')}"
    script += '.'
    return _spoken(mood, by, voice_of(NARRATOR, http), {'text': script, 'model_id': TTS_MODEL}, script,
                   http, narrated='thinking', agent=agent)


# --- reading a message aloud --------------------------------------------------------

def script_for(text, part=0):
    """(what to say, voice name, settings) for a message's text: its `part`th
    voice block, or, with none, the message as written."""
    rest, directions = split_voices(text)
    if directions:
        direction = directions[min(max(part, 0), len(directions) - 1)]
        if direction['script']:
            return direction['script'][:MAX_SCRIPT_CHARS], direction['voice'], direction['settings']
    return plain(rest)[:MAX_SCRIPT_CHARS], '', {}


def _cut(text, limit):
    """(the start of `text`, up to `limit` characters, ending where a sentence does; the rest)."""
    if len(text) <= limit:
        return text, ''
    window = text[:limit]
    ends = [m.end() for m in re.finditer(r'[.!?…][\'")\]]*\s', window)]
    if ends and ends[-1] > limit // 3:
        at = ends[-1]
    else:  # no sentence ends early enough: at a word
        at = window.rfind(' ') if window.rfind(' ') > limit // 3 else limit
    return text[:at].strip(), text[at:].strip()


def pieces(script):
    """`script` in the pieces it's read in, paragraphs kept together where they fit:
    a short first one, to start at once (FIRST_PIECE_MIN to FIRST_PIECE_CHARS);
    a second made while it plays (SECOND_PIECE_CHARS); then up to PIECE_CHARS.
    A paragraph too long for its piece is cut where a sentence ends."""
    paragraphs = [p.strip() for p in re.split(r'\n\s*\n', script or '') if p.strip()]
    out, current = [], ''

    def limit():
        return (FIRST_PIECE_CHARS, SECOND_PIECE_CHARS)[len(out)] if len(out) < 2 else PIECE_CHARS
    while paragraphs:
        paragraph = paragraphs.pop(0)
        joined = f'{current}\n\n{paragraph}' if current else paragraph
        if len(joined) <= limit():
            current = joined
            if not out and len(current) >= FIRST_PIECE_MIN:
                out.append(current)  # the first is out as soon as it's enough
                current = ''
            continue
        if current and (out or len(current) >= FIRST_PIECE_MIN):
            out.append(current)  # what's gathered is a piece; this paragraph starts the next
            current = ''
            paragraphs.insert(0, paragraph)
            continue
        # Too long for what's left of this piece: as much of it as fits, at a sentence.
        room = limit() - (len(current) + 2 if current else 0)
        head, rest = _cut(paragraph, room)
        out.append(f'{current}\n\n{head}' if current else head)
        current = ''
        if rest:
            paragraphs.insert(0, rest)
    if current:
        out.append(current)
    return out


def speak(message, by, http=requests, part=0, piece=0):
    """The URL of `message` (its `part`th voice block) read aloud -- its `piece`th
    piece (speak_piece). VoiceError if it can't be."""
    return speak_piece(message, by, http=http, part=part, piece=piece)['url']


def speak_piece(message, by, http=requests, part=0, piece=0):
    """{'url', 'pieces'}: the `piece`th piece of `message` read aloud, and how many
    there are. Made once and kept; made knowing the pieces before it, if they
    were made lately. VoiceError if it can't be."""
    from conversations.services.mood_view import prose
    script, voice_name, voice_settings = script_for(prose(message.content), part)
    parts = pieces(script)
    if not parts:
        raise VoiceError('nothing in that message to read aloud', status=400)
    piece = min(max(piece, 0), len(parts) - 1)
    # A voice its writer named; else the writer's own.
    voice_id = voice_id_for(voice_name, http) if voice_name else voice_of(message.sender_id, http)

    def body_for(text):
        body = {'text': text, 'model_id': TTS_MODEL}
        if voice_settings:
            names = {'similarity': 'similarity_boost'}
            body['voice_settings'] = {names.get(k, k): v for k, v in voice_settings.items()}
        return body
    before = [_request_id(_key(voice_id, body_for(text))) for text in parts[max(0, piece - 3):piece]]
    stitch = {'previous_request_ids': [r for r in before if r]} if any(before) else {}
    url = _spoken(message.mood, by, voice_id, body_for(parts[piece]), parts[piece], http, extra=stitch,
                  message=str(message.id), **({'piece': piece} if piece else {}))
    return {'url': url, 'pieces': len(parts)}


def _key(voice_id, body):
    return hashlib.sha256(json.dumps([voice_id, body], sort_keys=True).encode()).hexdigest()


def _request_id(key):
    """ElevenLabs' id for the making of what `key` names, if it was made lately enough to stitch onto."""
    from datetime import timedelta
    from django.utils import timezone
    from conversations.models import Message
    row = (Message.objects.filter(source_file=SOURCE, content__type='spoken', content__key=key,
                                  created_at__gte=timezone.now() - timedelta(seconds=STITCH_WITHIN))
           .order_by('-created_at').values_list('content', flat=True).first())
    return (row or {}).get('request_id') or None


def _spoken(mood, by, voice_id, body, script, http, extra=None, **details):
    """The URL of `body` spoken in `voice_id`: the kept one if it's been made, else
    made now (sent with `extra` too: what it's stitched onto, not part of what it is)."""
    from conversations.models import Media, Message
    key = _key(voice_id, body)

    def made():
        done = (Message.objects.filter(source_file=SOURCE, content__type='spoken', content__key=key)
                .values_list('content', flat=True).first())
        if done and Media.objects.filter(sha256=done.get('media')).exists():
            return Media.objects.get(sha256=done['media']).url
        return None
    if made():
        return made()
    # One making at a time per message and voice: a second press (or another
    # person's) while it's being made waits for that one, never pays twice.
    from django.core.cache import cache
    if not cache.add(f'voice:making:{key}', 1, 120):
        for _ in range(110):
            time.sleep(1)
            if made():
                return made()
            if cache.get(f'voice:making:{key}') is None:
                break
        raise VoiceError('it is still being read aloud for someone else; try again in a moment', status=409)
    try:
        return made() or _make(mood, by, key, voice_id, {**body, **(extra or {})}, script, http, details)
    finally:
        cache.delete(f'voice:making:{key}')


def _make(mood, by, key, voice_id, body, script, http, details):
    from conversations.services import media as media_store
    usd = round(len(script) / 1000 * TTS_USD_PER_KCHAR, 4)
    check_budget(usd)
    response = http.post(f'{API}/text-to-speech/{voice_id}', params={'output_format': 'mp3_44100_128'},
                         headers=_headers(), json=body, timeout=110)
    if response.status_code != 200:
        raise VoiceError(f'ElevenLabs answered {response.status_code}: {_why(response)}')
    stored = media_store.store(response.content, audio=True)
    if stored is None:
        raise VoiceError('ElevenLabs sent back something that is not audio')
    request_id = (getattr(response, 'headers', None) or {}).get('request-id')
    record(mood, 'spoken', by, **details, key=key, media=stored.sha256, voice=voice_id, chars=len(script), usd=usd,
           **({'request_id': request_id} if request_id else {}))
    return stored.url


# --- a memo, transcribed --------------------------------------------------------------

def keyterms():
    from conversations.models import Mood, ThinkingEntity
    names = list(ThinkingEntity.objects.values_list('name', flat=True))
    moods = [t for t in Mood.objects.values_list('title', flat=True) if t and len(t) <= 50]
    seen, out = set(), []
    for term in KEYTERMS + names + moods:
        if term and term.lower() not in seen and len(term) <= 50:
            seen.add(term.lower())
            out.append(term)
    return out[:1000]


def transcribe(media, mood, by, http=requests):
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
    record(mood, 'transcribed', by, media=media.sha256, seconds=seconds, usd=usd)
    return {'text': (result.get('text') or '').strip(), 'seconds': seconds,
            'language': result.get('language_code') or ''}


# --- a memo, heard while it's being sent -----------------------------------------------

HEARD_FOR = 86400       # seconds a memo's words are kept, waiting for it to be sent
HEARING_FOR = 300       # seconds one transcription may take before another may try
WAIT_TO_POST = 240      # seconds a sent memo waits for its words before going without
_MEMO = re.compile(r'🎙 \[voice memo[^\]]*\]\(/(?:moods|motions)/media/([0-9a-f]{64})\.[a-z0-9]+\)')


def _in_background(fn, *args):
    """Run fn(*args) in a thread of its own, with its own database connection. (Tests run it inline.)"""
    import threading

    def run():
        from django.db import connection
        try:
            fn(*args)
        finally:
            connection.close()
    threading.Thread(target=run, daemon=True).start()


def hear_later(media, mood, by, mentionable, aliases=None):
    """Start transcribing a memo now, in the background, so its words are in
    by the time it's sent. Once per recording. `mentionable`: the names a
    spoken name may become an @mention of (not agents', for a wiki sign-in)."""
    from django.core.cache import cache
    if cache.get(f'memo:heard:{media.sha256}') is not None or not cache.add(f'memo:hearing:{media.sha256}', 1, HEARING_FOR):
        return
    _in_background(_hear, media.sha256, mood.pk, by, sorted(mentionable), aliases or {})


def _hear(sha, mood_pk, by, mentionable, aliases):
    from django.core.cache import cache
    from conversations.models import Media, Mood
    try:
        heard = transcribe(Media.objects.get(sha256=sha), Mood.objects.get(pk=mood_pk), by)
        heard['text'] = spoken_mentions(heard['text'], set(mentionable), aliases)
    except VoiceError as e:
        heard = {'error': str(e)}
    except Exception:  # noqa: BLE001 -- whatever it was, the memo still goes, saying so
        heard = {'error': 'transcribing failed'}
    cache.set(f'memo:heard:{sha}', heard, HEARD_FOR)
    cache.delete(f'memo:hearing:{sha}')


def memo_in(text):
    """The sha of a memo in `text` still waiting for its words (one recorded
    here since hear_later), or None."""
    from django.core.cache import cache
    match = _MEMO.search(text or '')
    if not match:
        return None
    sha = match.group(1)
    heard = cache.get(f'memo:heard:{sha}')
    if heard is None and cache.get(f'memo:hearing:{sha}') is None:
        return None  # not one being heard: posted as written
    if heard and heard.get('text') and heard['text'] in text:
        return None  # its words are there already
    return sha


def heard(sha):
    """A memo's words, if they're in: {'text', ...} or {'error'}; None while still being heard."""
    from django.core.cache import cache
    return cache.get(f'memo:heard:{sha}')


def with_transcript(text, heard_as):
    """`text` with the memo's words after its link (or why there are none)."""
    match = _MEMO.search(text)
    words = (heard_as or {}).get('text') or f"(not transcribed: {(heard_as or {}).get('error') or 'no words heard'})"
    rest = text[match.end():].lstrip('\n')
    return text[:match.end()] + '\n\n' + words + ('\n\n' + rest if rest else '')


def post_when_heard(sha, text, post):
    """In the background: wait for the memo's words (hearing it here if nobody
    is), then post(text with them)."""
    _in_background(_post_when_heard, sha, text, post)


def _post_when_heard(sha, text, post):
    from django.core.cache import cache
    for _ in range(WAIT_TO_POST):
        if heard(sha) is not None:
            break
        if cache.get(f'memo:hearing:{sha}') is None:
            break  # nobody's hearing it (a restart, an eviction): it goes without, saying so
        time.sleep(1)
    post(with_transcript(text, heard(sha) or {'error': 'it took too long'}))


# --- a mention, spoken ------------------------------------------------------------

GREETINGS = r'(?:hey|hi|hello|ok|okay|yo)'
# How a transcription may write a name it heard: only ever read as that name
# where a name is plainly being said to someone.
HEARD_AS = {'magnet': 'magent'}


def spoken_mentions(text, mentionable, aliases=None):
    """A memo's words with its spoken addresses made @mentions, for the person
    to look over before sending: a name it opens with ("Magent, can you...",
    "Hey Justin ..."), and "at <name>" anywhere ("at skyler what do you think").

    `aliases`: {lowercased other name (a PickiPedia name): name here}."""
    names = {n.lower(): n.lower() for n in mentionable}
    names.update({k.lower(): v.lower() for k, v in (aliases or {}).items() if v.lower() in names})
    names.update({k: v for k, v in HEARD_AS.items() if v in names and k not in names})
    if not names or not text:
        return text
    alternatives = '|'.join(re.escape(n) for n in sorted(names, key=len, reverse=True))
    name = rf'(?P<name>{alternatives})(?![\w@-])'

    def at(match):
        return '@' + names[match.group('name').lower()]
    # Opening: after a greeting, or followed by a comma, colon or the like.
    opening = re.compile(rf'^(?P<lead>\s*(?:{GREETINGS}[,!]?\s+)?){name}', re.I)
    found = opening.match(text)
    if found and (found.group('lead').strip() or re.match(r'\s*[,:!—–-]', text[found.end():])):
        text = text[:found.start('name')] + at(found) + text[found.end('name'):]
    return re.sub(rf'(?<![\w@])at\s+{name}', at, text, flags=re.I)


def _why(response):
    try:
        detail = response.json().get('detail')
    except ValueError:
        return response.text[:200]
    if isinstance(detail, dict):
        return str(detail.get('message') or detail.get('status') or detail)[:200]
    return str(detail)[:200]
