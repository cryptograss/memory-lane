"""Writing into Motions: device enrollment by SSH key, and the say endpoint.

See services/motion_auth.py for the design. Everything here that changes
anything is a POST: enrollment is authenticated by an SSH signature, the
rest by a device cookie plus Django's CSRF check.
"""

import json
import re
import time
import uuid
from datetime import timedelta

from django.conf import settings
from django.http import HttpResponseRedirect, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt, ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from .models import Message, Motion, ThinkingEntity
from .services import motion_auth
from .services.redaction import redact

MAX_CHARS = 20_000
PER_MINUTE = 20
WEB_SOURCE = 'motion-web'
ENROLL_MAX_BYTES = 64 * 1024
# For everyone together: behind the proxy every client has the same address.
ENROLL_PER_MINUTE = 30


def _under_limit(bucket, per_minute):
    """Count one more attempt in this minute; False once there are too many."""
    from django.core.cache import cache
    key = f'limit:{bucket}:{int(time.time() // 60)}'
    cache.add(key, 0, 120)
    return cache.incr(key) <= per_minute


@require_GET
def api_challenge(request):
    return JsonResponse({'challenge': motion_auth.new_challenge(), 'namespace': motion_auth.NAMESPACE})


@csrf_exempt  # authenticated by the SSH signature, not a browser session
@require_POST
def api_enroll(request):
    """Trade a signed challenge for a one-time login link."""
    # Each attempt runs ssh-keygen twice; keep strangers from making that a load.
    if len(request.body) > ENROLL_MAX_BYTES:
        return JsonResponse({'error': 'too large'}, status=413)
    if not _under_limit('enroll', ENROLL_PER_MINUTE):
        return JsonResponse({'error': 'too many sign-in attempts; wait a minute'}, status=429)
    try:
        body = json.loads(request.body)
        challenge, signature = body['challenge'], body['signature']
        name = (body.get('name') or '').lower()
    except (ValueError, KeyError, AttributeError):
        return JsonResponse({'error': 'expected challenge and signature'}, status=400)

    if not motion_auth.challenge_is_fresh(challenge):
        return JsonResponse({'error': 'challenge expired; fetch a new one'}, status=400)
    message = motion_auth.signed_message(challenge, motion_auth.origin_of(request))
    if name:
        name = name if motion_auth.signature_is_valid(name, message, signature) else ''
    else:
        name = motion_auth.signer_of(message, signature) or ''  # the key says who you are
    entity = ThinkingEntity.objects.filter(name=name).first() if name else None
    if entity is None:
        return JsonResponse({'error': 'signature not accepted'}, status=403)
    from .views_admin import locked_response
    from .services import settings as knobs
    if locked_response():
        return locked_response()
    if knobs.banned(name):
        return JsonResponse({'error': 'signing in is barred for this name; ask an admin'}, status=403)

    code = motion_auth.issue_login_code(entity)
    url = request.build_absolute_uri(f'/motions/login/{code}/')
    return JsonResponse({'url': url, 'name': name, 'expires_in': int(motion_auth.CODE_LIFETIME.total_seconds())})


@ensure_csrf_cookie
@require_http_methods(['GET', 'POST'])
def login_page(request, code):
    """GET asks; POST enrolls this browser. A link preview only ever GETs.

    A link that can't be used says why: spent (when, for which device, and
    whether this very browser is the one it signed in), expired, or unknown.
    """
    if request.method == 'GET':
        state, detail = motion_auth.code_state(code)
        here = motion_auth.device_for(request)
        context = {'state': state, 'name': detail.entity_id if state == 'live' else None,
                   'used': detail if state == 'used' else None,
                   'signed_in_here': here.entity_id if here else None}
        return render(request, 'conversations/motion_login.html', context, status=200 if state == 'live' else 410)

    from .views_admin import locked_response
    if locked_response():
        return locked_response()
    label = getattr(settings, 'DEVICE_LABEL_PREFIX', '') + request.POST.get('label', '')
    device, token = motion_auth.redeem_login_code(code, label=label)
    if device is None:
        state, detail = motion_auth.code_state(code)
        return render(request, 'conversations/motion_login.html',
                      {'state': state, 'used': detail if state == 'used' else None}, status=410)
    response = HttpResponseRedirect('/motions/')
    response.set_cookie(motion_auth.COOKIE, token, max_age=motion_auth.COOKIE_AGE,
                        httponly=True, secure=not settings.DEBUG, samesite='Lax')
    return response


