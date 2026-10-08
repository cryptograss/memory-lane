"""Signing in with PickiPedia, and the names people go by there.

**The wiki tier.** PickiPedia's accounts are by invitation, so a wiki
account is a known bluegrasser. Signing in with one (OAuth 2, the wiki's
OAuth extension) enrols a device of tier 'wiki': it can chat and mention
people, and their mentions notify as anyone's do. It can't do what makes
the machines act -- an @agent from it is just text, and starting, archiving,
renaming or pinning a Mood, stopping an agent or changing a setting all need
the SSH-key tier (mood_auth). Nothing is shared with the wiki but a
yes-this-is-them: the grant asked for is identity only.

**Names.** Each person on hunter can carry their PickiPedia name in its
inventory (`pickipedia:`); maybelle writes those to a file, one
"<name here> <PickiPedia name>" per line (settings.MOOD_WIKI_NAMES). The
thread shows the wiki name, links it to their user page, and @-mentions
work with either. A wiki sign-in by a mapped account is that person;
anyone else gets a name of their own here, from their wiki name, unless it
would be someone else's.
"""

import os
import re
import secrets
from urllib.parse import urlencode

import requests
from django.conf import settings

from conversations.services.mood_view import pickipedia_url

STATE_COOKIE = 'wiki_signin'
STATE_AGE = 600  # seconds to come back from the wiki


class SignInRefused(Exception):
    """Why this wiki account can't sign in here, to tell them."""


def enabled():
    return bool(getattr(settings, 'PICKIPEDIA_OAUTH_CLIENT_ID', '')
                and getattr(settings, 'PICKIPEDIA_OAUTH_CLIENT_SECRET', ''))


# --- names ----------------------------------------------------------------------

_read = {}  # path -> (mtime, names): the file is asked for per message shown


def names():
    """{name here: PickiPedia name}, from hunter's inventory (empty if not set up)."""
    path = getattr(settings, 'MOOD_WIKI_NAMES', '') or os.environ.get('MOOD_WIKI_NAMES', '')
    if not path or not os.path.exists(path):
        return {}
    mtime = os.path.getmtime(path)
    if path in _read and _read[path][0] == mtime:
        return dict(_read[path][1])
    found = {}
    if True:
        with open(path) as f:
            for line in f:
                parts = line.split(None, 1)
                if len(parts) == 2 and not line.startswith('#'):
                    found[parts[0].strip().lower()] = parts[1].strip()
    _read[path] = (mtime, found)
    return dict(found)


def aliases():
    """{lowercased PickiPedia name: name here}: so @JMyles is @justin."""
    return {wiki.lower(): here for here, wiki in names().items()}


def user_page(wiki_name):
    return f"{pickipedia_url()}/wiki/User:{wiki_name.replace(' ', '_')}"


# --- the OAuth 2 dance --------------------------------------------------------------

def authorize_url(redirect_uri, state):
    query = urlencode({'response_type': 'code', 'client_id': settings.PICKIPEDIA_OAUTH_CLIENT_ID,
                       'redirect_uri': redirect_uri, 'state': state})
    return f'{pickipedia_url()}/rest.php/oauth2/authorize?{query}'


def new_state():
    return secrets.token_urlsafe(24)


def profile_for(code, redirect_uri, http=requests):
    """The wiki's word on who signed in: {'username', 'blocked', ...}. SignInRefused if not."""
    token = exchange(code, redirect_uri, settings.PICKIPEDIA_OAUTH_CLIENT_ID,
                     settings.PICKIPEDIA_OAUTH_CLIENT_SECRET, http=http)
    profile = profile_with(token, http=http)
    if profile.get('blocked'):
        raise SignInRefused('that PickiPedia account is blocked')
    return profile


def exchange(code, redirect_uri, client_id, client_secret, http=requests):
    """The access token a consumer's code is good for (sign-in's, or uploads': wiki_upload)."""
    token = http.post(f'{pickipedia_url()}/rest.php/oauth2/access_token', timeout=15, data={
        'grant_type': 'authorization_code', 'code': code, 'redirect_uri': redirect_uri,
        'client_id': client_id, 'client_secret': client_secret})
    if token.status_code != 200 or 'access_token' not in _json(token):
        raise SignInRefused(f'PickiPedia did not confirm the sign-in ({token.status_code})')
    return _json(token)['access_token']


def profile_with(access_token, http=requests):
    """Who an access token is: {'username', 'blocked', ...}."""
    who = http.get(f'{pickipedia_url()}/rest.php/oauth2/resource/profile', timeout=15,
                   headers={'Authorization': f'Bearer {access_token}'})
    profile = _json(who)
    if who.status_code != 200 or not profile.get('username'):
        raise SignInRefused(f'PickiPedia would not say who signed in ({who.status_code})')
    return profile


def _json(response):
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


# --- who that is here -------------------------------------------------------------------

def local_name_for(wiki_name):
    """The name here for a PickiPedia account: theirs if mapped, else one from
    the wiki name (lowercase, spaces as underscores, only name characters)."""
    mapped = aliases().get(wiki_name.lower())
    if mapped:
        return mapped
    return re.sub(r'[^a-z0-9_.-]', '', wiki_name.lower().replace(' ', '_'))[:60]


def entity_for(wiki_name):
    """The person a PickiPedia account signs in as; SignInRefused if it can't be anyone."""
    from conversations.models import ThinkingEntity
    from conversations.services import mood_auth
    name = local_name_for(wiki_name)
    if not name or not name[0].isalpha():
        raise SignInRefused(f'"{wiki_name}" makes no name usable here; ask an admin')
    entity = ThinkingEntity.objects.filter(name=name).first()
    mapped = aliases().get(wiki_name.lower()) == name
    if entity is not None and not mapped:
        # Only an unmapped wiki person's own name may be reused: never an
        # agent's, never someone with a key, never a name mapped to another account.
        if (not entity.is_biological_human or mood_auth.public_key_of(name)
                or name in names()):
            raise SignInRefused(f'the name "{name}" here belongs to someone else; ask an admin to map your account')
    if entity is not None and not entity.is_biological_human:
        raise SignInRefused('agents do not sign in')
    if entity is None:
        entity = ThinkingEntity.objects.create(name=name, is_biological_human=True)
    return entity
