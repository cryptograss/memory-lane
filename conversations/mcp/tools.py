"""
MCP tool handlers.

Each tool handler:
1. Calls appropriate service methods via sync_to_async
2. Can send progress notifications during long operations
3. Returns MCP-formatted results
"""

import datetime

import mcp.types as types
from asgiref.sync import sync_to_async
from django.db import close_old_connections
from conversations.services import MemoryService, BootstrapService


def with_fresh_connection(func):
    """Wrapper to ensure fresh database connection for each call"""
    def wrapper(*args, **kwargs):
        close_old_connections()
        try:
            return func(*args, **kwargs)
        finally:
            close_old_connections()
    return wrapper


async def handle_bootstrap_memory():
    """Complete memory bootstrap for cold starts"""
    # Call service with fresh connection
    bootstrap_data = await sync_to_async(with_fresh_connection(BootstrapService.bootstrap_memory))()

    # Format as text
    result_text = BootstrapService.format_bootstrap_text(bootstrap_data)

    return [types.TextContent(type="text", text=result_text)]


async def handle_get_latest_continuation():
    """Get most recent continuation message"""
    continuation = await sync_to_async(with_fresh_connection(MemoryService.get_latest_continuation))()

    if not continuation:
        return [types.TextContent(type="text", text="No continuation messages found")]

    return [types.TextContent(type="text", text=str(continuation.content))]


async def handle_get_message_by_id(arguments):
    """Get a specific message by UUID"""
    message_id = arguments.get("message_id")

    if not message_id:
        return [types.TextContent(type="text", text="Error: message_id is required")]

    from conversations.services.memory import resolve_message
    message, candidates, problem = await sync_to_async(with_fresh_connection(lambda: resolve_message(message_id)))()

    if not message:
        text = problem + ''.join(f"\n  {c.id}  {c.created_at.isoformat()[:16]}Z  [{c.sender_id}]" for c in candidates)
        return [types.TextContent(type="text", text=text)]

    lines = [
        f"Message ID: {message.id}",
        f"Sender: {message.sender_id}",
        f"Created: {message.created_at.isoformat()}",
        f"Timestamp: {message.timestamp}",
        f"Context Heap: {message.context_heap_id}",
        f"Message Number: {message.message_number}",
        f"\nContent:\n{str(message.content)}"
    ]

    return [types.TextContent(type="text", text='\n'.join(lines))]


async def handle_get_messages_before(arguments):
    """Get messages before a reference point"""
    reference_id = arguments.get("reference_id") if arguments else None
    reference_timestamp = arguments.get("reference_timestamp") if arguments else None
    limit = arguments.get("limit", 300) if arguments else 300

    messages = await sync_to_async(with_fresh_connection(
        lambda: MemoryService.get_messages_before(
            reference_id=reference_id,
            reference_timestamp=reference_timestamp,
            limit=limit
        )
    ))()

    # Format results
    lines = [f"Retrieved {len(messages)} messages:\n"]
    for msg in messages[:10]:  # Show first 10
        lines.append(f"[{msg.sender_id}] {msg.created_at.isoformat()}")
        lines.append(f"{str(msg.content)[:200]}...\n")

    return [types.TextContent(type="text", text='\n'.join(lines))]


async def handle_get_era_summary(arguments):
    """Get messages from Era 1"""
    era_name = arguments.get("era_name", "Compacting Meta-Conversation (Era 1)") if arguments else "Compacting Meta-Conversation (Era 1)"

    era_data = await sync_to_async(with_fresh_connection(lambda: MemoryService.get_era_summary(era_name)))()

    if not era_data:
        return [types.TextContent(type="text", text=f"Era '{era_name}' not found")]

    era = era_data['era']
    messages = era_data['messages']

    lines = [f"Era: {era.name}", f"Messages: {len(messages)}\n"]
    for msg in messages[:20]:
        lines.append(f"[{msg.sender_id}] {str(msg.content)[:150]}...\n")

    return [types.TextContent(type="text", text='\n'.join(lines))]


async def handle_get_context_heap(arguments):
    """Get all messages from a context heap"""
    heap_id = arguments.get("heap_id")

    heap_data = await sync_to_async(with_fresh_connection(lambda: MemoryService.get_context_heap(heap_id)))()

    if not heap_data:
        return [types.TextContent(type="text", text=f"Context heap '{heap_id}' not found")]

    heap = heap_data['heap']
    messages = heap_data['messages']

    lines = [f"Context Heap: {heap.id}", f"Messages: {len(messages)}\n"]
    for msg in messages[:20]:
        lines.append(f"[{msg.sender_id}] {str(msg.content)[:150]}...\n")

    return [types.TextContent(type="text", text='\n'.join(lines))]


