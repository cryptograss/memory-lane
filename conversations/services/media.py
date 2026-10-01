"""Images in Motions: recognised by their bytes, stored once by hash.

Three ways in, one store (models.Media):
  - a person pastes, drops or attaches one in the composer (views_auth.api_media);
  - a person pastes one into a terminal session, and the transcript line
    carries it as a base64 image block;
  - a tool returns one -- a screenshot, an image the agent read.

The importer lifts image blocks out of each line before anything else
reads it (lift_images), storing the bytes and leaving markdown in their
place, ![image](/motions/media/<sha256>.<ext>). So the record's text stays
text, an image pasted twice is stored once, and every view already knows
how to show a link.

Only PNG, JPEG, GIF and WebP, decided by their first bytes. SVG is never
accepted: it can carry script.
"""

import base64
import binascii
import hashlib
import json
import re

MAX_BYTES = 8 * 1024 * 1024

_SIGNATURES = (
    (b'\x89PNG\r\n\x1a\n', 'image/png'),
    (b'\xff\xd8\xff', 'image/jpeg'),
    (b'GIF87a', 'image/gif'),
    (b'GIF89a', 'image/gif'),
)
MEDIA_PATH = re.compile(r'/motions/media/([0-9a-f]{64})\.(png|jpg|gif|webp)')


def sniff(data):
    """The image type these bytes really are, or None."""
    for signature, mime in _SIGNATURES:
        if data.startswith(signature):
            return mime
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return 'image/webp'
    return None


def store(data, added_by=None):
    """The Media for these bytes, stored if new; None if not an image we take."""
    from conversations.models import Media

    if not data or len(data) > MAX_BYTES:
        return None
    mime = sniff(data)
    if mime is None:
        return None
    sha = hashlib.sha256(data).hexdigest()
    media, _ = Media.objects.get_or_create(
        sha256=sha, defaults={'mime': mime, 'data': data, 'size': len(data), 'added_by': added_by})
    return media


def store_base64(text, added_by=None):
    try:
        data = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError, TypeError):
        return None
    return store(data, added_by)


def markdown(media, alt='image'):
    return f'![{alt}]({media.url})'


def _image_bytes(block):
    """base64 text of an image block, in Anthropic's shape or MCP's."""
    if not isinstance(block, dict) or block.get('type') != 'image':
        return None
    source = block.get('source')
    if isinstance(source, dict) and source.get('type') == 'base64':
        return source.get('data')
    if isinstance(block.get('data'), str):  # MCP: {"type": "image", "data": ..., "mimeType": ...}
        return block['data']
    return None


def lift_images(line, person=None):
    """A transcript line with every image block stored and replaced by markdown.

    `person` is credited with images in a message they typed (pasted into a
    terminal); images in tool results are credited to nobody.
    """
    if '"image"' not in line:
        return line
    try:
        event = json.loads(line)
    except (json.JSONDecodeError, TypeError):
        return line
    message = event.get('message') if isinstance(event, dict) else None
    typed = (isinstance(message, dict) and message.get('role') == 'user'
             and not any(isinstance(b, dict) and b.get('type') == 'tool_result'
                         for b in (message.get('content') or []) if isinstance(message.get('content'), list)))
    # A tool's images are tool output: kept only when tool output is
    # (settings.TOOL_RESULT_CONTENT_CHARS), like its text.
    from django.conf import settings
    keep_tool_images = settings.TOOL_RESULT_CONTENT_CHARS > 0
    found = False

    def walk(value):
        nonlocal found
        if isinstance(value, list):
            return [walk(v) for v in value]
        if isinstance(value, dict):
            data = _image_bytes(value)
            if data is not None:
                found = True
                keep = typed or keep_tool_images
                media = store_base64(data, person if typed else None) if keep else None
                return {'type': 'text', 'text': markdown(media) if media else '[image omitted]'}
            return {k: walk(v) for k, v in value.items()}
        return value

    event = walk(event)
    return json.dumps(event) if found else line


def urls_in(text):
    """Media URLs a text refers to, in order, without repeats."""
    seen, out = set(), []
    for match in MEDIA_PATH.finditer(text or ''):
        url = match.group(0)
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out