MEDIA_PER_MINUTE = 30


@require_POST
def api_media(request, slug):
    """Store an image this device's person is putting into a Motion.

    The body is the image itself; what it is comes from its bytes, not the
    Content-Type. Answers with the markdown to put in a message.
    """
    from .services import media
    from .views_admin import locked_response

    if locked_response():
        return locked_response()
    device = motion_auth.device_for(request)
    if device is None:
        return JsonResponse({'error': 'sign in to write'}, status=401)
    get_object_or_404(Motion, slug=slug)
    if len(request.body) > media.MAX_BYTES:
        return JsonResponse({'error': f'larger than {media.MAX_BYTES // (1024 * 1024)} MB'}, status=413)
    if not _under_limit(f'media:{device.pk}', MEDIA_PER_MINUTE):
        return JsonResponse({'error': 'slow down'}, status=429)
    stored = media.store(request.body, added_by=device.entity)
    if stored is None:
        return JsonResponse({'error': 'not a PNG, JPEG, GIF or WebP image'}, status=400)
    return JsonResponse({'url': stored.url, 'markdown': media.markdown(stored)}, status=201)


@require_GET
def api_me(request):
    device = motion_auth.device_for(request)
    return JsonResponse({'name': device.entity_id if device else None})


@require_POST
def api_logout(request):
    device = motion_auth.device_for(request)
    if device:
        type(device).objects.filter(pk=device.pk).update(revoked_at=timezone.now())
    response = JsonResponse({'ok': True})
    response.delete_cookie(motion_auth.COOKIE)
    return response


@require_POST
def api_say(request, slug):
    """Post a turn into a Motion as the device's person."""
    from .views_admin import locked_response
    if locked_response():
        return locked_response()
    device = motion_auth.device_for(request)
    if device is None:
        return JsonResponse({'error': 'sign in to write'}, status=401)
    motion = get_object_or_404(Motion, slug=slug)

    try:
        text = json.loads(request.body).get('text', '')
    except (ValueError, AttributeError):
        return JsonResponse({'error': 'expected {"text": ...}'}, status=400)
    if not isinstance(text, str):
        return JsonResponse({'error': 'expected {"text": ...}'}, status=400)
    text = text.replace('\x00', '').strip()  # Postgres text can't hold NUL
    if not text:
        return JsonResponse({'error': 'nothing to say'}, status=400)
    if len(text) > MAX_CHARS:
        return JsonResponse({'error': f'longer than {MAX_CHARS} characters'}, status=400)

    if device.tier == 'wiki':
        # A PickiPedia sign-in can't address agents: refused, not quietly passed
        # on as text, so nobody believes an agent was asked.
        from .services.motion_view import known_names, mentions_in
        agents = set(ThinkingEntity.objects.filter(is_biological_human=False).values_list('name', flat=True))
        addressed = [n for n in mentions_in(text, known_names()) if n in agents]
        if addressed:
            return JsonResponse({'error': f"signed in with PickiPedia, you can't address agents "
                                          f"(@{', @'.join(addressed)}): take the mention out, or sign in with "
                                          f"your SSH key (magenta.sh login)", 'agents': addressed}, status=403)

    recent = Message.objects.filter(sender=device.entity, source_file=WEB_SOURCE,
                                    created_at__gt=timezone.now() - timedelta(minutes=1)).count()
    if recent >= PER_MINUTE:
        return JsonResponse({'error': 'slow down'}, status=429)

    text, _ = redact(text)
    message = Message.objects.create(
        id=uuid.uuid4(), sender=device.entity, content=text, motion=motion,
        timestamp=int(time.time() * 1000), source_file=WEB_SOURCE, client_version=web_client(request, device),
    )
    return JsonResponse({'id': str(message.id)}, status=201)


_MOBILE_AGENT = re.compile(r'Mobi|Android|iPhone|iPad|iPod', re.I)


def web_client(request, device=None):
    """What a web post was written on, kept as its client_version: 'magenta-web',
    then '/mobile' from a phone or tablet, and '/wiki' from a PickiPedia sign-in
    (its @agent wakes nobody). The thread marks both."""
    parts = ['magenta-web']
    if _MOBILE_AGENT.search(request.META.get('HTTP_USER_AGENT', '')):
        parts.append('mobile')
    if device is not None and device.tier == 'wiki':
        parts.append('wiki')
    return '/'.join(parts)


