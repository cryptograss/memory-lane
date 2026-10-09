"""Writing into Moods: device enrollment by SSH key, and the say endpoint.

See services/mood_auth.py for the design. Everything here that changes
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

from .models import Message, Mood, ThinkingEntity
from .services import mood_auth
from .services.redaction import redact

MAX_CHARS = 20_000
PER_MINUTE = 20
WEB_SOURCE = 'mood-web'
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
    return JsonResponse({'challenge': mood_auth.new_challenge(), 'namespace': mood_auth.NAMESPACE})


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

    if not mood_auth.challenge_is_fresh(challenge):
        return JsonResponse({'error': 'challenge expired; fetch a new one'}, status=400)
    message = mood_auth.signed_message(challenge, mood_auth.origin_of(request))
    if name:
        name = name if mood_auth.signature_is_valid(name, message, signature) else ''
    else:
        name = mood_auth.signer_of(message, signature) or ''  # the key says who you are
    entity = ThinkingEntity.objects.filter(name=name).first() if name else None
    if entity is None:
        return JsonResponse({'error': 'signature not accepted'}, status=403)
    from .views_admin import locked_response
    from .services import settings as knobs
    if locked_response():
        return locked_response()
    if knobs.banned(name):
        return JsonResponse({'error': 'signing in is barred for this name; ask an admin'}, status=403)

    code = mood_auth.issue_login_code(entity)
    url = request.build_absolute_uri(f'/moods/login/{code}/')
    return JsonResponse({'url': url, 'name': name, 'expires_in': int(mood_auth.CODE_LIFETIME.total_seconds())})


@ensure_csrf_cookie
@require_http_methods(['GET', 'POST'])
def login_page(request, code):
    """GET asks; POST enrolls this browser. A link preview only ever GETs.

    A link that can't be used says why: spent (when, for which device, and
    whether this very browser is the one it signed in), expired, or unknown.
    """
    if request.method == 'GET':
        state, detail = mood_auth.code_state(code)
        here = mood_auth.device_for(request)
        context = {'state': state, 'name': detail.entity_id if state == 'live' else None,
                   'used': detail if state == 'used' else None,
                   'signed_in_here': here.entity_id if here else None}
        return render(request, 'conversations/mood_login.html', context, status=200 if state == 'live' else 410)

    from .views_admin import locked_response
    if locked_response():
        return locked_response()
    label = getattr(settings, 'DEVICE_LABEL_PREFIX', '') + request.POST.get('label', '')
    device, token = mood_auth.redeem_login_code(code, label=label)
    if device is None:
        state, detail = mood_auth.code_state(code)
        return render(request, 'conversations/mood_login.html',
                      {'state': state, 'used': detail if state == 'used' else None}, status=410)
    from .services import access
    access.signed_in(device)  # said in #general: who, and with what
    response = HttpResponseRedirect('/moods/')
    response.set_cookie(mood_auth.COOKIE, token, max_age=mood_auth.COOKIE_AGE,
                        httponly=True, secure=not settings.DEBUG, samesite='Lax')
    return response


MEDIA_PER_MINUTE = 30


@require_POST
def api_media(request, slug):
    """Store an image this device's person is putting into a Mood.

    The body is the image itself; what it is comes from its bytes, not the
    Content-Type. Answers with the markdown to put in a message.
    """
    from .services import media
    from .views_admin import locked_response

    if locked_response():
        return locked_response()
    device = mood_auth.device_for(request)
    if device is None:
        return JsonResponse({'error': 'sign in to write'}, status=401)
    Mood.by_slug_or_404(slug)
    if len(request.body) > media.MAX_BYTES:
        return JsonResponse({'error': f'larger than {media.MAX_BYTES // (1024 * 1024)} MB'}, status=413)
    if not _under_limit(f'media:{device.pk}', MEDIA_PER_MINUTE):
        return JsonResponse({'error': 'slow down'}, status=429)
    stored = media.store(request.body, added_by=device.entity)
    if stored is None:
        return JsonResponse({'error': 'not a PNG, JPEG, GIF or WebP image'}, status=400)
    # Their own: told it's CC BY-SA 4.0, with CC0 a press away (api_media_license).
    return JsonResponse({'url': stored.url, 'markdown': media.markdown(stored), 'sha': stored.sha256,
                         'license': stored.license, 'mine': stored.added_by_id == device.entity_id}, status=201)


@require_GET
def api_me(request):
    device = mood_auth.device_for(request)
    return JsonResponse({'name': device.entity_id if device else None})


@require_POST
def api_logout(request):
    device = mood_auth.device_for(request)
    if device:
        type(device).objects.filter(pk=device.pk).update(revoked_at=timezone.now())
    response = JsonResponse({'ok': True})
    response.delete_cookie(mood_auth.COOKIE)
    return response


@require_POST
def api_say(request, slug):
    """Post a turn into a Mood as the device's person."""
    from .views_admin import locked_response
    if locked_response():
        return locked_response()
    device = mood_auth.device_for(request)
    if device is None:
        return JsonResponse({'error': 'sign in to write'}, status=401)
    mood = Mood.by_slug_or_404(slug)

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
        from .services.mood_view import addressed_in, known_names
        agents = set(ThinkingEntity.objects.filter(is_biological_human=False).values_list('name', flat=True))
        addressed = [n for n in addressed_in(text, known_names(), by=device.entity_id) if n in agents]
        if addressed:
            return JsonResponse({'error': f"signed in with PickiPedia, you can't address agents "
                                          f"(@{', @'.join(addressed)}): take the mention out, or sign in with "
                                          f"your SSH key (magenta.sh login)", 'agents': addressed}, status=403)

    # /clips: the team's saved clips (services/clips.py). A command either
    # becomes what's posted, or -- listing them -- posts nothing at all. Sent
    # as a reply, it's the words after the reply's '↩ #m-…' that are the command.
    from .services import clips
    from .services.mood_view import reply_to
    answering, said = reply_to(text)
    try:
        posted, note = clips.command(said, device.entity_id)
    except clips.Refused as e:
        return JsonResponse({'error': str(e)}, status=400)
    if posted is None:
        return JsonResponse({'note': note})
    if posted != said:
        text = f'↩ #m-{answering}\n{posted}' if answering else posted

    recent = Message.objects.filter(sender=device.entity, source_file=WEB_SOURCE,
                                    created_at__gt=timezone.now() - timedelta(minutes=1)).count()
    if recent >= PER_MINUTE:
        return JsonResponse({'error': 'slow down'}, status=429)

    def post(text, client_version=web_client(request, device)):
        text, _ = redact(text)
        return Message.objects.create(
            id=uuid.uuid4(), sender=device.entity, content=text[:MAX_CHARS], mood=mood,
            timestamp=int(time.time() * 1000), source_file=WEB_SOURCE, client_version=client_version,
        )

    # A memo still being heard (voice.hear_later) goes when its words are in:
    # now if they are, else in the background -- the page says it's on its way.
    from .services import voice
    memo = voice.memo_in(text)
    if memo and voice.heard(memo) is None:
        voice.post_when_heard(memo, text, post)
        return JsonResponse({'pending': memo}, status=202)
    if memo:
        text = voice.with_transcript(text, voice.heard(memo))
    message = post(text)
    if text.startswith('/clips '):
        clips.forget_cached()  # the library has a new save, or one fewer
    return JsonResponse({'id': str(message.id), **({'note': note} if note else {})}, status=201)


