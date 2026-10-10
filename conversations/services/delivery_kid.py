"""A video sent into a Mood: to delivery-kid, then onto PickiPedia as a ReleaseDraft, as its sender.

Pictures stay in magenta (services/media.py); a video is too big for that and
belongs where the band's videos go: delivery-kid, which transcodes and pins
them, with a ReleaseDraft: page on PickiPedia where it's titled, reviewed and
finalized. This is the wiki's own Special:DeliverVideo, from a Mood:

1. **The bytes go from the browser straight to delivery-kid,** never through
   here. What this server does is mint the per-upload token the wiki mints
   (an HMAC of "upload:<PickiPedia name>:<ms>"), so the key never reaches a
   browser. delivery-kid records the upload as wiki:<their PickiPedia name>.
   The key is delivery-kid's *upload-only* key (settings.
   DELIVERY_KID_UPLOAD_KEY; delivery-kid's upload_key), not its API key: it
   signs upload tokens and nothing else, and delivery-kid lets them live a
   day. Finalizing is the wiki's finalize-release right, and magenta holds
   no authority that could exercise it (Justin, 10 Oct).
2. **The ReleaseDraft page is made as them,** through PickiPedia's "may
   magenta edit for you?" (the "magenta uploads" consumer that puts pictures
   on PickiPedia: services/wiki_upload.py), and the permission is dropped
   once the page is written. What the page says about the files comes from
   delivery-kid itself, asked here, not from the browser.
3. **A line in the Mood** (a 'release-draft' event) links the draft.

Finalizing stays on the ReleaseDraft page, under the wiki's own
finalize-release right; delivery-kid runs it as a job of its own
(maybelle-config#175), so nobody has to keep a tab open.

People only. An agent's device gets no token, and a PickiPedia account in
the wiki's bot group gets no page (Justin, 10 Oct: human PickiPedians). What
the band's videos are is decided by the people in it.

Unset (no DELIVERY_KID_UPLOAD_KEY, or no "magenta uploads" consumer), videos
can't be sent and the composer takes pictures only, as before.
"""

import hashlib
import hmac
import json
import re
import time

from django.conf import settings
from django.core.cache import cache

from .wiki_upload import PENDING_FOR, UploadError

SOURCE = 'release-draft'
PENDING_PREFIX = 'video-draft:'
_DRAFT_ID = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
_SIGNED_IN_AS = re.compile(r'^PickiPedia sign-in \((.+), [^,]*\)$')
TITLE_MAX = 200


def enabled():
    from . import wiki_upload
    return bool(getattr(settings, 'DELIVERY_KID_UPLOAD_KEY', '') and getattr(settings, 'DELIVERY_KID_URL', '')
                and wiki_upload.enabled())


def base_url():
    return settings.DELIVERY_KID_URL.rstrip('/')


def person_only(device):
    """UploadError unless the device belongs to a person here."""
    if not getattr(getattr(device, 'entity', None), 'is_biological_human', False):
        raise UploadError('videos are sent by people; agents bring them in through a person', status=403)


def wiki_name_for(device):
    """Their PickiPedia name, as delivery-kid should record the upload, or None.

    Whoever has one in the names list (MOOD_WIKI_NAMES) has it from there;
    someone who signed in with PickiPedia has it in their device's label,
    which said sign-in wrote (views_auth.wiki_signin_return). Either way it is
    checked against PickiPedia itself before any page is written (finish()).
    """
    from . import wiki_auth
    known = wiki_auth.names().get(str(device.entity_id).lower())
    if known:
        return known
    if getattr(device, 'tier', None) == 'wiki':
        said = _SIGNED_IN_AS.match(getattr(device, 'label', '') or '')
        if said:
            return said.group(1)
    return None


def token_headers(wiki_name, now_ms=None):
    """An upload token, in the headers delivery-kid checks (its auth.verify_upload_token).

    Upload only: there is no other kind to ask for here, and the key couldn't
    sign one delivery-kid would take.
    """
    stamp = int(now_ms if now_ms is not None else time.time() * 1000)
    signed = hmac.new(settings.DELIVERY_KID_UPLOAD_KEY.encode(), f'upload:{wiki_name}:{stamp}'.encode(),
                      hashlib.sha256).hexdigest()
    return {'X-Upload-Token': signed, 'X-Upload-User': wiki_name, 'X-Upload-Timestamp': str(stamp)}