@require_POST
def api_rename(request, slug):
    """Rename a Motion (its title, and optionally its description), as the device's person.

    Body: {"title": "...", "description": "..."}. The slug -- the Motion's
    key, in every link -- never changes. The record keeps what it was called
    before: a system row in the Motion says who renamed it, from what, to what.
    """
    from .models import ConversationParticipant
    from .views_admin import locked_response
    if locked_response():
        return locked_response()
    device, refused = motion_auth.key_device(request, 'rename a Mood')
    if refused:
        return refused
    motion = get_object_or_404(Motion, slug=slug)
    try:
        body = json.loads(request.body)
        title = str(body.get('title', '')).replace('\x00', '').strip()
        description = body.get('description')
        if description is not None:
            description = str(description).replace('\x00', '').strip()
    except (ValueError, AttributeError):
        return JsonResponse({'error': 'expected {"title": ...}'}, status=400)
    if not title:
        return JsonResponse({'error': 'a title, please'}, status=400)
    if len(title) > 200 or (description is not None and len(description) > 2000):
        return JsonResponse({'error': 'at most 200 characters for a title, 2000 for a description'}, status=400)

    was = {'title': motion.title, 'description': motion.description}
    motion.title = title
    fields = ['title']
    if description is not None:
        motion.description = description
        fields.append('description')
    motion.save(update_fields=fields)
    system, _ = ConversationParticipant.objects.get_or_create(name='system', defaults={'participant_type': 'system'})
    Message.objects.create(
        id=uuid.uuid4(), sender=system, motion=motion, timestamp=int(time.time() * 1000), source_file='motion-rename',
        content={'type': 'renamed', 'by': device.entity_id, 'from': was,
                 'to': {'title': motion.title, 'description': motion.description}},
    )
    return JsonResponse({'title': motion.title, 'description': motion.description})


# --- your devices: a list, revoking one, renewing one by name -------------------

def _device_payload(device, this):
    state = motion_auth.device_state(device)
    used = device.last_used_at or device.created_at
    return {'id': str(device.id), 'label': device.label or '(unnamed)', 'signed_in_at': device.created_at.isoformat(),
            'last_used_at': used.isoformat(), 'state': state,
            'times_out_at': (used + motion_auth.DEVICE_IDLE_LIMIT).isoformat() if state == 'live' else None,
            'this': this}


@require_GET
def api_devices(request):
    """The signed-in person's devices: when each signed in, last wrote, and times out."""
    from .models import Device
    device = motion_auth.device_for(request)
    if device is None:
        return JsonResponse({'error': 'sign in to see your devices'}, status=401)
    mine = Device.objects.filter(entity=device.entity).order_by('-created_at')
    return JsonResponse({'name': device.entity_id, 'idle_days': motion_auth.DEVICE_IDLE_LIMIT.days,
                         'devices': [_device_payload(d, d.pk == device.pk) for d in mine]})


@require_POST
def api_device_revoke(request, device_id):
    """Sign one of your own devices out, for good (a new login makes a new one)."""
    from .models import Device
    device = motion_auth.device_for(request)
    if device is None:
        return JsonResponse({'error': 'sign in first'}, status=401)
    done = Device.objects.filter(pk=device_id, entity=device.entity, revoked_at__isnull=True).update(
        revoked_at=timezone.now())
    if not done:
        return JsonResponse({'error': 'no such device of yours'}, status=404)
    return JsonResponse({'revoked': str(device_id)})