@require_POST
def api_message_edit(request, slug, message_id):
    """{"text"}: replace what you said (services/retract.py). Yours, from the web; never a signed statement."""
    return _retract(request, slug, message_id, edit=True)


@require_POST
def api_message_delete(request, slug, message_id):
    """Take a message's words out of the record (services/retract.py): yours, or anyone's as an admin."""
    return _retract(request, slug, message_id, edit=False)


def _retract(request, slug, message_id, edit):
    from .services import retract
    from .views_admin import locked_response
    if locked_response():
        return locked_response()
    device = mood_auth.device_for(request)
    if device is None:
        return JsonResponse({'error': 'sign in first'}, status=401)
    message = Message.objects.filter(id=_uuid_or_none(message_id), mood=Mood.by_slug(slug)).first()
    if message is None:
        return JsonResponse({'error': 'no such message in this Mood'}, status=404)
    if not _under_limit(f'retract:{device.pk}', 20):
        return JsonResponse({'error': 'slow down'}, status=429)
    if edit:
        if not retract.may_edit(message, device):
            return JsonResponse({'error': 'only what you wrote here yourself, within a day of writing it, can be edited'},
                                status=403)
        try:
            text = json.loads(request.body).get('text', '')
        except (ValueError, AttributeError):
            return JsonResponse({'error': 'expected {"text": ...}'}, status=400)
        text = (text if isinstance(text, str) else '').replace('\x00', '').strip()
        if not text:
            return JsonResponse({'error': 'nothing left: delete it instead'}, status=400)
        if len(text) > MAX_CHARS:
            return JsonResponse({'error': f'longer than {MAX_CHARS} characters'}, status=400)
        if device.tier == 'wiki':  # as when posting: a PickiPedia sign-in can't address agents
            from .services.mood_view import addressed_in, known_names
            agents = set(ThinkingEntity.objects.filter(is_biological_human=False).values_list('name', flat=True))
            if [n for n in addressed_in(text, known_names(), by=device.entity_id) if n in agents]:
                return JsonResponse({'error': "signed in with PickiPedia, you can't address agents"}, status=403)
        try:
            done = retract.edit(message, text, device.entity_id)
        except retract.Refused as e:
            return JsonResponse({'error': str(e)}, status=e.status)
    else:
        if not retract.may_delete(message, device):
            return JsonResponse({'error': 'only what you wrote, within a week of writing it, can be deleted '
                                          '(or anything, by an admin with their SSH key)'}, status=403)
        try:
            done = retract.delete(message, device.entity_id)
        except retract.Refused as e:
            return JsonResponse({'error': str(e)}, status=e.status)
    return JsonResponse({'id': str(message.id), 'kind': 'edited' if edit else 'deleted', **done})


@require_POST
def api_react(request, slug, message_id):
    """{"emoji"}: react to a message, or take the reaction back (services/reactions.py). Any sign-in.
    Answers the message's reactions as they now stand: {"reactions": {emoji: [who]}, "on"}."""
    from .services import reactions
    from .views_admin import locked_response
    if locked_response():
        return locked_response()
    device = mood_auth.device_for(request)
    if device is None:
        return JsonResponse({'error': 'sign in to react'}, status=401)
    message = Message.objects.filter(id=_uuid_or_none(message_id), mood=Mood.by_slug(slug)).first()
    if message is None:
        return JsonResponse({'error': 'no such message in this Mood'}, status=404)
    try:
        emoji = str(json.loads(request.body).get('emoji') or '').strip()
    except (ValueError, AttributeError):
        return JsonResponse({'error': 'expected {"emoji": ...}'}, status=400)
    if not reactions.valid(emoji):
        return JsonResponse({'error': 'an emoji, please'}, status=400)
    if not _under_limit(f'react:{device.pk}', 40):
        return JsonResponse({'error': 'slow down'}, status=429)
    on = reactions.toggle(message, device.entity_id, emoji)
    return JsonResponse({'on': on, 'reactions': reactions.in_mood(message.mood, [message.id]).get(str(message.id), {})})


