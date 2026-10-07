"""A picture from a Mood, onto PickiPedia: by choice, never by default.

Pictures sent into Moods stay in memory-lane (services/media.py); most are
screenshots, useful in the conversation and stale soon after. One worth
keeping -- a photo from a jam, a flyer -- goes up from the Mood's "→ PickiPedia"
button (views_auth.api_media_to_pickipedia), as a File: with a name, a
description and a license, credited to whoever sent it there and linking back
to the message it came from.

It goes up as a BotPassword (settings.WIKI_UPLOAD_USERNAME / _PASSWORD, from
the vault): unset, there is no button. PickiPedia is asked first whether the
name is free and whether it already has the very same picture -- a duplicate
is linked to, never uploaded twice; a taken name is refused, never
overwritten. What happened is kept in the Mood (a 'wiki-upload' event), so
the picture shows where it went, and a second press finds it there.
"""

import re
from urllib.parse import quote

from django.conf import settings

SOURCE = 'wiki-upload'
EXTENSIONS = {'image/png': 'png', 'image/jpeg': 'jpg', 'image/gif': 'gif', 'image/webp': 'webp'}
RIGHTS = {
    'cc-by-sa-4.0': 'Own work, released under [https://creativecommons.org/licenses/by-sa/4.0/ CC BY-SA 4.0].',
    'cc-by-4.0': 'Own work, released under [https://creativecommons.org/licenses/by/4.0/ CC BY 4.0].',
    'cc0': 'Own work, dedicated to the public domain ([https://creativecommons.org/publicdomain/zero/1.0/ CC0]).',
    'ask': 'Not sure of the rights: ask before reusing it.',
}
_UNSAFE = re.compile(r'[#<>\[\]|{}/:\\\x00-\x1f]+')


class UploadError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def enabled():
    return bool(getattr(settings, 'WIKI_UPLOAD_USERNAME', '') and getattr(settings, 'WIKI_UPLOAD_PASSWORD', ''))


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


def page_text(description, rights, by, link):
    from .wiki_auth import names
    wiki = names().get(by)
    who = f'[[User:{wiki}|{wiki}]]' if wiki else by
    return (f'== Summary ==\n{description.strip() or "(no description)"}\n\n'
            f'Shared by {who} in magenta: {link}\n\n'
            f'== Licensing ==\n{RIGHTS.get(rights, RIGHTS["ask"])}\n\n'
            '[[Category:From magenta]]\n')


class Wiki:
    """PickiPedia's action API, signed in with the upload BotPassword."""

    def __init__(self, http=None):
        import requests
        from .mood_view import pickipedia_url
        self.api = f'{pickipedia_url()}/api.php'
        self.http = http or requests.Session()
        self.http.headers['User-Agent'] = 'memory-lane (magenta; uploads)'

    def call(self, post=False, files=None, **params):
        params['format'] = 'json'
        answer = (self.http.post(self.api, data=params, files=files, timeout=60) if post
                  else self.http.get(self.api, params=params, timeout=20))
        body = answer.json()
        if 'error' in body:
            raise UploadError(f"PickiPedia: {body['error'].get('info') or body['error'].get('code')}", status=502)
        return body

    def sign_in(self):
        token = self.call(action='query', meta='tokens', type='login')['query']['tokens']['logintoken']
        result = self.call(post=True, action='login', lgname=settings.WIKI_UPLOAD_USERNAME,
                           lgpassword=settings.WIKI_UPLOAD_PASSWORD, lgtoken=token)['login']
        if result.get('result') != 'Success':
            raise UploadError("PickiPedia didn't take the upload bot's sign-in", status=502)

    def upload(self, name, data, mime, text, comment):
        token = self.call(action='query', meta='tokens')['query']['tokens']['csrftoken']
        return self.call(post=True, files={'file': (name, data, mime)}, action='upload', filename=name,
                         text=text, comment=comment, token=token)['upload']


def to_pickipedia(media, wanted, description, rights, by, mood, message_id, http=None):
    """Put a stored picture on PickiPedia. {'file', 'page', 'new'}; UploadError if it can't be."""
    from .mood_view import pickipedia_url
    if not enabled():
        raise UploadError("uploading to PickiPedia isn't set up here", status=503)
    gone = already(media.sha256)
    if gone:
        return {**gone, 'new': False}
    name = file_name(wanted, media.mime)
    link = f'https://magenta.cryptograss.live/moods/{mood.slug}/#m-{message_id}' if message_id else f'#{mood.slug}'
    wiki = Wiki(http)
    wiki.sign_in()
    said = wiki.upload(name, bytes(media.data), media.mime, page_text(description, rights, by, link),
                       f'From magenta, shared by {by}')
    warnings = said.get('warnings') or {}
    if said.get('result') == 'Warning' and warnings.get('duplicate'):
        name = warnings['duplicate'][0]  # the very same picture is there already: that one
    elif said.get('result') == 'Warning' and ('exists' in warnings or 'page-exists' in warnings):
        raise UploadError(f'PickiPedia already has a File:{name}: choose another name', status=409)
    elif said.get('result') != 'Success':
        raise UploadError(f'PickiPedia said {said.get("result")}: {", ".join(warnings) or "no reason"}', status=502)
    else:
        name = said.get('filename') or name
    name = name.replace('_', ' ')  # as the wiki titles it; the API answers with underscores
    page = f"{pickipedia_url()}/wiki/File:{quote(name.replace(' ', '_'))}"
    _record(mood, by, file=name, page=page, sha=media.sha256, message=str(message_id or ''))
    return {'file': name, 'page': page, 'new': True}


def _record(mood, by, **details):
    """A line in the Mood (an event, mood_view.EVENT_SOURCES): who put which picture where."""
    import time
    import uuid
    from conversations.models import ConversationParticipant, Message
    system, _ = ConversationParticipant.objects.get_or_create(name='system', defaults={'participant_type': 'system'})
    return Message.objects.create(id=uuid.uuid4(), sender=system, mood=mood, source_file=SOURCE,
                                  content={'type': 'wiki-upload', 'by': by, **details}, timestamp=int(time.time() * 1000))