@csrf_exempt  # authenticated by the SSH signature, like enrolling
@require_POST
def api_renew(request):
    """Bring a device back by its name, signed with your SSH key: `magenta.sh renew <name>`.

    Body: {"challenge", "signature", "label"}; the signature is over the
    challenge with purpose 'renew <label>', so it renews that one and no other.
    """
    import re
    if len(request.body) > ENROLL_MAX_BYTES:
        return JsonResponse({'error': 'too large'}, status=413)
    if not _under_limit('enroll', ENROLL_PER_MINUTE):
        return JsonResponse({'error': 'too many attempts; wait a minute'}, status=429)
    try:
        body = json.loads(request.body)
        challenge, signature, label = body['challenge'], body['signature'], str(body['label']).strip()
    except (ValueError, KeyError, AttributeError, TypeError):
        return JsonResponse({'error': 'expected challenge, signature and label'}, status=400)
    if not re.fullmatch(r'[^\n\r]{1,100}', label):
        return JsonResponse({'error': 'a device name, on one line'}, status=400)
    if not motion_auth.challenge_is_fresh(challenge):
        return JsonResponse({'error': 'challenge expired; fetch a new one'}, status=400)
    message = motion_auth.signed_message(challenge, motion_auth.origin_of(request), purpose=f'renew {label}')
    name = motion_auth.signer_of(message, signature) or ''
    entity = ThinkingEntity.objects.filter(name=name).first() if name else None
    if entity is None:
        return JsonResponse({'error': 'signature not accepted'}, status=403)
    from .views_admin import locked_response
    from .services import settings as knobs
    if locked_response():
        return locked_response()
    if knobs.banned(name):
        return JsonResponse({'error': 'barred for this name; ask an admin'}, status=403)
    device = motion_auth.renew_device(entity, label)
    if device is None:
        return JsonResponse({'error': f'no device of yours named "{label}"; sign in with magenta.sh login'}, status=404)
    return JsonResponse({'name': name, **_device_payload(device, False)})


# --- attesting: a statement signed with your SSH key, in #general --------------

GENERAL = 'general'
ATTEST_MAX = 2000


@csrf_exempt  # authenticated by the SSH signature
@require_POST
def api_attest(request):
    """Post a statement signed with your SSH key into #general: `magenta.sh attest "<words>"`.

    Body: {"challenge", "signature", "text"}; the signature is over
    attest_message(challenge, origin, text). The record keeps exactly what
    was signed, the signature and the key, so anyone can check it later with
    ssh-keygen -Y verify, without memory-lane.
    """
    if len(request.body) > ENROLL_MAX_BYTES:
        return JsonResponse({'error': 'too large'}, status=413)
    if not _under_limit('enroll', ENROLL_PER_MINUTE):
        return JsonResponse({'error': 'too many attempts; wait a minute'}, status=429)
    try:
        body = json.loads(request.body)
        challenge, signature, text = body['challenge'], body['signature'], str(body['text'])
    except (ValueError, KeyError, AttributeError, TypeError):
        return JsonResponse({'error': 'expected challenge, signature and text'}, status=400)
    text = text.replace('\x00', '').strip()
    if not text or len(text) > ATTEST_MAX:
        return JsonResponse({'error': f'a statement of 1 to {ATTEST_MAX} characters'}, status=400)
    if not motion_auth.challenge_is_fresh(challenge):
        return JsonResponse({'error': 'challenge expired; fetch a new one'}, status=400)
    origin = motion_auth.origin_of(request).lower().rstrip('/')
    message = motion_auth.attest_message(challenge, origin, text)
    name = motion_auth.signer_of(message, signature) or ''
    entity = ThinkingEntity.objects.filter(name=name).first() if name else None
    if entity is None:
        return JsonResponse({'error': 'signature not accepted'}, status=403)
    from .views_admin import locked_response
    from .services import settings as knobs
    if locked_response():
        return locked_response()
    if knobs.banned(name):
        return JsonResponse({'error': 'barred for this name; ask an admin'}, status=403)
    general, _ = Motion.objects.get_or_create(slug=GENERAL, defaults={
        'title': '#general', 'description': 'For everyone: what concerns us all, and statements signed with our keys.'})
    if Message.objects.filter(motion=general, source_file='motion-attest', content__signature=signature).exists():
        return JsonResponse({'error': 'already attested'}, status=409)
    message_row = Message.objects.create(
        id=uuid.uuid4(), sender=entity, motion=general, timestamp=int(time.time() * 1000), source_file='motion-attest',
        content={'type': 'attestation', 'text': text, 'signed': message, 'signature': signature,
                 'namespace': motion_auth.NAMESPACE, 'key': motion_auth.public_key_of(name)})
    return JsonResponse({'id': str(message_row.id), 'motion': GENERAL,
                         'url': request.build_absolute_uri(f'/motions/{GENERAL}/#m-{message_row.id}')}, status=201)


