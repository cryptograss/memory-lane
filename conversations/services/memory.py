"""
Memory service - handles querying conversation history.

All methods are synchronous Django ORM queries.
Called via sync_to_async from MCP tools.
"""

from conversations.models import Message, Era, ContextHeap
from django.db.models import Count, Max, Min, Q
import json
import random


# Opening prompts that say nothing about what a thread is *about*. Skipped
# when picking a title hint, so a background session started with
# `claude --bg 'reawaken magent'` is titled by its first real request.
_NON_TOPICAL_OPENERS = ('reawaken magent', 'reawaken')


def _text_of(content):
    """Best-effort plain text from a Message.content value.

    Content arrives in several shapes depending on importer and era: a plain
    string, a JSON-encoded string of blocks, a list of blocks, or a dict.
    Only human-readable text blocks are kept; tool calls and results are not.
    """
    if isinstance(content, str):
        stripped = content.strip()
        if stripped[:1] in ('[', '{'):
            try:
                return _text_of(json.loads(stripped))
            except ValueError:
                pass
        return content
    if isinstance(content, dict):
        return content.get('text', '') if content.get('type', 'text') == 'text' else ''
    if isinstance(content, list):
        return ' '.join(t for t in (_text_of(block) for block in content) if t)
    return ''


class MemoryService:
    """Service for querying conversation memory"""

    @staticmethod
    def get_latest_continuation():
        """Get the most recent continuation message"""
        return Message.objects.filter(
            is_continuation_message=True
        ).order_by('-created_at').first()

    @staticmethod
    def get_message_by_id(message_id):
        """Get a specific message by its UUID"""
        try:
            return Message.objects.get(id=message_id)
        except Message.DoesNotExist:
            return None

    @staticmethod
    def get_messages_before(reference_id=None, reference_timestamp=None, limit=300):
        """Get N messages before a reference point"""
        if reference_id:
            ref_msg = Message.objects.get(id=reference_id)
            messages = Message.objects.filter(
                created_at__lt=ref_msg.created_at
            ).order_by('-created_at')[:limit]
        elif reference_timestamp:
            messages = Message.objects.filter(
                created_at__lt=reference_timestamp
            ).order_by('-created_at')[:limit]
        else:
            messages = Message.objects.order_by('-created_at')[:limit]

        return list(messages)

    @staticmethod
    def get_era_summary(era_name="Compacting Meta-Conversation (Era 1)"):
        """Get messages from a specific era"""
        try:
            era = Era.objects.get(name=era_name)
            heaps = ContextHeap.objects.filter(era=era)
            messages = Message.objects.filter(
                context_heap__in=heaps
            ).order_by('message_number')[:100]
            return {
                'era': era,
                'messages': list(messages)
            }
        except Era.DoesNotExist:
            return None

    @staticmethod
    def get_context_heap(heap_id):
        """Get all messages from a specific context heap"""
        try:
            heap = ContextHeap.objects.get(id=heap_id)
            messages = Message.objects.filter(
                context_heap=heap
            ).order_by('message_number')
            return {
                'heap': heap,
                'messages': list(messages)
            }
        except ContextHeap.DoesNotExist:
            return None

    @staticmethod
    def search_messages(query, limit=50):
        """Full-text search for messages"""
        # PostgreSQL full-text search
        from django.contrib.postgres.search import SearchQuery, SearchRank, SearchVector

        search_vector = SearchVector('content')
        search_query = SearchQuery(query)

        # Tool output is excluded: it was never stored until #19, and long
        # output would outrank prose (rank isn't normalised for length).
        messages = Message.objects.exclude(sender_id='tool-result').annotate(
            rank=SearchRank(search_vector, search_query)
        ).filter(
            rank__gt=0
        ).order_by('-rank', '-created_at')[:limit]

        return list(messages)

    @staticmethod
    def get_recent_work(limit=50, session_id=None):
        """Get most recent messages, optionally scoped to one thread"""
        messages = Message.objects.all()
        if session_id:
            messages = messages.filter(session_id=session_id)
        return list(messages.order_by('-created_at')[:limit])

    @staticmethod
    def list_threads(limit=20, since=None):
        """Distinct threads, most recently active first.

        Without this, recall is a flat chronological column: concurrent
        threads interleave and read as one confused stream.

        A thread here is a session_id. That is a runtime instance, not a
        conversation -- one long session spans several context heaps, and a
        resumed conversation gets a new session_id -- so this is the unit
        available today, not the right one. The intended key is a mood ID
        (see magenta#41). Keep callers keyed on the returned `thread_id` so
        the grouping can change underneath them.
        """
        messages = Message.objects.exclude(session_id__isnull=True)
        if since:
            messages = messages.filter(created_at__gte=since)

        rows = (
            messages.values('session_id')
            .annotate(
                message_count=Count('id'),
                first_at=Min('created_at'),
                last_at=Max('created_at'),
            )
            .order_by('-last_at')[:limit]
        )

        threads = []
        for row in rows:
            in_thread = Message.objects.filter(session_id=row['session_id'])

            # cwd and branch can change mid-session; report where it is now.
            latest = in_thread.exclude(cwd__isnull=True).order_by('-created_at').first()

            # Thinking entities only: tools and system components also send
            # messages, but "who was in this thread" means humans and agents.
            participants = sorted(set(
                in_thread.filter(sender__thinkingentity__isnull=False)
                .values_list('sender_id', flat=True)
            ))

            title_hint = ''
            human_messages = in_thread.filter(
                sender__thinkingentity__is_biological_human=True
            ).order_by('created_at')
            for msg in human_messages[:25]:
                text = ' '.join(_text_of(msg.content).split())
                if not text or text.startswith('<'):
                    continue
                if text.lower().rstrip('.!') in _NON_TOPICAL_OPENERS:
                    continue
                title_hint = text
                break

            threads.append({
                'thread_id': str(row['session_id']),
                'message_count': row['message_count'],
                'first_at': row['first_at'],
                'last_at': row['last_at'],
                'cwd': latest.cwd if latest else None,
                'git_branch': latest.git_branch if latest else None,
                'participants': participants,
                'title_hint': title_hint,
            })

        return threads

    @staticmethod
    def get_random_messages_with_context(count=4, context_messages=4):
        """Get random messages with following context"""
        total = Message.objects.count()
        if total == 0:
            return []

        # Get random message IDs
        all_ids = list(Message.objects.values_list('id', flat=True))
        random_ids = random.sample(all_ids, min(count, len(all_ids)))

        results = []
        for msg_id in random_ids:
            random_msg = Message.objects.get(id=msg_id)

            # Get this message plus N following messages
            following = list(Message.objects.filter(
                created_at__gte=random_msg.created_at
            ).order_by('created_at')[:(context_messages + 1)])

            results.append({
                'starting_message': random_msg,
                'context': following
            })

        return results

    @staticmethod
    def get_recent_messages_by_chars(max_chars=10000, scan=500):
        """Recent messages, newest first, up to a character budget.

        Tool output is left out, and a single message too big for what's
        left of the budget is skipped rather than ending the walk -- one
        long paste used to make this return nothing at all. At most `scan`
        messages are looked at.
        """
        messages = []
        total_chars = 0

        for msg in Message.objects.exclude(sender_id='tool-result').order_by('-created_at')[:scan]:
            content_str = str(msg.content)
            if total_chars + len(content_str) > max_chars:
                continue
            messages.append(msg)
            total_chars += len(content_str)
            if total_chars >= max_chars:
                break

        return messages, total_chars

    @staticmethod
    def get_awakening_reflection():
        """Get most recent 'reawaken and breathe' message"""
        # TODO: Query by topic once topic tagging is working
        # For now, search for messages from magent containing "reawaken" or "breathe"
        return Message.objects.filter(
            Q(sender_id='magent') &
            (Q(content__icontains='reawaken') | Q(content__icontains='breathe'))
        ).order_by('-created_at').first()