async def handle_search_messages(arguments):
    """Search for messages: each hit with its id, kind, Mood and the text around the match"""
    from conversations.services.memory import search
    arguments = arguments or {}
    query = arguments.get("query", "")
    exact = bool(arguments.get("exact", False))
    sender = arguments.get("sender") or None
    limit = arguments.get("limit", 20)

    hits = await sync_to_async(with_fresh_connection(lambda: search(query, limit=limit, exact=exact, sender=sender)))()

    how = 'exact phrase' if exact else 'words'
    lines = [f"{len(hits)} messages matching '{query}' ({how}"
             + (f", from {sender}" if sender else '') + ("; newest first" if exact else "; best first") + "):\n"]
    for i, hit in enumerate(hits, 1):
        m = hit['message']
        where = f"Mood {m.mood_slug}" if m.mood_id else (f"session {str(m.session_id)[:8]}" if m.session_id else "no session")
        lines.append(f"[{i}] {m.sender_id} · {hit['kind']} · {m.created_at.isoformat()[:16]}Z · {where} · {m.id}")
        lines.append(f"    {hit['snippet']}\n")
    if hits:
        lines.append("get_message_context with an id (or its first 8 characters) shows what was said around it.")
    elif not exact:
        lines.append("Nothing found. For a literal string (a command, a code fragment), try exact: true.")
    return [types.TextContent(type="text", text='\n'.join(lines))]


async def handle_get_message_context(arguments):
    """A message with the messages before and after it in its session (or heap)"""
    from conversations.services.memory import message_context
    arguments = arguments or {}
    text = await sync_to_async(with_fresh_connection(lambda: message_context(
        arguments.get("message_id", ""), arguments.get("before", 10), arguments.get("after", 10))))()
    return [types.TextContent(type="text", text=text)]


async def handle_get_recent_work(arguments):
    """Get recent messages"""
    limit = arguments.get("limit", 50) if arguments else 50
    session_id = arguments.get("thread_id") if arguments else None

    messages = await sync_to_async(with_fresh_connection(
        lambda: MemoryService.get_recent_work(limit, session_id=session_id)
    ))()

    scope = f" in thread {session_id}" if session_id else ""
    lines = [f"Most recent {len(messages)} messages{scope}:\n"]
    for msg in messages:
        lines.append(f"[{msg.sender_id}] {msg.created_at.isoformat()}")
        lines.append(f"{str(msg.content)[:150]}...\n")

    return [types.TextContent(type="text", text='\n'.join(lines))]


async def handle_random_messages(arguments):
    """Get random messages with context"""
    count = arguments.get("count", 4) if arguments else 4
    context_messages = arguments.get("context_messages", 4) if arguments else 4

    results = await sync_to_async(with_fresh_connection(
        lambda: MemoryService.get_random_messages_with_context(
            count=count,
            context_messages=context_messages
        )
    ))()

    if not results:
        return [types.TextContent(type="text", text="No messages in database")]

    lines = [f"# Random Messages with Context\n"]
    lines.append(f"Selected {len(results)} random starting points\n")

    for idx, result in enumerate(results, 1):
        starting = result['starting_message']
        context = result['context']

        lines.append(f"\n## Random Sample {idx}\n")
        lines.append(f"**Starting at:** [{starting.sender_id}] {starting.created_at.isoformat()}\n")

        for msg in context:
            lines.append(f"[{msg.sender_id}] {msg.created_at.isoformat()}")
            lines.append(f"{str(msg.content)[:300]}\n")
            lines.append("---\n")

    return [types.TextContent(type="text", text='\n'.join(lines))]


# --- Moods (memory-lane's Moods): what each is, and what was said in one ------

READ_MOOD_MAX = 300      # turns at most in one read
READ_MOOD_CHARS = 60_000  # and about this much text; the oldest go first
EACH_CHARS = 4000


def list_moods_text():
    from django.db.models import Max
    from conversations.models import Mood
    lines = []
    moods = []
    for mood in Mood.objects.all():
        said = mood.messages.exclude(sender_id='system')
        last = said.aggregate(last=Max('created_at'))['last']
        moods.append((last, mood, said.count()))
    moods.sort(key=lambda m: m[0].isoformat() if m[0] else '', reverse=True)
    lines.append(f"{len(moods)} Moods, most recently active first:\n")
    for last, mood, count in moods:
        people = sorted(e.name for e in mood.thinking_entities())
        lines.append(f"{mood.slug} -- {mood.title or mood.slug}")
        if mood.description:
            lines.append(f"    {mood.description[:200]}")
        lines.append(f"    {count} messages · last {last.isoformat() if last else 'never'} · with {', '.join(people) or 'nobody yet'}\n")
    lines.append("read_mood with a slug reads what was said there.")
    return '\n'.join(lines)