@require_http_methods(['GET', 'POST'])
def api_interrupt(request, slug):
    """Stop what an agent is doing in a Motion, as Esc does in a terminal.

    POST {"agent": "magent"}, as the device's person: a turn under way ends
    (its runner checks every few seconds), and a mention not yet answered is
    let go -- posted by mistake, say, to be added to. What was said stays
    in the Motion, so the next mention's wake still reads it. A line in the
    thread says who stopped whom.

    GET ?agent=magent: the newest such stop here, and whether an AZ5 is in
    force -- what a runner asks while a turn runs. Readable by anyone, like
    the thread it is a line in.
    """
    from .models import ConversationParticipant
    from .services import settings as knobs
    from .services.motion_view import INTERRUPT_SOURCE, latest_interrupt
    from .views_admin import locked_response
    motion = get_object_or_404(Motion, slug=slug)
    if request.method == 'GET':
        return JsonResponse({'scram': knobs.scram(), 'interrupt': latest_interrupt(motion, request.GET.get('agent'))})
    if locked_response():
        return locked_response()
    device, refused = motion_auth.key_device(request, 'stop an agent')
    if refused:
        return refused
    try:
        agent = str(json.loads(request.body or b'{}').get('agent') or 'magent').lower()
    except (ValueError, AttributeError):
        return JsonResponse({'error': 'expected {"agent": "<name>"}'}, status=400)
    if not ThinkingEntity.objects.filter(name=agent, is_biological_human=False).exists():
        return JsonResponse({'error': f'no agent named {agent}'}, status=400)
    system, _ = ConversationParticipant.objects.get_or_create(name='system', defaults={'participant_type': 'system'})
    Message.objects.create(id=uuid.uuid4(), sender=system, motion=motion, source_file=INTERRUPT_SOURCE,
                           content={'type': 'interrupt', 'agent': agent, 'by': device.entity_id},
                           timestamp=int(time.time() * 1000))
    return JsonResponse({'interrupt': latest_interrupt(motion, agent)})


@require_POST
def api_new_motion(request):
    """Start a Mood, as the device's person: POST {"title", "description"}.

    Its slug comes from the title (made unique), and never changes. Nothing
    else is needed here: the first @mention there wakes an agent in a new
    session (a runner that starts new ones; see poller/letter.md for what
    that session is told). A system row says who started it.
    """
    from django.utils.text import slugify
    from .models import ConversationParticipant
    from .services.motion_view import NEW_MOOD_SOURCE
    from .views_admin import locked_response
    if locked_response():
        return locked_response()
    device, refused = motion_auth.key_device(request, 'start a Mood')
    if refused:
        return refused
    try:
        body = json.loads(request.body or b'{}')
        title = str(body.get('title') or '').replace('\x00', '').strip()
        description = str(body.get('description') or '').replace('\x00', '').strip()
    except (ValueError, AttributeError):
        return JsonResponse({'error': 'expected {"title": ...}'}, status=400)
    if not title:
        return JsonResponse({'error': 'a title, please'}, status=400)
    if len(title) > 200 or len(description) > 2000:
        return JsonResponse({'error': 'at most 200 characters for a title, 2000 for a description'}, status=400)
    base = slugify(title)[:60].strip('-') or 'mood'
    slug, n = base, 2
    while Motion.objects.filter(slug=slug).exists():
        slug, n = f'{base}-{n}', n + 1
    motion = Motion.objects.create(slug=slug, title=title, description=description)
    system, _ = ConversationParticipant.objects.get_or_create(name='system', defaults={'participant_type': 'system'})
    Message.objects.create(id=uuid.uuid4(), sender=system, motion=motion, source_file=NEW_MOOD_SOURCE,
                           content={'type': 'created', 'by': device.entity_id, 'title': title},
                           timestamp=int(time.time() * 1000))
    return JsonResponse({'slug': motion.slug, 'title': motion.title, 'description': motion.description}, status=201)


@require_POST
def api_archive(request, slug):
    """Archive a Mood, or bring it back: POST {"archived": true|false}.

    Archiving only takes it out of the Moods list, into "Archived": it stays
    readable, its containers and sessions are untouched, and a mention there
    is still answered. Recorded as a setting row: who, and when.
    """
    return _mood_flag(request, slug, 'archived')


@require_POST
def api_pin(request, slug):
    """Pin a Mood to the top of the list, or unpin it: POST {"pinned": true|false}. For everyone."""
    return _mood_flag(request, slug, 'pinned')