# --- Finding a message, and what was said around it ---------------------------

MIN_PREFIX = 6  # hex digits of an id before a prefix is accepted


def resolve_message(ref):
    """(message, candidates, problem) for a full id, an id prefix, or a #m-<id> link.

    A prefix that matches exactly one message resolves to it. One that matches
    several returns them as candidates (up to ten) so the caller can choose.
    """
    from django.db.models import TextField
    from django.db.models.functions import Cast

    ref = str(ref or '').strip()
    if '#m-' in ref:
        ref = ref.split('#m-', 1)[1]
    ref = ref.strip().lower()
    if not ref:
        return None, [], 'Give a message id, the first few characters of one, or a #m-<id> link.'
    if len(ref) == 36:
        message = Message.objects.filter(id=ref).first() if _looks_like_uuid(ref) else None
        return (message, [], None) if message else (None, [], f"No message '{ref}'.")
    if len(ref.replace('-', '')) < MIN_PREFIX or any(c not in '0123456789abcdef-' for c in ref):
        return None, [], f"'{ref}' is too short or not hex; give at least {MIN_PREFIX} characters of an id."
    found = list(Message.objects.annotate(id_text=Cast('id', output_field=TextField()))
                 .filter(id_text__startswith=ref).order_by('created_at')[:11])
    if not found:
        return None, [], f"No message id starts with '{ref}'."
    if len(found) > 1:
        return None, found[:10], f"'{ref}' starts {'more than ten' if len(found) > 10 else len(found)} message ids."
    return found[0], [], None


