"""Repositories that moved into the cryptograss org: a link to the old place
is the same repository (GitHub redirects it), so it's matched as the new one.

The org's webhook only hears the org's repositories, so memory-lane and
magenta moved there (Justin, 2026-10-09). Everything said before still links
the old names: jMyles/memory-lane/pull/138 is cryptograss/memory-lane's #138.
"""

MOVED = {'jmyles/memory-lane': 'cryptograss/memory-lane',
         'magent-cryptograss/magenta': 'cryptograss/magenta'}


def same(full_name):
    """'owner/repo', lowercased, as it is now: where a moved repository lives."""
    name = (full_name or '').lower()
    return MOVED.get(name, name)