def _mood_flag(request, slug, key):
    from .services import settings as knobs
    from .views_admin import locked_response
    if locked_response():
        return locked_response()
    device, refused = motion_auth.key_device(request, f'change what is {key}')
    if refused:
        return refused
    motion = get_object_or_404(Motion, slug=slug)
    try:
        value = json.loads(request.body or b'{}').get(key, True)
        knobs.change(key, value, motion=motion, by=device.entity, note='from the Mood')
    except (ValueError, AttributeError, knobs.Invalid) as e:
        return JsonResponse({'error': str(e) or f'expected {{"{key}": true|false}}'}, status=400)
    return JsonResponse({'slug': motion.slug, key: value})


VOICE_PER_MINUTE = 6


@require_POST
def api_memo(request, slug):
    """A voice memo, as the device's person: the body is the recording.

    Stored like an image (by its bytes' hash), then transcribed. Answers
    with the audio's URL and the transcript, for the person to look over
    and send -- nothing is posted until they do.
    """
    from .services import media, voice
    from .views_admin import locked_response
    if locked_response():
        return locked_response()
    device = motion_auth.device_for(request)
    if device is None:
        return JsonResponse({'error': 'sign in to write'}, status=401)
    motion = get_object_or_404(Motion, slug=slug)
    if not voice.enabled():
        return JsonResponse({'error': "voice isn't set up here (no ElevenLabs key)"}, status=503)
    if len(request.body) > media.MAX_AUDIO_BYTES:
        return JsonResponse({'error': f'larger than {media.MAX_AUDIO_BYTES // (1024 * 1024)} MB'}, status=413)
    if not _under_limit(f'voice:{device.pk}', VOICE_PER_MINUTE):
        return JsonResponse({'error': 'slow down'}, status=429)
    stored = media.store(request.body, added_by=device.entity, audio=True)
    if stored is None:
        return JsonResponse({'error': 'not a recording this understands (WebM, Ogg, MP4, MP3 or WAV)'}, status=400)
    try:
        heard = voice.transcribe(stored, motion, device.entity_id)
    except voice.VoiceError as e:
        return JsonResponse({'error': str(e), 'url': stored.url}, status=e.status)
    return JsonResponse({'url': stored.url, **heard}, status=201)


@require_POST
def api_speak(request, slug, message_id):
    """One message read aloud, for the device's person: {"url"} of the audio.

    Made the first time anyone asks and kept, so a message is paid for once
    per voice and direction. See services/voice.py for how agents direct it.
    """
    from .services import voice
    from .views_admin import locked_response
    if locked_response():
        return locked_response()
    device = motion_auth.device_for(request)
    if device is None:
        return JsonResponse({'error': 'sign in to hear messages read aloud'}, status=401)
    message = Message.objects.filter(id=_uuid_or_none(message_id), motion_id=slug).first()
    if message is None:
        return JsonResponse({'error': 'no such message in this Mood'}, status=404)
    if not _under_limit(f'voice:{device.pk}', VOICE_PER_MINUTE):
        return JsonResponse({'error': 'slow down'}, status=429)
    try:
        part = int(request.GET.get('part') or 0)
    except ValueError:
        part = 0
    try:
        return JsonResponse({'url': voice.speak(message, device.entity_id, part=part)})
    except voice.VoiceError as e:
        return JsonResponse({'error': str(e)}, status=e.status)


@require_GET
def api_voices(request):
    """The voices there are to choose from (for a ```voice block, or the house voice), and today's spend."""
    from .services import settings as knobs
    from .services import voice
    if not voice.enabled():
        return JsonResponse({'enabled': False, 'voices': []})
    from django.core.cache import cache
    note = ''
    try:
        listed = voice.voices()
        if cache.get('voice:refused'):
            note = f"{cache.get('voice:refused')} -- so these are ElevenLabs' standard voices, listed to anyone"
    except voice.VoiceError as e:
        listed = []
        note = (f"{e} -- so voices are named by id, and the house voice is "
                f"{knobs.global_value('voice') or voice.FALLBACK_VOICE_ID + ' (George)'}")
    return JsonResponse({'enabled': True, 'model': voice.TTS_MODEL, 'house_voice': knobs.global_value('voice'),
                         'spent_today_usd': voice.spent_today(),
                         'usd_per_day': knobs.global_value('voice_usd_per_day'), 'voices': listed,
                         **({'note': note} if note else {})})


def _uuid_or_none(value):
    try:
        return uuid.UUID(str(value))
    except ValueError:
        return None