# What was taken back, for an admin's page to open with the recovery key
# (services/retract.py). Sealed, it's nothing to anyone without the key's
# private half; that stays in the browser, and only what it opened comes back.

def _admin_in(request, slug):
    """(device, mood), or a JsonResponse saying why not."""
    from .services import retract
    device = mood_auth.device_for(request)
    if not retract.is_admin(device):
        return None, JsonResponse({'error': 'only an admin signed in with their SSH key'}, status=403)
    mood = Mood.by_slug(slug)
    if mood is None:
        return None, JsonResponse({'error': 'no such Mood'}, status=404)
    return (device, mood), None


@require_GET
def api_message_sealed(request, slug, message_id):
    """{'copies': [{'copy', 'kind', 'by', 'at', 'sealed'}], 'public'}: what a message said before each change, sealed."""
    from .services import retract
    found, refused = _admin_in(request, slug)
    if refused:
        return refused
    message = Message.objects.filter(id=_uuid_or_none(message_id), mood=found[1]).first()
    if message is None:
        return JsonResponse({'error': 'no such message in this Mood'}, status=404)
    return JsonResponse({'copies': retract.copies_of(message.pk),
                         'public': getattr(settings, 'MOOD_RECOVERY_PUBLIC_KEY', '')})


@require_GET
def api_mood_taken_back(request, slug):
    """{'copies': [{'copy', 'message', 'sender', 'kind', 'by', 'at'}]}: who took back what here, newest first."""
    from .services import retract
    found, refused = _admin_in(request, slug)
    if refused:
        return refused
    return JsonResponse({'copies': retract.taken_back_in(found[1])})


@require_POST
def api_message_restore(request, slug, message_id):
    """?copy=N, and the bytes that copy opened to: put the message back as it had it."""
    from .services import retract
    from .views_admin import locked_response
    if locked_response():
        return locked_response()
    found, refused = _admin_in(request, slug)
    if refused:
        return refused
    from .models import SealedCopy
    copy = SealedCopy.objects.filter(id=request.GET.get('copy') if (request.GET.get('copy') or '').isdigit() else None,
                                     message_id=_uuid_or_none(message_id), mood_slug=found[1].slug).first()
    if copy is None:
        return JsonResponse({'error': 'no such sealed copy of this message'}, status=404)
    try:
        retract.put_back(copy, retract.opened(copy, request.body), found[0].entity_id)
    except retract.Refused as e:
        return JsonResponse({'error': str(e)}, status=e.status)
    return JsonResponse({'id': str(copy.message_id), 'kind': 'restored', 'copy': copy.id})


@require_GET
def api_clips(request):
    """The saved clips, by name: for the composer's /clips suggestions."""
    from .services import clips
    return JsonResponse({'clips': [{'name': n, 'by': c['by'], 'clip': c['clip']}
                                   for n, c in sorted(clips.library().items())]})


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
    """Rename a Mood (its title, and optionally its description), as the device's person.

    Body: {"title": "...", "description": "..."}. Its slug follows the title,
    so its URL changes; the old slug stays an alias, so links and runners that
    know it by the old name still find it. The record keeps what it was called
    before: a system row in the Mood says who renamed it, from what, to what.
    """
    from .models import ConversationParticipant
    from .views_admin import locked_response
    if locked_response():
        return locked_response()
    device, refused = mood_auth.key_device(request, 'rename a Mood')
    if refused:
        return refused
    mood = Mood.by_slug_or_404(slug)
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

    was = {'title': mood.title, 'description': mood.description, 'slug': mood.slug}
    mood.rename(title, description)
    system, _ = ConversationParticipant.objects.get_or_create(name='system', defaults={'participant_type': 'system'})
    Message.objects.create(
        id=uuid.uuid4(), sender=system, mood=mood, timestamp=int(time.time() * 1000), source_file='mood-rename',
        content={'type': 'renamed', 'by': device.entity_id, 'from': was,
                 'to': {'title': mood.title, 'description': mood.description, 'slug': mood.slug}},
    )
    return JsonResponse({'title': mood.title, 'description': mood.description, 'slug': mood.slug})


# --- your devices: a list, revoking one, renewing one by name -------------------

def _device_payload(device, this):
    state = mood_auth.device_state(device)
    used = device.last_used_at or device.created_at
    return {'id': str(device.id), 'label': device.label or '(unnamed)', 'signed_in_at': device.created_at.isoformat(),
            'last_used_at': used.isoformat(), 'state': state,
            'times_out_at': (used + mood_auth.DEVICE_IDLE_LIMIT).isoformat() if state == 'live' else None,
            'signed_out_at': device.revoked_at.isoformat() if device.revoked_at else None,
            'this': this}


SIGNED_OUT_SHOWN = timedelta(days=14)