def ticket(device):
    """Where the browser sends the video, and with what: {'url', 'headers'}."""
    if not enabled():
        raise UploadError("sending videos to delivery-kid isn't set up here", status=503)
    person_only(device)
    wiki_name = wiki_name_for(device)
    if not wiki_name:
        raise UploadError("magenta doesn't know your PickiPedia name, which delivery-kid records uploads under: "
                          "sign in with PickiPedia, or ask an admin to map your account", status=403)
    return {'url': base_url() + '/draft-content', 'headers': token_headers(wiki_name), 'as': wiki_name}


def fetch_draft(draft_id, wiki_name, http=None):
    """What delivery-kid holds for a draft: its files, as it analysed them, and its build."""
    http = http or _requests()
    answer = http.get(f'{base_url()}/draft-content/{draft_id}', headers=token_headers(wiki_name), timeout=20)
    if getattr(answer, 'status_code', 500) == 404:
        raise UploadError('delivery-kid has no such draft', status=404)
    if getattr(answer, 'status_code', 500) == 403:
        # delivery-kid shows a draft only to whoever uploaded it, by PickiPedia
        # name: so asking as the name PickiPedia just vouched for is also the
        # check that this draft is theirs to write a page for.
        raise UploadError("that video was sent by someone else's PickiPedia account, so its page isn't yours "
                          "to make", status=403)
    if getattr(answer, 'status_code', 500) != 200:
        raise UploadError(f'delivery-kid answered {getattr(answer, "status_code", "?")}', status=502)
    try:
        return answer.json()
    except ValueError:
        raise UploadError("delivery-kid's answer wasn't JSON", status=502)


def _q(value):
    """A YAML scalar that can't be misread: JSON's double-quoted string is valid YAML."""
    return json.dumps('' if value is None else str(value), ensure_ascii=False)


def draft_yaml(draft_id, draft, title, uploader, upload_blockheight=None):
    """The ReleaseDraft page, laid out as Special:DeliverVideo writes it (buildVideoYaml)."""
    lines = [f'draft_id: {draft_id}', 'type: video', 'source: magenta',
             f'commit: {_q(draft.get("commit") or "unknown")}', f'uploader: {_q(uploader)}',
             'blockheight: null', f'upload_blockheight: {int(upload_blockheight) if upload_blockheight else "null"}',
             'content:', f'    title: {_q(title)}', '    description: ""', '    file_type: ""', '    venue: ""',
             '    performers:', 'files:']
    for f in draft.get('files') or []:
        lines.append('    -')
        lines.append(f'        original_filename: {_q(f.get("original_filename"))}')
        lines.append(f'        media_type: {_q(f.get("media_type"))}')
        lines.append(f'        format: {_q(f.get("format"))}')
        for key in ('duration_seconds', 'width', 'height', 'size_bytes'):
            if isinstance(f.get(key), (int, float)) and not isinstance(f.get(key), bool):
                lines.append(f'        {key}: {f[key]}')
        for key in ('video_codec', 'audio_codec', 'creation_time'):
            if f.get(key):
                lines.append(f'        {key}: {_q(f[key])}')
    return '\n'.join(lines) + '\n'


def draft_page(draft_id):
    from urllib.parse import quote
    from .mood_view import pickipedia_url
    return f'{pickipedia_url()}/wiki/{quote("ReleaseDraft:" + draft_id, safe=":")}'


def begin(draft_id, title, device, mood, redirect_uri):
    """The first half, before PickiPedia's yes: {'go', 'state'}."""
    from urllib.parse import urlencode
    from .mood_view import pickipedia_url
    from .wiki_auth import new_state
    if not enabled():
        raise UploadError("sending videos to delivery-kid isn't set up here", status=503)
    draft_id = str(draft_id or '').lower()
    if not _DRAFT_ID.match(draft_id):
        raise UploadError('not a delivery-kid draft id')
    person_only(device)
    wiki_name = wiki_name_for(device)
    if not wiki_name:
        raise UploadError("magenta doesn't know your PickiPedia name", status=403)
    title = ' '.join(str(title or '').split())[:TITLE_MAX]
    state = new_state()
    cache.set(PENDING_PREFIX + state, {'draft': draft_id, 'title': title, 'by': device.entity_id,
                                       'as': wiki_name, 'mood': mood.slug}, PENDING_FOR)
    query = urlencode({'response_type': 'code', 'client_id': settings.PICKIPEDIA_UPLOAD_CLIENT_ID,
                       'redirect_uri': redirect_uri, 'state': state})
    return {'go': f'{pickipedia_url()}/rest.php/oauth2/authorize?{query}', 'state': state}