def read_mood_text(slug, start=None, limit=60):
    """What people and agents said in a Mood, oldest first: the newest `limit`
    turns, or from `start` (a message id, or an ISO time) on."""
    from django.utils.dateparse import parse_datetime
    from conversations.models import Message, Mood
    from conversations.services.mood_view import turns
    mood = Mood.objects.filter(slug=slug).first()
    if mood is None:
        return f"No Mood '{slug}'. list_moods names them all."
    limit = max(1, min(int(limit or 60), READ_MOOD_MAX))
    found = list(turns(mood))  # (message, text), oldest first
    if start:
        when = None
        anchor = Message.objects.filter(id=start).first() if len(str(start)) == 36 else None
        if anchor is not None:
            when = anchor.created_at
        else:
            when = parse_datetime(str(start))
            if when is None:
                from django.utils.dateparse import parse_date
                day = parse_date(str(start))
                when = datetime.datetime(day.year, day.month, day.day) if day else None
            if when is not None and when.tzinfo is None:
                when = when.replace(tzinfo=datetime.timezone.utc)  # a time without an offset is UTC
        if when is None:
            return f"'{start}' is neither a message id nor an ISO time."
        found = [(m, t) for m, t in found if m.created_at >= when][:limit]
    else:
        found = found[-limit:]
    lines = []
    for msg, text in found:
        if len(text) > EACH_CHARS:
            text = text[:EACH_CHARS] + ' […]'
        lines.append(f"[{msg.sender_id}, {msg.created_at.isoformat()[:16]}Z, #m-{msg.id}] {text}")
    dropped = 0
    while lines and sum(len(l) for l in lines) > READ_MOOD_CHARS:
        lines.pop(0)
        dropped += 1
    head = [f"Mood: {mood.title or mood.slug} ({mood.slug})"]
    if mood.description:
        head.append(mood.description)
    head.append(f"{len(lines)} turns, oldest first" + (f" ({dropped} more before them left out for length)" if dropped else '')
                + " -- what people and agents said; tool calls aren't shown. Each line's #m-<id> is its link.\n")
    return '\n'.join(head + lines)


async def handle_list_moods(arguments):
    text = await sync_to_async(with_fresh_connection(list_moods_text))()
    return [types.TextContent(type="text", text=text)]


async def handle_read_mood(arguments):
    arguments = arguments or {}
    text = await sync_to_async(with_fresh_connection(
        lambda: read_mood_text(arguments.get('slug', ''), arguments.get('from'), arguments.get('limit', 60))
    ))()
    return [types.TextContent(type="text", text=text)]


# Tool registry - maps tool names to handlers
async def handle_list_threads(arguments):
    """List distinct threads, most recently active first"""
    limit = arguments.get("limit", 20) if arguments else 20
    since = arguments.get("since") if arguments else None

    threads = await sync_to_async(with_fresh_connection(
        lambda: MemoryService.list_threads(limit=limit, since=since)
    ))()

    heading = f"{len(threads)} threads" + (f" active since {since}" if since else "")
    lines = [f"{heading}, most recent first:\n"]
    for i, t in enumerate(threads, 1):
        lines.append(
            f"[{i}] last {t['last_at'].isoformat()} · first {t['first_at'].isoformat()}"
            f" · {t['message_count']} msgs"
        )
        if t['participants']:
            lines.append(f"    with: {', '.join(t['participants'])}")
        if t['cwd']:
            branch = f"  (branch {t['git_branch']})" if t['git_branch'] else ""
            lines.append(f"    cwd: {t['cwd']}{branch}")
        if t['title_hint']:
            lines.append(f"    \"{t['title_hint'][:140]}\"")
        lines.append(f"    thread_id: {t['thread_id']}\n")

    lines.append("Pass a thread_id to get_recent_work to read that thread alone.")
    return [types.TextContent(type="text", text='\n'.join(lines))]


TOOL_HANDLERS = {
    "list_moods": handle_list_moods,
    "read_mood": handle_read_mood,
    "list_threads": handle_list_threads,
    "bootstrap_memory": handle_bootstrap_memory,
    "get_latest_continuation": handle_get_latest_continuation,
    "get_message_by_id": handle_get_message_by_id,
    "get_message_context": handle_get_message_context,
    "get_messages_before": handle_get_messages_before,
    "get_era_summary": handle_get_era_summary,
    "get_context_heap": handle_get_context_heap,
    "search_messages": handle_search_messages,
    "get_recent_work": handle_get_recent_work,
    "random_messages": handle_random_messages,
}