def _looks_like_uuid(text):
    import uuid
    try:
        uuid.UUID(text)
        return True
    except ValueError:
        return False


def kind_of(message):
    for kind, attr in (('thought', 'thought'), ('tool_use', 'tooluse'), ('tool_result', 'toolresult')):
        try:
            getattr(message, attr)
            return kind
        except Exception:  # the reverse one-to-one raises DoesNotExist when absent
            continue
    return 'message'


def readable(message, kind=None):
    """What a message says, as plain text, including what _text_of leaves out:
    thinking, and the input of a tool call (the command that was run)."""
    kind = kind or kind_of(message)
    content = message.content
    if kind == 'thought':
        blocks = content if isinstance(content, list) else [content]
        text = ' '.join(b.get('thinking', '') for b in blocks if isinstance(b, dict)).strip()
        return text or '(the thinking itself was not recorded)'
    if kind == 'tool_use':
        name = getattr(getattr(message, 'tooluse', None), 'tool_name', 'tool')
        if isinstance(content, dict) and set(content) <= {'command', 'description', 'timeout', 'run_in_background'} \
                and 'command' in content:
            return f"{name}: {content['command']}"
        return f"{name}: {json.dumps(content, ensure_ascii=False)}"
    text = _text_of(content)
    if not text.strip():
        if kind == 'tool_result':
            return '(output not kept)' if not content else json.dumps(content, ensure_ascii=False)
        return json.dumps(content, ensure_ascii=False)
    return text


def stream_of(message):
    """The messages that ran alongside this one, in order: its session's, or,
    for the many older messages that have no session, its context heap's."""
    from django.db.models import F
    if message.session_id:
        stream, what = Message.objects.filter(session_id=message.session_id), f'session {message.session_id}'
    elif message.context_heap_id:
        stream, what = Message.objects.filter(context_heap_id=message.context_heap_id), \
            f'context heap {message.context_heap_id} (this message has no session)'
    else:
        return Message.objects.filter(id=message.id), 'this message alone (no session, no heap)'
    return stream.order_by(F('timestamp').asc(nulls_last=True), F('message_number').asc(nulls_last=True),
                           'created_at', 'id'), what