def pending(state):
    """The draft a PickiPedia round trip is for, if it is for one."""
    return cache.get(PENDING_PREFIX + state) if state else None


def place(state):
    found = pending(state)
    return f'/moods/{found["mood"]}/' if found else '/moods/'


def finish(state, code, entity, redirect_uri, http=None):
    """The second half, with PickiPedia's yes: write the ReleaseDraft page as them, then forget the permission."""
    from conversations.models import Mood
    from . import wiki_auth
    from .wiki_upload import Wiki
    if not enabled():
        raise UploadError("sending videos to delivery-kid isn't set up here", status=503)
    found = pending(state)
    cache.delete(PENDING_PREFIX + state)  # once only
    name_here = getattr(entity, 'pk', entity)
    if not found or found['by'] != name_here:
        raise UploadError('that went stale; send the video again')
    try:
        token = wiki_auth.exchange(code, redirect_uri, settings.PICKIPEDIA_UPLOAD_CLIENT_ID,
                                   settings.PICKIPEDIA_UPLOAD_CLIENT_SECRET, http=http or _requests())
        profile = wiki_auth.profile_with(token, http=http or _requests())
    except wiki_auth.SignInRefused as e:
        raise UploadError(str(e), status=502)
    if profile.get('blocked'):
        raise UploadError('that PickiPedia account is blocked', status=403)
    if 'bot' in (profile.get('groups') or []):
        # The wiki's own word for an account that isn't a person.
        raise UploadError(f'{profile["username"]} is a bot account on PickiPedia; videos are sent as a person',
                          status=403)
    if wiki_auth.local_name_for(profile['username']) != name_here:
        raise UploadError(f"you're signed in to PickiPedia as {profile['username']}, which isn't {name_here}'s account "
                          "here: sign in there as yourself (or ask an admin to map your account), then try again",
                          status=403)
    mood = Mood.by_slug(found['mood'])
    if mood is None:
        raise UploadError('that Mood is gone', status=404)
    draft = fetch_draft(found['draft'], profile['username'], http=http)
    title = found['title'] or _title_from(draft)
    text = draft_yaml(found['draft'], draft, title, profile['username'], _block_now())
    wiki = Wiki(http, token=token)
    csrf = wiki.call(action='query', meta='tokens')['query']['tokens']['csrftoken']
    wiki.call(post=True, action='edit', title=f'ReleaseDraft:{found["draft"]}', text=text, createonly=1,
              summary='New video draft, sent from magenta', token=csrf)
    details = {'title': title, 'page': draft_page(found['draft']), 'draft': found['draft']}
    _record(mood, name_here, **details)
    return {**details, 'back': f'/moods/{mood.slug}/'}


def _title_from(draft):
    files = draft.get('files') or []
    return (files[0].get('detected_title') or files[0].get('original_filename') or '') if files else ''


def _block_now():
    """The Ethereum block as the draft is made (upload_blockheight), or None: never worth failing over."""
    try:
        from . import eth_blocks
        return eth_blocks.fetch_head(eth_blocks.rpc_url())[0]
    except Exception:
        return None


def _requests():
    import requests
    return requests


def _record(mood, by, **details):
    """A line in the Mood (an event, mood_view.EVENT_SOURCES): who sent which video, and where its draft is."""
    import uuid
    from conversations.models import ConversationParticipant, Message
    system, _ = ConversationParticipant.objects.get_or_create(name='system', defaults={'participant_type': 'system'})
    return Message.objects.create(id=uuid.uuid4(), sender=system, mood=mood, source_file=SOURCE,
                                  content={'type': 'release-draft', 'by': by, **details},
                                  timestamp=int(time.time() * 1000))