@require_GET
def api_devices(request):
    """The signed-in person's devices: when each signed in, last wrote, and times out.

    ?all=1, for an admin (settings.MOOD_ADMINS) on an SSH-key device: everyone's
    live devices, either tier, by person -- and those signed out in the last
    SIGNED_OUT_SHOWN, with when, so "why was I signed out?" can be answered.
    Seeing only: signing someone else out stays a signed admin action
    (magenta.sh kick), never a cookie's.
    """
    from .models import Device
    device = mood_auth.device_for(request)
    if device is None:
        return JsonResponse({'error': 'sign in to see your devices'}, status=401)
    if request.GET.get('all'):
        device, refused = mood_auth.key_device(request, "see everyone's devices")
        if refused:
            return refused
        if device.entity_id not in getattr(settings, 'MOOD_ADMINS', ()):
            return JsonResponse({'error': "only an admin can see everyone's devices"}, status=403)
        from django.db.models import Q
        people = {}
        recent = Q(revoked_at__isnull=True) | Q(revoked_at__gte=timezone.now() - SIGNED_OUT_SHOWN)
        from django.db.models import F
        # By person, by name; each one's live devices first, the most lately used first; then those signed
        # out lately, the latest first. (Spelled out: Postgres puts NULLs last ascending, SQLite first.)
        order = ('entity_id', F('revoked_at').desc(nulls_first=True), F('last_used_at').desc(nulls_last=True),
                 '-created_at')
        for d in Device.objects.filter(recent).order_by(*order):
            if d.revoked_at or mood_auth.device_state(d) == 'live':
                people.setdefault(d.entity_id, []).append(
                    {**_device_payload(d, d.pk == device.pk), 'tier': d.tier})
        return JsonResponse({'name': device.entity_id, 'idle_days': mood_auth.DEVICE_IDLE_LIMIT.days,
                             'people': [{'name': n, 'devices': ds} for n, ds in people.items()]})
    mine = Device.objects.filter(entity=device.entity).order_by('-created_at')
    return JsonResponse({'name': device.entity_id, 'idle_days': mood_auth.DEVICE_IDLE_LIMIT.days,
                         'admin': device.tier == 'key' and device.entity_id in getattr(settings, 'MOOD_ADMINS', ()),
                         'devices': [_device_payload(d, d.pk == device.pk) for d in mine]})


@require_POST
def api_device_revoke(request, device_id):
    """Sign one of your own devices out, for good (a new login makes a new one)."""
    from .models import Device
    device = mood_auth.device_for(request)
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
    if not mood_auth.challenge_is_fresh(challenge):
        return JsonResponse({'error': 'challenge expired; fetch a new one'}, status=400)
    message = mood_auth.signed_message(challenge, mood_auth.origin_of(request), purpose=f'renew {label}')
    name = mood_auth.signer_of(message, signature) or ''
    entity = ThinkingEntity.objects.filter(name=name).first() if name else None
    if entity is None:
        return JsonResponse({'error': 'signature not accepted'}, status=403)
    from .views_admin import locked_response
    from .services import settings as knobs
    if locked_response():
        return locked_response()
    if knobs.banned(name):
        return JsonResponse({'error': 'barred for this name; ask an admin'}, status=403)
    device = mood_auth.renew_device(entity, label)
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
    if not mood_auth.challenge_is_fresh(challenge):
        return JsonResponse({'error': 'challenge expired; fetch a new one'}, status=400)
    origin = mood_auth.origin_of(request).lower().rstrip('/')
    message = mood_auth.attest_message(challenge, origin, text)
    name = mood_auth.signer_of(message, signature) or ''
    entity = ThinkingEntity.objects.filter(name=name).first() if name else None
    if entity is None:
        return JsonResponse({'error': 'signature not accepted'}, status=403)
    from .views_admin import locked_response
    from .services import settings as knobs
    if locked_response():
        return locked_response()
    if knobs.banned(name):
        return JsonResponse({'error': 'barred for this name; ask an admin'}, status=403)
    general, _ = Mood.objects.get_or_create(slug=GENERAL, defaults={
        'title': '#general', 'description': 'For everyone: what concerns us all, and statements signed with our keys.'})
    if Message.objects.filter(mood=general, source_file='mood-attest', content__signature=signature).exists():
        return JsonResponse({'error': 'already attested'}, status=409)
    message_row = Message.objects.create(
        id=uuid.uuid4(), sender=entity, mood=general, timestamp=int(time.time() * 1000), source_file='mood-attest',
        content={'type': 'attestation', 'text': text, 'signed': message, 'signature': signature,
                 'namespace': mood_auth.NAMESPACE, 'key': mood_auth.public_key_of(name)})
    return JsonResponse({'id': str(message_row.id), 'mood': GENERAL,
                         'url': request.build_absolute_uri(f'/moods/{GENERAL}/#m-{message_row.id}')}, status=201)


@require_http_methods(['GET', 'POST'])
def api_interrupt(request, slug):
    """Stop what an agent is doing in a Mood, as Esc does in a terminal.

    POST {"agent": "magent"}, as the device's person: a turn under way ends
    (its runner checks every few seconds), and a mention not yet answered is
    let go -- posted by mistake, say, to be added to. What was said stays
    in the Mood, so the next mention's wake still reads it. A line in the
    thread says who stopped whom.

    GET ?agent=magent: the newest such stop here, and whether an AZ5 is in
    force -- what a runner asks while a turn runs. Readable by anyone, like
    the thread it is a line in.
    """
    from .models import ConversationParticipant
    from .services import settings as knobs
    from .services.mood_view import INTERRUPT_SOURCE, latest_interrupt
    from .views_admin import locked_response
    mood = Mood.by_slug_or_404(slug)
    if request.method == 'GET':
        return JsonResponse({'scram': knobs.scram(), 'interrupt': latest_interrupt(mood, request.GET.get('agent'))})
    if locked_response():
        return locked_response()
    device, refused = mood_auth.key_device(request, 'stop an agent')
    if refused:
        return refused
    try:
        agent = str(json.loads(request.body or b'{}').get('agent') or 'magent').lower()
    except (ValueError, AttributeError):
        return JsonResponse({'error': 'expected {"agent": "<name>"}'}, status=400)
    if not ThinkingEntity.objects.filter(name=agent, is_biological_human=False).exists():
        return JsonResponse({'error': f'no agent named {agent}'}, status=400)
    system, _ = ConversationParticipant.objects.get_or_create(name='system', defaults={'participant_type': 'system'})
    Message.objects.create(id=uuid.uuid4(), sender=system, mood=mood, source_file=INTERRUPT_SOURCE,
                           content={'type': 'interrupt', 'agent': agent, 'by': device.entity_id},
                           timestamp=int(time.time() * 1000))
    return JsonResponse({'interrupt': latest_interrupt(mood, agent)})


