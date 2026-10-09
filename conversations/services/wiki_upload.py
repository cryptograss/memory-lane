"""A picture from a Mood, onto PickiPedia: by anyone here, as themselves, credited to whoever shared it.

Pictures sent into Moods stay in memory-lane (services/media.py), under the
license their sharer gave them there: CC BY-SA 4.0 unless they made it CC0.
One worth keeping -- a photo from a jam, a flyer -- its sharer can put on
PickiPedia from the "→ PickiPedia" button under it, as a File: with a name
and a description, under that license, linking back to the message.

Every picture shared here is under a free license its sharer gave it (CC
BY-SA 4.0, or CC0 if they chose), so anyone here may put it on PickiPedia,
as that license allows: under it, unchanged -- only its sharer may change it
-- and its file page saying who shared it, as its author. Pictures an agent
brought in carry no such license from a person, and don't go.

It goes up from the uploader's own PickiPedia account, so the file's
history says who put it there. Pressing the button sends them to PickiPedia to let magenta upload
for them (OAuth 2, the "magenta uploads" consumer: settings.
PICKIPEDIA_UPLOAD_CLIENT_ID / _SECRET; unset, there is no button). Back here
(views_auth.wiki_upload_return), the upload is made with that permission and
the permission dropped: nothing kept here can act as anyone on the wiki. The
sign-in consumer (wiki_auth) stays identity-only.

Before anyone is sent anywhere, PickiPedia is asked, as anyone may ask,
whether it has the very same picture already -- then that File: is linked
to, never uploaded twice -- and whether the name is taken: then they choose
another, never overwriting. What happened is kept in the Mood (a
'wiki-upload' event), so the picture shows where it went, and a second press
finds it there.
"""

import hashlib
import re

from django.conf import settings
from django.core.cache import cache

SOURCE = 'wiki-upload'
EXTENSIONS = {'image/png': 'png', 'image/jpeg': 'jpg', 'image/gif': 'gif', 'image/webp': 'webp'}
LICENSE_TEXT = {
    'cc-by-sa-4.0': 'Released by its author under [https://creativecommons.org/licenses/by-sa/4.0/ CC BY-SA 4.0].',
    'cc0': 'Dedicated by its author to the public domain ([https://creativecommons.org/publicdomain/zero/1.0/ CC0]).',
}
STATE_COOKIE = 'wiki_upload'
PENDING_FOR = 900  # seconds to say yes on PickiPedia (signing in there first, perhaps)
_UNSAFE = re.compile(r'[#<>\[\]|{}/:\\\x00-\x1f]+')


class UploadError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def enabled():
    return bool(getattr(settings, 'PICKIPEDIA_UPLOAD_CLIENT_ID', '')
                and getattr(settings, 'PICKIPEDIA_UPLOAD_CLIENT_SECRET', ''))


def file_name(wanted, mime):
    """A File: name PickiPedia will take: no characters it forbids, the picture's own extension."""
    ext = EXTENSIONS.get(mime)
    if not ext:
        raise UploadError('only pictures go to PickiPedia from here')
    stem = _UNSAFE.sub(' ', (wanted or '').strip())
    stem = re.sub(r'\.(png|jpe?g|gif|webp)$', '', stem, flags=re.I)
    stem = re.sub(r'\s+', ' ', stem).strip(' .')[:180]
    if not stem:
        raise UploadError('give it a name')
    return f'{stem[:1].upper()}{stem[1:]}.{ext}'


def already(sha):
    """Where this picture went before, as {'file', 'page'}, or None."""
    from conversations.models import Message
    row = (Message.objects.filter(source_file=SOURCE, content__sha=sha).values_list('content', flat=True).first())
    return {'file': row['file'], 'page': row['page']} if row else None


def page_text(description, license, link, author=None):
    """A file page: what it is, who made it (whoever shared it in magenta), where, and its license."""
    from .wiki_auth import names
    credit = ''
    if author:
        wiki = names().get(author.lower())
        credit = f'Author: {"[[User:" + wiki + "|" + wiki + "]]" if wiki else author}, who shared it in magenta.\n\n'
    return (f'== Summary ==\n{description.strip() or "(no description)"}\n\n'
            f'{credit}Shared in magenta: {link}\n\n'
            f'== Licensing ==\n{LICENSE_TEXT.get(license, LICENSE_TEXT["cc-by-sa-4.0"])}\n\n'
            '[[Category:From magenta]]\n')