def message_context(ref, before=10, after=10):
    """(text, problem): a message with what came before and after it in its stream."""
    message, candidates, problem = resolve_message(ref)
    if message is None:
        lines = [problem]
        for c in candidates:
            lines.append(f"  {c.id}  {c.created_at.isoformat()[:16]}Z  [{c.sender_id}]  {readable(c)[:100]!r}")
        return '\n'.join(lines)

    before = max(0, min(int(before if before is not None else 10), 100))
    after = max(0, min(int(after if after is not None else 10), 100))
    stream, what = stream_of(message)
    ids = list(stream.values_list('id', flat=True))
    at = ids.index(message.id)
    window = ids[max(0, at - before):at + after + 1]
    rows = {m.id: m for m in Message.objects.filter(id__in=window)
            .select_related('thought', 'tooluse', 'toolresult')}

    head = [f"Message {message.id}",
            f"  in {what}: {at + 1} of {len(ids)}" + (f" · Mood: {message.mood_slug}" if message.mood_id else ''),
            f"  showing {at - max(0, at - before)} before and {len(window) - 1 - (at - max(0, at - before))} after\n"]
    lines = []
    for mid in window:
        m = rows[mid]
        kind = kind_of(m)
        text = readable(m, kind)
        limit = 6000 if mid == message.id else 1200
        if len(text) > limit:
            text = text[:limit] + ' […]'
        when = m.created_at.isoformat()[:19] + 'Z'
        marker = '>>>' if mid == message.id else '   '
        side = ' (subagent)' if m.is_sidechain else ''
        lines.append(f"{marker} [{m.sender_id} · {kind}{side} · {when} · {m.id}]\n{text}\n")
    return '\n'.join(head + lines)


def search(query, limit=20, exact=False, sender=None):
    """Matching messages, newest or best first, each with its id and the text around the match.

    exact=True finds the phrase as written, case-insensitively, anywhere in a
    message's stored content, tool calls included. Otherwise it is Postgres
    full-text search (words, stemmed), ranked.
    """
    from django.contrib.postgres.search import SearchQuery, SearchRank, SearchVector
    from django.db.models import TextField
    from django.db.models.functions import Cast

    query = (query or '').strip()
    if not query:
        return []
    limit = max(1, min(int(limit or 20), 100))
    messages = Message.objects.exclude(sender_id='tool-result')
    if sender:
        messages = messages.filter(sender_id=sender)
    if exact:
        # The content is stored as JSON, where a quote or a newline inside a
        # phrase is escaped; look for the phrase both as typed and as escaped.
        escaped = json.dumps(query, ensure_ascii=False)[1:-1]
        found = (messages.annotate(content_text=Cast('content', output_field=TextField()))
                 .filter(Q(content_text__icontains=query) | Q(content_text__icontains=escaped))
                 .order_by('-created_at'))
    else:
        found = (messages.annotate(rank=SearchRank(SearchVector('content'), SearchQuery(query)))
                 .filter(rank__gt=0).order_by('-rank', '-created_at'))
    found = list(found.select_related('thought', 'tooluse', 'toolresult')[:limit])

    terms = [query] if exact else [t for t in query.split() if len(t) > 2] or [query]
    hits = []
    for m in found:
        kind = kind_of(m)
        hits.append({'message': m, 'kind': kind, 'snippet': snippet(readable(m, kind), terms)})
    return hits


def snippet(text, terms, width=140):
    """The text around the first place any term appears; the start if none does."""
    text = ' '.join(text.split())
    lowered = text.lower()
    places = [lowered.find(t.lower()) for t in terms]
    places = [p for p in places if p >= 0]
    if not places:
        return text[:2 * width] + (' …' if len(text) > 2 * width else '')
    at = min(places)
    start, end = max(0, at - width), min(len(text), at + width)
    return ('… ' if start else '') + text[start:end] + (' …' if end < len(text) else '')