@require_POST
def api_new_mood(request):
    """Start a Mood, as the device's person: POST {"title", "description"}.

    Its slug comes from the title (made unique), and never changes. Nothing
    else is needed here: the first @mention there wakes an agent in a new
    session (a runner that starts new ones; see poller/letter.md for what
    that session is told). A system row says who started it.
    """
    from .models import ConversationParticipant
    from .services.mood_view import NEW_MOOD_SOURCE
    from .views_admin import locked_response
    if locked_response():
        return locked_response()
    device, refused = mood_auth.key_device(request, 'start a Mood')
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
    mood = Mood.objects.create(slug=Mood.free_slug(title), title=title, description=description)
    system, _ = ConversationParticipant.objects.get_or_create(name='system', defaults={'participant_type': 'system'})
    Message.objects.create(id=uuid.uuid4(), sender=system, mood=mood, source_file=NEW_MOOD_SOURCE,
                           content={'type': 'created', 'by': device.entity_id, 'title': title},
                           timestamp=int(time.time() * 1000))
    return JsonResponse({'slug': mood.slug, 'title': mood.title, 'description': mood.description}, status=201)


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
    device, refused = mood_auth.key_device(request, f'change what is {key}')
    if refused:
        return refused
    mood = Mood.by_slug_or_404(slug)
    try:
        value = json.loads(request.body or b'{}').get(key, True)
        knobs.change(key, value, mood=mood, by=device.entity, note='from the Mood')
    except (ValueError, AttributeError, knobs.Invalid) as e:
        return JsonResponse({'error': str(e) or f'expected {{"{key}": true|false}}'}, status=400)
    return JsonResponse({'slug': mood.slug, key: value})


VOICE_PER_MINUTE = 6


@require_POST
def api_memo(request, slug):
    """A voice memo, as the device's person: the body is the recording.

    Stored like an image (by its bytes' hash), and answered at once with its
    URL, to go in the box ready to send. Scribe starts on it now, in the
    background (voice.hear_later): sent, it's posted when its words are in.
    """
    from .services import media, voice
    from .views_admin import locked_response
    if locked_response():
        return locked_response()
    device = mood_auth.device_for(request)
    if device is None:
        return JsonResponse({'error': 'sign in to write'}, status=401)
    mood = Mood.by_slug_or_404(slug)
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
        voice.check_budget(voice.STT_USD_PER_HOUR * 10 / 60)  # refused now, not after it's sent
    except voice.VoiceError as e:
        return JsonResponse({'error': str(e), 'url': stored.url}, status=e.status)
    # "Magent, ..." or "at Skyler" said aloud becomes an @mention -- of an agent
    # only from an SSH-key sign-in, as typing one would be (api_say).
    from .services import wiki_auth
    from .services.mood_view import known_names
    mentionable = set(known_names())
    if device.tier == 'wiki':
        mentionable -= set(ThinkingEntity.objects.filter(is_biological_human=False).values_list('name', flat=True))
    voice.hear_later(stored, mood, device.entity_id, mentionable, wiki_auth.aliases())
    return JsonResponse({'url': stored.url}, status=201)


@require_POST
def api_speak(request, slug, message_id):
    """One message read aloud, for the device's person: {"url"} of the audio.

    Made the first time anyone asks and kept, so a message is paid for once
    per voice and direction. See services/voice.py for how agents direct it.

    ?intro=1 adds {"intro"}: the narrator saying who speaks ("Justin says:");
    ?intro=where, and in which Mood. ?only=intro: that alone (a voice memo is
    heard as recorded, so only its speaker needs saying).

    It comes in pieces (voice.pieces), so it starts at once: {"url"} is the
    first, and {"pieces"} how many; ?piece=N asks for the Nth, as the one
    before it starts playing.
    """
    from .services import voice
    from .views_admin import locked_response
    if locked_response():
        return locked_response()
    device = mood_auth.device_for(request)
    if device is None:
        return JsonResponse({'error': 'sign in to hear messages read aloud'}, status=401)
    message = Message.objects.filter(id=_uuid_or_none(message_id), mood=Mood.by_slug(slug)).first()
    if message is None:
        return JsonResponse({'error': 'no such message in this Mood'}, status=404)
    try:
        part = int(request.GET.get('part') or 0)
        piece = int(request.GET.get('piece') or 0)
    except ValueError:
        part = piece = 0
    # A message's later pieces are the same reading going on: only its start counts against the pace.
    if piece == 0 and not _under_limit(f'voice:{device.pk}', VOICE_PER_MINUTE):
        return JsonResponse({'error': 'slow down'}, status=429)
    intro = request.GET.get('intro', '')
    try:
        out = {}
        if intro or request.GET.get('only') == 'intro':
            out['intro'] = voice.intro(message, device.entity_id, where=intro == 'where')
        if request.GET.get('only') != 'intro':
            out.update(voice.speak_piece(message, device.entity_id, part=part, piece=piece))
        return JsonResponse(out)
    except voice.VoiceError as e:
        return JsonResponse({'error': str(e)}, status=e.status)


