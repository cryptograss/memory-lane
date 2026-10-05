"""Saved Yarn clips: the team's library of quotes, kept in the record itself.

    /yarn save <name> <yarn link>   saves a clip under a name
    /yarn <name>                    posts the saved clip
    /yarn forget <name>             forgets a name
    /yarn                           lists the names (to whoever asked; nothing posted)

Saving and forgetting are posts like any other (shown as one line, "🎬 saved
tony", without the clip), so the team sees new names as they arrive, and the
library is read back from those posts: no table of its own, and history and
who-saved-what come with it. '/yarn tony' posts the clip itself, as
'/yarn tony <link>' -- shown as just the clip; the name stays in the record --
so saving the name again later never changes what was already said.

A name is first come, first served: only whoever saved it can save over it or
forget it. That rule is applied when the library is read back, so it holds
even for a post that never went through command().
"""

import re

from django.core.cache import cache

from .mood_view import yarn_clip

NAME = re.compile(r'^[a-z0-9][a-z0-9_-]{0,34}$')
RESERVED = {'save', 'forget', 'list'}
SAVED = re.compile(r'^/yarn save (\S+) (\S+)$')
FORGOTTEN = re.compile(r'^/yarn forget (\S+)$')
CACHE_KEY = 'yarn-library'
USAGE = '/yarn <name> posts a saved clip; /yarn save <name> <yarn link>; /yarn forget <name>; /yarn lists them'


class Refused(ValueError):
    pass


def clip_link(clip):
    return f'https://www.yarn.co/yarn-clip/{clip}'


def library():
    """name -> {'clip', 'by', 'at'}: each name's latest save, unless since forgotten."""
    found = cache.get(CACHE_KEY)
    if found is not None:
        return found
    from django.db.models import TextField
    from django.db.models.functions import Cast

    from conversations.models import Message
    from conversations.views_auth import WEB_SOURCE

    posts = (Message.objects.filter(source_file=WEB_SOURCE)
             .annotate(as_text=Cast('content', TextField())).filter(as_text__startswith='"/yarn ')
             .order_by('created_at').values_list('content', 'sender_id', 'created_at'))
    found = {}
    for content, by, at in posts:
        if not isinstance(content, str):
            continue
        saved, forgotten = SAVED.match(content), FORGOTTEN.match(content)
        if saved:
            name, clip = saved[1], yarn_clip(saved[2])
            if clip and NAME.match(name) and found.get(name, {}).get('by', by) == by:
                found[name] = {'clip': clip, 'by': by, 'at': at.isoformat()}
        elif forgotten and found.get(forgotten[1], {}).get('by') == by:
            del found[forgotten[1]]
    cache.set(CACHE_KEY, found, 300)
    return found


def forget_cached():
    cache.delete(CACHE_KEY)


def command(text, who):
    """For a post: (what to post instead -- None to post nothing --, a note for
    whoever sent it). Text that isn't a /yarn command comes back as it is.
    Raises Refused, with what to do instead."""
    words = text.strip().split()
    if not words or words[0].lower() != '/yarn':
        return text, ''
    args = words[1:]
    found = library()

    if not args or args == ['list']:
        names = sorted(found)
        return None, ('Saved clips: ' + ', '.join(names)) if names else \
            'No clips saved yet: /yarn save <name> <yarn link>'

    def owner_check(name):
        owner = found.get(name, {}).get('by')
        if owner and owner != who:
            raise Refused(f'"{name}" is {owner}\'s clip; pick another name')

    if args[0].lower() == 'save':
        if len(args) != 3:
            raise Refused('/yarn save <name> <yarn link>')
        name = args[1].lower()
        if not NAME.match(name) or name in RESERVED:
            raise Refused('a name: letters, digits, - and _, up to 35, and not save, forget or list')
        clip = yarn_clip(args[2].rstrip('.,;:!?)'))
        if not clip:
            raise Refused("that isn't a Yarn clip link (https://www.yarn.co/yarn-clip/…)")
        owner_check(name)
        return f'/yarn save {name} {clip_link(clip)}', f'Saved "{name}": /yarn {name} posts it'

    if args[0].lower() == 'forget':
        if len(args) != 2:
            raise Refused('/yarn forget <name>')
        name = args[1].lower()
        if name not in found:
            raise Refused(f'No clip called "{name}"')
        owner_check(name)
        return f'/yarn forget {name}', f'Forgot "{name}"'

    if len(args) == 1:
        name = args[0].lower()
        if name not in found:
            raise Refused(f'No clip called "{name}": save one with /yarn save {name} <yarn link>')
        return f'/yarn {name} {clip_link(found[name]["clip"])}', ''

    raise Refused(USAGE)