def file_page(name):
    from urllib.parse import quote
    from .mood_view import pickipedia_url
    return f"{pickipedia_url()}/wiki/File:{quote(name.replace(' ', '_'))}"


class Wiki:
    """PickiPedia's action API: as anyone, or with someone's OAuth token."""

    def __init__(self, http=None, token=None):
        import requests
        from .mood_view import pickipedia_url
        self.api = f'{pickipedia_url()}/api.php'
        self.http = http or requests.Session()
        self.http.headers['User-Agent'] = 'memory-lane (magenta; uploads)'
        if token:
            self.http.headers['Authorization'] = f'Bearer {token}'

    def call(self, post=False, files=None, **params):
        params['format'] = 'json'
        answer = (self.http.post(self.api, data=params, files=files, timeout=60) if post
                  else self.http.get(self.api, params=params, timeout=20))
        try:
            body = answer.json()
        except ValueError:
            raise UploadError(f'PickiPedia answered {getattr(answer, "status_code", "?")}', status=502)
        if 'error' in body:
            raise UploadError(f"PickiPedia: {body['error'].get('info') or body['error'].get('code')}", status=502)
        return body

    def holding(self, sha1):
        """The File: name PickiPedia keeps these very bytes under, or None."""
        found = self.call(action='query', list='allimages', aisha1=sha1, ailimit=1)
        images = found.get('query', {}).get('allimages') or []
        return images[0]['name'].replace('_', ' ') if images else None

    def taken(self, name):
        pages = self.call(action='query', titles=f'File:{name}').get('query', {}).get('pages', {})
        return any('missing' not in page and 'invalid' not in page for page in pages.values())

    def upload(self, name, data, mime, text, comment):
        token = self.call(action='query', meta='tokens')['query']['tokens']['csrftoken']
        return self.call(post=True, files={'file': (name, data, mime)}, action='upload', filename=name,
                         text=text, comment=comment, token=token)['upload']


def begin(media, wanted, description, license, entity, mood, message_id, redirect_uri, http=None):
    """The first half, before PickiPedia's yes.

    {'file', 'page', 'new': False} if the picture is there already; else
    {'go': where to ask, 'state': to know them by when they're back}.
    UploadError if it can't go at all.
    """
    from . import media as media_service
    from .wiki_auth import new_state
    if not enabled():
        raise UploadError("putting pictures on PickiPedia isn't set up here", status=503)
    name_here = getattr(entity, 'pk', entity)
    author = media.added_by_id
    if not author or not getattr(media.added_by, 'is_biological_human', False):
        raise UploadError("this picture wasn't shared by a person here, so it carries no license from one to "
                          "pass on", status=403)
    if license not in media_service.LICENSES:
        raise UploadError('CC BY-SA 4.0 or CC0')
    if author != name_here:
        license = media.license  # its sharer's to choose; anyone else passes it on as it is
    gone = already(media.sha256)
    if gone:
        return {**gone, 'new': False}
    name = file_name(wanted, media.mime)
    wiki = Wiki(http)
    there = wiki.holding(hashlib.sha1(bytes(media.data)).hexdigest())
    if there:
        _record(mood, name_here, file=there, page=file_page(there), sha=media.sha256,
                message=str(message_id or ''), author=author, already=True)
        return {'file': there, 'page': file_page(there), 'new': False}
    if wiki.taken(name):
        raise UploadError(f'PickiPedia already has a File:{name}: choose another name', status=409)
    if author == name_here:
        media_service.relicense(media, license, name_here)
    state = new_state()
    cache.set(f'wiki-upload:{state}', {'sha': media.sha256, 'name': name, 'description': description,
                                       'license': license, 'by': name_here, 'author': author, 'mood': mood.slug,
                                       'message': str(message_id or '')}, PENDING_FOR)
    from urllib.parse import urlencode
    from .mood_view import pickipedia_url
    query = urlencode({'response_type': 'code', 'client_id': settings.PICKIPEDIA_UPLOAD_CLIENT_ID,
                       'redirect_uri': redirect_uri, 'state': state})
    return {'go': f'{pickipedia_url()}/rest.php/oauth2/authorize?{query}', 'state': state}