@require_GET
def api_voice_sample(request, voice_id):
    """A voice's sample (ElevenLabs' preview), served from here as audio/mpeg.

    Its host labels some samples text/plain, which Firefox won't play. Only
    the voices ElevenLabs lists -- nothing else is fetched -- and each kept a day.
    """
    from django.core.cache import cache
    from django.http import HttpResponse
    from .services import voice
    key = f'voice:sample:{voice_id[:40]}'
    audio = cache.get(key)
    if audio is None:
        try:
            listed = {v['voice_id']: v.get('preview_url') for v in voice.voices()}
        except voice.VoiceError:
            listed = {}
        url = listed.get(voice_id)
        if not url:
            return JsonResponse({'error': 'no sample of that voice'}, status=404)
        import requests
        try:
            answer = requests.get(url, timeout=15)
        except requests.RequestException:
            return JsonResponse({'error': 'the sample could not be fetched'}, status=502)
        if answer.status_code != 200 or not answer.content[:3] in (b'ID3', b'\xff\xfb', b'\xff\xf3', b'\xff\xf2'):
            return JsonResponse({'error': 'the sample is not audio'}, status=502)
        audio = answer.content
        cache.set(key, audio, 86400)
    response = HttpResponse(audio, content_type='audio/mpeg')
    response['Cache-Control'] = 'public, max-age=86400'
    return response


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
                         # Who is read in which voice, where it's been chosen or given (voice.voice_of).
                         'speakers': voice.chosen_voices(), 'narrator': voice.NARRATOR,
                         **({'note': note} if note else {})})


HEARD_OUTCOMES = ('ended', 'failed', 'blocked', 'skipped')


@require_POST
def api_voice_heard(request):
    """What a page's reader (reading as they come) did with one clip: {"mood", "message",
    "clip": intro|message|memo, "outcome": ended|failed|blocked|skipped, "error", "hidden"}.
    Kept as a voice row in that Mood, so what a phone's player did can be read afterwards."""
    from .services import voice
    device = mood_auth.device_for(request)
    if device is None:
        return JsonResponse({'error': 'sign in first'}, status=401)
    if not _under_limit(f'heard:{device.pk}', 60):
        return JsonResponse({'error': 'slow down'}, status=429)
    try:
        body = json.loads(request.body)
        outcome, clip = str(body['outcome']), str(body.get('clip') or '')[:10]
    except (ValueError, KeyError, TypeError):
        return JsonResponse({'error': 'expected {"mood", "message", "clip", "outcome"}'}, status=400)
    mood = Mood.by_slug(str(body.get('mood') or ''))
    if mood is None or outcome not in HEARD_OUTCOMES:
        return JsonResponse({'error': f'a Mood, and an outcome of {", ".join(HEARD_OUTCOMES)}'}, status=400)
    voice.record(mood, 'heard', device.entity_id, message=str(body.get('message') or '')[:40], clip=clip,
                 outcome=outcome, error=redact(str(body.get('error') or ''))[0][:200], hidden=bool(body.get('hidden')),
                 device=device.label[:60])
    return JsonResponse({'kept': True}, status=201)


@require_POST
def api_speaker_voice(request):
    """{"name", "voice"}: the voice `name`'s messages are read in (a voice's id
    or name; empty to be given one again). Your own, from any device; anyone
    else's -- an agent's included -- from a device signed in with an SSH key."""
    from .services import settings as knobs
    from .services import voice
    from .views_admin import locked_response
    if locked_response():
        return locked_response()
    device = mood_auth.device_for(request)
    if device is None:
        return JsonResponse({'error': 'sign in to choose voices'}, status=401)
    try:
        body = json.loads(request.body)
        name, chosen = str(body['name']).lower(), str(body.get('voice') or '').strip()
    except (ValueError, KeyError, TypeError):
        return JsonResponse({'error': 'expected {"name": ..., "voice": ...}'}, status=400)
    entity = ThinkingEntity.objects.filter(name=name).first()
    if entity is None and name != voice.NARRATOR:
        return JsonResponse({'error': f'nobody called {name}'}, status=404)
    if name != device.entity_id and (device.tier != 'key' or not device.entity.is_biological_human):
        return JsonResponse({'error': "only your own voice, from a PickiPedia sign-in; others' take your SSH key"},
                            status=403)
    if chosen:
        try:
            listed = voice.voices()
        except voice.VoiceError:
            listed = []
        known = {v['voice_id'] for v in listed} | {v['name'].lower() for v in listed}
        if chosen.lower() not in known and not voice._VOICE_ID.match(chosen):
            return JsonResponse({'error': f'no voice called {chosen}'}, status=400)
    try:
        if entity is None:  # the narrator: everyone's, so an SSH key's to change
            row = knobs.change('narrator_voice', chosen, by=device.entity, note=body.get('note', ''))
        else:
            row = knobs.change('speaker_voice', chosen, agent=entity, by=device.entity, note=body.get('note', ''))
    except knobs.Invalid as e:
        return JsonResponse({'error': str(e)}, status=400)
    return JsonResponse(knobs.describe(row), status=201)


@require_POST
def api_narrate(request):
    """{"mood", "agent", "where"}: the narrator saying an agent has started work there
    ("Pound magenta interface. Magent is thinking."), for reading Moods as they come: {"url"}.
    {"mood", "deploy": a deploy row's id}: saying a server redeployed, or failed to."""
    from .services import voice
    from .views_admin import locked_response
    if locked_response():
        return locked_response()
    device = mood_auth.device_for(request)
    if device is None:
        return JsonResponse({'error': 'sign in to hear Moods read aloud'}, status=401)
    try:
        body = json.loads(request.body)
    except ValueError:
        return JsonResponse({'error': 'expected JSON'}, status=400)
    mood = Mood.by_slug(str(body.get('mood') or ''))
    if body.get('deploy'):
        row = Message.objects.filter(id=_uuid_or_none(str(body['deploy'])), source_file='deploy').first()
        if mood is None or row is None:
            return JsonResponse({'error': 'no such Mood, or no such redeploy'}, status=404)
        if not _under_limit(f'voice:{device.pk}', VOICE_PER_MINUTE):
            return JsonResponse({'error': 'slow down'}, status=429)
        try:
            url = voice.narrate_deploy(row, mood, device.entity_id)
        except voice.VoiceError as e:
            return JsonResponse({'error': str(e)}, status=e.status)
        return JsonResponse({'url': url}) if url else JsonResponse({'error': 'only a finished or failed redeploy is said'}, status=400)
    agent = ThinkingEntity.objects.filter(name=str(body.get('agent') or ''), is_biological_human=False).first()
    if mood is None or agent is None:
        return JsonResponse({'error': 'no such Mood, or no such agent'}, status=404)
    if not _under_limit(f'voice:{device.pk}', VOICE_PER_MINUTE):
        return JsonResponse({'error': 'slow down'}, status=429)
    try:
        return JsonResponse({'url': voice.narrate_thinking(mood, agent.name, device.entity_id, where=bool(body.get('where')))})
    except voice.VoiceError as e:
        return JsonResponse({'error': str(e)}, status=e.status)