@require_GET
def api_verify(request, slug, message_id):
    """An attestation, checked again now (motion_auth.verify_attestation): anyone may ask."""
    from .services.motion_view import attestation_of
    message = Message.objects.filter(id=_uuid_or_none(message_id), motion_id=slug).first()
    proof = attestation_of(message) if message else None
    if proof is None:
        return JsonResponse({'error': 'no such attestation in this Mood'}, status=404)
    checked = motion_auth.verify_attestation(message.sender_id, proof, (message.content or {}).get('text', ''))
    return JsonResponse({'signer': message.sender_id, 'checked_at': timezone.now().isoformat(), **checked})


# --- signing in with PickiPedia (services/wiki_auth.py) ------------------------

@require_GET
def wiki_signin(request):
    """Off to PickiPedia to say who you are; back at wiki_signin_return."""
    from .services import wiki_auth
    if not wiki_auth.enabled():
        return render(request, 'conversations/motion_login.html', {'state': 'no-wiki'}, status=404)
    state = wiki_auth.new_state()
    response = HttpResponseRedirect(wiki_auth.authorize_url(_wiki_return(request), state))
    response.set_cookie(wiki_auth.STATE_COOKIE, state, max_age=wiki_auth.STATE_AGE, httponly=True,
                        secure=not settings.DEBUG, samesite='Lax')
    return response


@require_GET
def wiki_signin_return(request):
    """PickiPedia vouched (or didn't): enrol this browser as a wiki-tier device."""
    from .services import wiki_auth
    from .views_admin import locked_response
    if not wiki_auth.enabled():
        return render(request, 'conversations/motion_login.html', {'state': 'no-wiki'}, status=404)
    if locked_response():
        return locked_response()
    expected = request.COOKIES.get(wiki_auth.STATE_COOKIE, '')
    if not expected or request.GET.get('state') != expected or not request.GET.get('code'):
        why = request.GET.get('error_description') or request.GET.get('error') or 'the sign-in went stale; try again'
        return render(request, 'conversations/motion_login.html', {'state': 'wiki-refused', 'why': why}, status=400)
    try:
        profile = wiki_auth.profile_for(request.GET['code'], _wiki_return(request))
        entity = wiki_auth.entity_for(profile['username'])
        from .services import settings as knobs
        if knobs.banned(entity.name):
            raise wiki_auth.SignInRefused('signing in is barred for this name; ask an admin')
    except wiki_auth.SignInRefused as e:
        return render(request, 'conversations/motion_login.html', {'state': 'wiki-refused', 'why': str(e)}, status=403)
    agent = request.META.get('HTTP_USER_AGENT', '')
    where = 'phone' if re.search(r'Mobi|Android|iPhone|iPad', agent, re.I) else 'browser'
    device, token = motion_auth.enrol_device(entity, f"PickiPedia sign-in ({profile['username']}, {where})", tier='wiki')
    response = HttpResponseRedirect('/motions/')
    response.set_cookie(motion_auth.COOKIE, token, max_age=motion_auth.COOKIE_AGE,
                        httponly=True, secure=not settings.DEBUG, samesite='Lax')
    response.delete_cookie(wiki_auth.STATE_COOKIE)
    return response


def _wiki_return(request):
    return request.build_absolute_uri('/motions/auth/wiki/callback')


@require_http_methods(['GET', 'POST'])
def api_seen(request):
    """How far the device's person has read, in every Mood: {"seen": {slug: iso}}.

    POST {"motion": slug} marks it read up to now (never back). Any signed-in
    device, either tier: it's their own reading. The page merges these with
    its own, so a phone's unread counts know what the laptop read.
    """
    from .models import ReadMark
    device = motion_auth.device_for(request)
    if device is None:
        return JsonResponse({'error': 'sign in to keep your place'}, status=401)
    if request.method == 'POST':
        try:
            slug = str(json.loads(request.body or b'{}')['motion'])
        except (ValueError, KeyError, TypeError):
            return JsonResponse({'error': 'expected {"motion": slug}'}, status=400)
        motion = get_object_or_404(Motion, slug=slug)
        now = timezone.now()
        mark, made = ReadMark.objects.get_or_create(entity=device.entity, motion=motion, defaults={'seen_at': now})
        if not made and mark.seen_at < now:
            ReadMark.objects.filter(pk=mark.pk, seen_at__lt=now).update(seen_at=now)
    marks = ReadMark.objects.filter(entity=device.entity).values_list('motion_id', 'seen_at')
    return JsonResponse({'seen': {slug: at.isoformat() for slug, at in marks}})