def place(state):
    """Where the picture an upload is for was shown: the way back to it."""
    pending = cache.get(f'wiki-upload:{state}') if state else None
    return place_of(pending['mood'], pending['message']) if pending else '/moods/'


def place_of(slug, message):
    return f'/moods/{slug}/' + (f'#m-{message}' if message else '')


def finish(state, code, entity, redirect_uri, http=None):
    """The second half, with PickiPedia's yes: upload as them, then forget the permission.

    {'file', 'page', 'new', 'mood', 'message'}; UploadError if it can't be.
    """
    from conversations.models import Media, Mood
    from . import wiki_auth
    if not enabled():
        raise UploadError("putting pictures on PickiPedia isn't set up here", status=503)
    pending = cache.get(f'wiki-upload:{state}') if state else None
    cache.delete(f'wiki-upload:{state}')  # once only
    name_here = getattr(entity, 'pk', entity)
    if not pending or pending['by'] != name_here:
        raise UploadError('that went stale; press → PickiPedia again')
    try:
        token = wiki_auth.exchange(code, redirect_uri, settings.PICKIPEDIA_UPLOAD_CLIENT_ID,
                                   settings.PICKIPEDIA_UPLOAD_CLIENT_SECRET, http=http or _requests())
        profile = wiki_auth.profile_with(token, http=http or _requests())
    except wiki_auth.SignInRefused as e:
        raise UploadError(str(e), status=502)
    if profile.get('blocked'):
        raise UploadError('that PickiPedia account is blocked', status=403)
    if wiki_auth.local_name_for(profile['username']) != name_here:
        raise UploadError(f"you're signed in to PickiPedia as {profile['username']}, which isn't {name_here}'s account "
                          "here: sign in there as yourself (or ask an admin to map your account), then try again",
                          status=403)
    media = Media.objects.filter(sha256=pending['sha']).first()
    mood = Mood.by_slug(pending['mood'])
    if media is None or mood is None:
        raise UploadError('that picture or its Mood is gone', status=404)
    link = 'https://magenta.cryptograss.live' + place_of(mood.slug, pending['message'])
    wiki = Wiki(http, token=token)
    said = wiki.upload(pending['name'], bytes(media.data), media.mime,
                       page_text(pending['description'], media.license, link, author=pending.get('author')),
                       'From magenta')
    warnings = said.get('warnings') or {}
    name, new = pending['name'], True
    if said.get('result') == 'Warning' and warnings.get('duplicate'):
        name, new = warnings['duplicate'][0], False  # the very same picture is there already: that one
    elif said.get('result') == 'Warning' and ('exists' in warnings or 'page-exists' in warnings):
        raise UploadError(f'PickiPedia already has a File:{name}: choose another name', status=409)
    elif said.get('result') != 'Success':
        raise UploadError(f'PickiPedia said {said.get("result")}: {", ".join(warnings) or "no reason"}', status=502)
    else:
        name = said.get('filename') or name
    name = name.replace('_', ' ')  # as the wiki titles it; the API answers with underscores
    details = {'file': name, 'page': file_page(name), 'sha': media.sha256, 'message': pending['message'],
               'author': pending.get('author') or media.added_by_id}
    _record(mood, name_here, **details, **({} if new else {'already': True}))
    return {**details, 'new': new, 'back': place_of(mood.slug, pending['message'])}


def _requests():
    import requests
    return requests


def _record(mood, by, **details):
    """A line in the Mood (an event, mood_view.EVENT_SOURCES): who put which picture where."""
    import time
    import uuid
    from conversations.models import ConversationParticipant, Message
    system, _ = ConversationParticipant.objects.get_or_create(name='system', defaults={'participant_type': 'system'})
    return Message.objects.create(id=uuid.uuid4(), sender=system, mood=mood, source_file=SOURCE,
                                  content={'type': 'wiki-upload', 'by': by, **details}, timestamp=int(time.time() * 1000))