def _uuid_or_none(value):
    try:
        return uuid.UUID(str(value))
    except ValueError:
        return None


@require_GET
def api_verify(request, slug, message_id):
    """An attestation, checked again now (mood_auth.verify_attestation): anyone may ask."""
    from .services.mood_view import attestation_of
    message = Message.objects.filter(id=_uuid_or_none(message_id), mood=Mood.by_slug(slug)).first()
    proof = attestation_of(message) if message else None
    if proof is None:
        return JsonResponse({'error': 'no such attestation in this Mood'}, status=404)
    checked = mood_auth.verify_attestation(message.sender_id, proof, (message.content or {}).get('text', ''))
    return JsonResponse({'signer': message.sender_id, 'checked_at': timezone.now().isoformat(), **checked})


# --- signing in with PickiPedia (services/wiki_auth.py) ------------------------

@require_GET
def wiki_signin(request):
    """Off to PickiPedia to say who you are; back at wiki_signin_return."""
    from .services import wiki_auth
    if not wiki_auth.enabled():
        return render(request, 'conversations/mood_login.html', {'state': 'no-wiki'}, status=404)
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
        return render(request, 'conversations/mood_login.html', {'state': 'no-wiki'}, status=404)
    if locked_response():
        return locked_response()
    expected = request.COOKIES.get(wiki_auth.STATE_COOKIE, '')
    if not expected or request.GET.get('state') != expected or not request.GET.get('code'):
        why = request.GET.get('error_description') or request.GET.get('error') or 'the sign-in went stale; try again'
        return render(request, 'conversations/mood_login.html', {'state': 'wiki-refused', 'why': why}, status=400)
    try:
        profile = wiki_auth.profile_for(request.GET['code'], _wiki_return(request))
        entity = wiki_auth.entity_for(profile['username'])
        from .services import settings as knobs
        if knobs.banned(entity.name):
            raise wiki_auth.SignInRefused('signing in is barred for this name; ask an admin')
    except wiki_auth.SignInRefused as e:
        return render(request, 'conversations/mood_login.html', {'state': 'wiki-refused', 'why': str(e)}, status=403)
    agent = request.META.get('HTTP_USER_AGENT', '')
    where = 'phone' if re.search(r'Mobi|Android|iPhone|iPad', agent, re.I) else 'browser'
    device, token = mood_auth.enrol_device(entity, f"PickiPedia sign-in ({profile['username']}, {where})", tier='wiki')
    from .services import access
    access.signed_in(device)  # said in #general: who, and with what
    response = HttpResponseRedirect('/moods/')
    response.set_cookie(mood_auth.COOKIE, token, max_age=mood_auth.COOKIE_AGE,
                        httponly=True, secure=not settings.DEBUG, samesite='Lax')
    response.delete_cookie(wiki_auth.STATE_COOKIE)
    return response


def _wiki_return(request):
    return request.build_absolute_uri('/moods/auth/wiki/callback')


@require_http_methods(['GET', 'POST'])
def api_seen(request):
    """How far the device's person has read, in every Mood: {"seen": {slug: iso}}.

    POST {"mood": slug} marks it read up to now (never back). Any signed-in
    device, either tier: it's their own reading. The page merges these with
    its own, so a phone's unread counts know what the laptop read.
    """
    from .models import ReadMark
    device = mood_auth.device_for(request)
    if device is None:
        return JsonResponse({'error': 'sign in to keep your place'}, status=401)
    if request.method == 'POST':
        try:
            slug = str(json.loads(request.body or b'{}')['mood'])
        except (ValueError, KeyError, TypeError):
            return JsonResponse({'error': 'expected {"mood": slug}'}, status=400)
        mood = Mood.by_slug_or_404(slug)
        now = timezone.now()
        mark, made = ReadMark.objects.get_or_create(entity=device.entity, mood=mood, defaults={'seen_at': now})
        if not made and mark.seen_at < now:
            ReadMark.objects.filter(pk=mark.pk, seen_at__lt=now).update(seen_at=now)
    marks = ReadMark.objects.filter(entity=device.entity).values_list('mood__slug', 'seen_at')
    return JsonResponse({'seen': {slug: at.isoformat() for slug, at in marks}})



# --- notifications with magenta closed (services/push.py) ---------------------------

@require_GET
def api_push(request):
    """Whether push is on here, and if a key is set but push is off, why
    (the library missing, a key it can't read) -- never the key."""
    from .services import push
    return JsonResponse({'enabled': push.enabled(), 'problem': push.problem()})


@require_POST
def api_push_subscribe(request):
    """This device's push subscription, from the browser's PushManager:
    {"endpoint", "keys": {"p256dh", "auth"}}. Its bell, turned on."""
    from .models import PushSubscription
    from .services import push
    device = mood_auth.device_for(request)
    if device is None:
        return JsonResponse({'error': 'sign in to be notified'}, status=401)
    if not push.enabled():
        return JsonResponse({'error': "push isn't set up here"}, status=503)
    try:
        body = json.loads(request.body)
        endpoint, keys = str(body['endpoint']), body['keys']
        p256dh, auth = str(keys['p256dh']), str(keys['auth'])
    except (ValueError, KeyError, TypeError):
        return JsonResponse({'error': 'expected {"endpoint": ..., "keys": {"p256dh": ..., "auth": ...}}'}, status=400)
    if not endpoint.startswith('https://') or len(endpoint) > 2000 or len(p256dh) > 200 or len(auth) > 100:
        return JsonResponse({'error': 'not a push subscription'}, status=400)
    PushSubscription.objects.update_or_create(endpoint=endpoint, defaults={'device': device, 'p256dh': p256dh, 'auth': auth})
    return JsonResponse({'subscribed': True}, status=201)


@require_POST
def api_push_unsubscribe(request):
    """{"endpoint"}: no more pushes there. Its bell, turned off."""
    from .models import PushSubscription
    device = mood_auth.device_for(request)
    if device is None:
        return JsonResponse({'error': 'sign in first'}, status=401)
    try:
        endpoint = str(json.loads(request.body)['endpoint'])
    except (ValueError, KeyError, TypeError):
        return JsonResponse({'error': 'expected {"endpoint": ...}'}, status=400)
    PushSubscription.objects.filter(endpoint=endpoint, device__entity=device.entity).delete()
    return JsonResponse({'subscribed': False})


@require_POST
def api_media_to_pickipedia(request, sha256):
    """{"mood", "message", "name", "description", "license"}: put a picture on PickiPedia as whoever
    shared it (services/wiki_upload.py), from either sign-in. Answers {"go": PickiPedia's "may magenta
    upload for you?"}, to be sent to; or, if the picture is there already, {"file", "page"}."""
    from .models import Media
    from .services import wiki_upload
    from .views_admin import locked_response
    if locked_response():
        return locked_response()
    device = mood_auth.device_for(request)
    if device is None:
        return JsonResponse({'error': 'sign in first'}, status=401)
    if not _under_limit(f'wiki-upload:{device.pk}', 6):
        return JsonResponse({'error': 'slow down'}, status=429)
    try:
        body = json.loads(request.body)
    except ValueError:
        return JsonResponse({'error': 'expected JSON'}, status=400)
    media = Media.objects.filter(sha256=sha256).first()
    mood = Mood.by_slug(str(body.get('mood') or ''))
    if media is None or mood is None:
        return JsonResponse({'error': 'no such picture, or no such Mood'}, status=404)
    try:
        done = wiki_upload.begin(media, str(body.get('name') or ''), str(body.get('description') or '')[:2000],
                                 str(body.get('license') or 'cc-by-sa-4.0'), device.entity_id, mood,
                                 _uuid_or_none(body.get('message')), _wiki_upload_return(request))
    except wiki_upload.UploadError as e:
        return JsonResponse({'error': str(e)}, status=e.status)
    if 'go' not in done:
        return JsonResponse(done)
    response = JsonResponse({'go': done['go']})
    response.set_cookie(wiki_upload.STATE_COOKIE, done['state'], max_age=wiki_upload.PENDING_FOR, httponly=True,
                        secure=not settings.DEBUG, samesite='Lax')
    return response


@require_GET
def wiki_upload_return(request):
    """Back from PickiPedia with its yes (or not): upload as them, then back to the picture."""
    from .services import wiki_upload
    from .views_admin import locked_response
    if locked_response():
        return locked_response()
    expected = request.COOKIES.get(wiki_upload.STATE_COOKIE, '')
    back = wiki_upload.place(expected)
    device = mood_auth.device_for(request)
    try:
        if device is None:
            raise wiki_upload.UploadError('this browser is signed out of magenta', status=401)
        if request.GET.get('error') == 'access_denied':
            raise wiki_upload.UploadError("You didn't allow it on PickiPedia, so nothing went up.")
        if request.GET.get('error'):
            raise wiki_upload.UploadError('PickiPedia gave no permission, so nothing went up: '
                                          + (request.GET.get('error_description') or request.GET['error']))
        if not expected or request.GET.get('state') != expected or not request.GET.get('code'):
            raise wiki_upload.UploadError('that went stale; press → PickiPedia again')
        done = wiki_upload.finish(expected, request.GET['code'], device.entity_id, _wiki_upload_return(request))
    except wiki_upload.UploadError as e:
        response = render(request, 'conversations/mood_login.html',
                          {'state': 'upload-refused', 'why': str(e), 'back': back}, status=e.status)
    else:
        response = HttpResponseRedirect(done['back'])
    response.delete_cookie(wiki_upload.STATE_COOKIE)
    return response


def _wiki_upload_return(request):
    return request.build_absolute_uri('/moods/auth/wiki/upload')


@require_http_methods(['GET', 'POST'])
def api_media_license(request, sha256):
    """A picture's license (services/media.LICENSES): {"license", "mine"}. POST {"license"}
    sets it: only whoever shared it may."""
    from .models import Media
    from .services import media as media_service
    media = Media.objects.filter(sha256=sha256).first()
    if media is None:
        return JsonResponse({'error': 'no such picture'}, status=404)
    device = mood_auth.device_for(request)
    if request.method == 'POST':
        if device is None:
            return JsonResponse({'error': 'sign in first'}, status=401)
        try:
            wanted = str(json.loads(request.body).get('license') or '')
        except (ValueError, AttributeError):
            return JsonResponse({'error': 'expected {"license": ...}'}, status=400)
        if wanted not in media_service.LICENSES:
            return JsonResponse({'error': 'CC BY-SA 4.0 or CC0: ' + ', '.join(media_service.LICENSES)}, status=400)
        if not media_service.relicense(media, wanted, device.entity_id):
            return JsonResponse({'error': "only whoever shared a picture can choose its license"}, status=403)
    return JsonResponse({'license': media.license, 'mine': bool(device) and media.added_by_id == device.entity_id})
