import hmac
import json
import logging
import os
import uuid
from collections import defaultdict
from datetime import datetime
from django.db.models import F, Max, Min, OuterRef, Q, Subquery
from django.shortcuts import render
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from .models import (
    Message,
    Thought,
    ToolUse,
    ToolResult,
    Era,
    ThinkingEntity,
)

logger = logging.getLogger(__name__)


# The read endpoints below are public and unauthenticated, so each serves a
# bounded page: none may hand out a whole table, or walk one in Python, just
# because somebody asked.
MAX_PAGE_LIMIT = 1000
RECENT_MESSAGES_MAX_LIMIT = 500
ALL_MESSAGES_DEFAULT_LIMIT = 500
MESSAGES_SINCE_DEFAULT_LIMIT = 500

# Everything a serialized message touches, fetched in the message query
# itself.  `parent__tooluse` is for the tool name shown on a ToolResult.
_MESSAGE_SELECT_RELATED = ('thought', 'tooluse', 'toolresult', 'sender', 'parent__tooluse')


def _int_param(request, name, default, minimum=0, maximum=None):
    """Read an integer query parameter.

    Returns (value, None), or (None, a 400 response) when the parameter is
    not an integer or is below `minimum`.  A value above `maximum` is
    clamped rather than refused: asking for more than we serve gets a full
    page.
    """
    raw = request.GET.get(name, '')
    if raw == '':
        return default, None
    try:
        value = int(raw)
    except ValueError:
        return None, JsonResponse({'error': f'{name} must be an integer'}, status=400)
    if value < minimum:
        return None, JsonResponse({'error': f'{name} must be at least {minimum}'}, status=400)
    if maximum is not None and value > maximum:
        value = maximum
    return value, None


def _uuid_param(raw, name):
    """Parse a UUID from a URL segment or query value: (uuid, None) or (None, 400)."""
    try:
        return uuid.UUID(str(raw)), None
    except ValueError:
        return None, JsonResponse({'error': f'{name} is not a valid UUID'}, status=400)


def _heap_order():
    """A heap's messages in order.  NULLS LAST matches Postgres's default (and
    the (context_heap, message_number) index); `id` makes the order total so
    an ?after= cursor never skips or repeats a message."""
    return (F('message_number').asc(nulls_last=True), 'id')


def _later_in_heap(anchor):
    """Messages after `anchor` within its heap, in _heap_order()."""
    if anchor.message_number is None:
        return Q(message_number__isnull=True, id__gt=anchor.id)
    return (Q(message_number__gt=anchor.message_number)
            | Q(message_number=anchor.message_number, id__gt=anchor.id)
            | Q(message_number__isnull=True))


def _anchor_message(raw):
    """The message an ?after= cursor names: (message, None) or (None, 400/404)."""
    anchor_id, error = _uuid_param(raw, 'after')
    if error:
        return None, error
    anchor = Message.objects.filter(id=anchor_id).only('id', 'context_heap', 'message_number').first()
    if anchor is None:
        return None, JsonResponse({'error': 'after: message not found'}, status=404)
    return anchor, None


def _take_page(queryset, limit):
    """At most `limit` rows, and whether there are more after them."""
    rows = list(queryset[:limit + 1])
    return rows[:limit], len(rows) > limit


def _serialize_note(note):
    return {
        'id': str(note.id),
        'from_entity': note.from_entity.name,
        'content': note.content,
        'eth_blockheight': note.eth_blockheight,
        'created_at': note.created_at.isoformat()
    }


def _notes_by_object(model_name, object_ids):
    """Serialized notes on the given objects in one query, keyed by str(id)."""
    from django.contrib.contenttypes.models import ContentType
    from .models import Note

    by_id = defaultdict(list)
    object_ids = list(object_ids)
    if not object_ids:
        return by_id
    ct = ContentType.objects.get_by_natural_key('conversations', model_name)
    notes = (Note.objects.filter(content_type=ct, object_id__in=object_ids)
             .select_related('from_entity').order_by('created_at'))
    for note in notes:
        by_id[str(note.object_id)].append(_serialize_note(note))
    return by_id


def _raw_content_by_compacting_action(actions):
    """Each CompactingAction's RawImportedContent (the first, if several), keyed by str(id)."""
    from django.contrib.contenttypes.models import ContentType
    from .models import RawImportedContent

    if not actions:
        return {}
    ca_ct = ContentType.objects.get_by_natural_key('conversations', 'compactingaction')
    raw_by_id = {}
    rows = RawImportedContent.objects.filter(
        content_type=ca_ct, object_id__in=[a.id for a in actions]
    ).order_by('pk')
    for rc in rows:
        raw_by_id.setdefault(str(rc.object_id), rc)
    return raw_by_id


def _ending_message_id(action):
    if action.ending_message_id:
        return str(action.ending_message_id)
    if action.looking_for_ending_message:
        return str(action.looking_for_ending_message)
    return None


def _compacting_actions_by_leaf(message_ids):
    """CompactingActions whose leaf (last message before the compact) is one
    of `message_ids`, keyed by that message id, as (action, raw_content).

    This used to load every CompactingAction in the database on every
    request, then run one RawImportedContent query per hit.
    """
    from .models import CompactingAction

    ids = set(message_ids)
    if not ids:
        return {}
    by_leaf = {}
    candidates = CompactingAction.objects.filter(
        Q(ending_message_id__in=ids) | Q(looking_for_ending_message__in=ids)
    )
    for action in candidates:
        # Same precedence as before: the FK wins over the looking-for UUID.
        leaf = action.ending_message_id or action.looking_for_ending_message
        if leaf in ids:
            by_leaf[leaf] = action
    raw_by_id = _raw_content_by_compacting_action(list(by_leaf.values()))
    return {leaf: (action, raw_by_id.get(str(action.id))) for leaf, action in by_leaf.items()}


def _compacting_pseudo_message(action, raw_content):
    """A CompactingAction rendered as a row in a heap's message list."""
    return {
        'id': str(action.id),
        'message_type': 'CompactingAction',
        'ending_message_id': _ending_message_id(action),
        'compact_trigger': action.compact_trigger,
        'pre_compact_tokens': action.pre_compact_tokens,
        'is_orphaned': action.context_heap_id is None,
        'linked_heap_id': str(action.context_heap_id) if action.context_heap_id else None,
        'raw_imported_content': raw_content.raw_data if raw_content else None
    }


def _serialize_full_message(msg, notes):
    """One message as heap_messages and all_messages serve it.

    Expects `msg` fetched with _MESSAGE_SELECT_RELATED and recipients
    prefetched, so this runs no queries of its own.
    """
    if hasattr(msg, 'thought'):
        actual_msg = msg.thought
    elif hasattr(msg, 'tooluse'):
        actual_msg = msg.tooluse
    elif hasattr(msg, 'toolresult'):
        actual_msg = msg.toolresult
    else:
        actual_msg = msg

    recipients = list(msg.recipients.all())
    msg_dict = {
        'id': str(actual_msg.id),
        'message_number': actual_msg.message_number,
        'message_type': actual_msg.__class__.__name__,
        'sender': msg.sender.name,
        'sender_type': msg.sender.participant_type,
        'recipients': [r.name for r in recipients],
        'recipient_types': [r.participant_type for r in recipients],
        'content': msg.content,  # JSONField - keep as dict/str, JsonResponse will serialize properly
        'timestamp': msg.timestamp,
        'eth_blockheight': msg.eth_blockheight,
        'eth_block_offset': msg.eth_block_offset,
        'created_at': msg.created_at.isoformat(),
        'session_id': str(msg.session_id) if msg.session_id else None,
        'source_file': msg.source_file,
        'missing_from_markdown': msg.missing_from_markdown,
        'cwd': msg.cwd,
        'git_branch': msg.git_branch,
        'client_version': msg.client_version,
        'parent_id': str(msg.parent_id) if msg.parent_id else None,
        'is_synthetic_error': msg.is_synthetic_error,
        'is_retry': msg.is_retry,
        'notes': notes,
    }

    # Add type-specific fields
    if hasattr(msg, 'tooluse'):
        msg_dict['tool_name'] = msg.tooluse.tool_name
        msg_dict['tool_id'] = msg.tooluse.tool_id
    elif hasattr(msg, 'toolresult'):
        msg_dict['tool_use_id'] = msg.toolresult.tool_use_id
        msg_dict['is_error'] = msg.toolresult.is_error
        # Look up parent ToolUse to get tool name
        if msg.parent and hasattr(msg.parent, 'tooluse'):
            msg_dict['tool_name'] = msg.parent.tooluse.tool_name
    elif hasattr(msg, 'thought'):
        msg_dict['signature'] = msg.thought.signature

    return msg_dict


def _serialize_page_messages(page):
    """A page of messages in heap_messages' list shape: each message, then the
    CompactingAction pseudo-message for any compact it was the leaf of."""
    page_ids = [m.id for m in page]
    notes = _notes_by_object('message', page_ids)
    compacts = _compacting_actions_by_leaf(page_ids)
    out = []
    for msg in page:
        out.append(_serialize_full_message(msg, notes.get(str(msg.id), [])))
        if msg.id in compacts:
            out.append(_compacting_pseudo_message(*compacts[msg.id]))
    return out


def memory_lane(request):
    """Main memory viewer/editor page."""
    return render(request, 'conversations/memory_lane.html')


def stream(request):
    """Live stream view - compact, auto-updating recent messages."""
    return render(request, 'conversations/stream.html')


def recent_messages(request):
    """Lightweight endpoint for stream - just last N messages (?limit=, default 100, max 500)."""
    limit, error = _int_param(request, 'limit', 100, maximum=RECENT_MESSAGES_MAX_LIMIT)
    if error:
        return error

    # The subclass rows ride along so the hasattr() checks below don't each
    # cost a query (three per message before).
    messages = (Message.objects
                .select_related('sender', 'thought', 'tooluse', 'toolresult')
                .order_by('-created_at')[:limit])

    messages_data = []
    for msg in messages:
        # Get the actual polymorphic instance
        msg_type = 'Message'
        tool_name = None
        is_error = False

        if hasattr(msg, 'thought'):
            msg_type = 'Thought'
        elif hasattr(msg, 'tooluse'):
            msg_type = 'ToolUse'
            tool_name = msg.tooluse.tool_name
        elif hasattr(msg, 'toolresult'):
            msg_type = 'ToolResult'
            is_error = msg.toolresult.is_error

        # Handle content - extract text from message format
        content = ''
        if msg.content:
            if isinstance(msg.content, str):
                content = msg.content[:1000]
            elif isinstance(msg.content, list):
                # Extract text from [{"text": "...", "type": "text"}] format
                texts = []
                for item in msg.content:
                    if isinstance(item, dict) and 'text' in item:
                        texts.append(item['text'])
                content = '\n'.join(texts)[:1000]
            elif isinstance(msg.content, dict):
                import json
                content = json.dumps(msg.content)[:1000]
            else:
                content = str(msg.content)[:1000]

        msg_dict = {
            'id': str(msg.id),
            'message_type': msg_type,
            'sender': msg.sender.name if msg.sender else 'unknown',
            'content': content,
            'timestamp': msg.timestamp,
            'tool_name': tool_name,
            'is_error': is_error,
            'context_heap_id': str(msg.context_heap_id) if msg.context_heap_id else None,
        }
        messages_data.append(msg_dict)

    return JsonResponse({'messages': messages_data})


def messages_since(request, message_id):
    """
    Get messages created after the specified message_id, oldest first.
    Returns messages with their heap context for proper placement.

    At most ?limit= of them (default 500, max 1000); `has_more` says whether
    more were waiting.  Before the cap, an old enough message_id returned
    most of the database.
    """
    anchor_id, error = _uuid_param(message_id, 'message_id')
    if error:
        return error
    limit, error = _int_param(request, 'limit', MESSAGES_SINCE_DEFAULT_LIMIT, minimum=1, maximum=MAX_PAGE_LIMIT)
    if error:
        return error

    try:
        last_msg = Message.objects.get(id=anchor_id)
        last_msg_number = last_msg.message_number
    except Message.DoesNotExist:
        return JsonResponse({'error': 'Message not found'}, status=404)
    if last_msg_number is None:
        return JsonResponse({'error': 'Message has no message_number to count from'}, status=400)

    # Messages with message_number > last
    new_messages, has_more = _take_page(
        Message.objects.filter(
            message_number__gt=last_msg_number
        ).select_related(
            'thought', 'tooluse', 'toolresult', 'sender', 'context_heap', 'parent__tooluse'
        ).prefetch_related('recipients').order_by('message_number', 'id'),
        limit,
    )

    messages_data = []
    for msg in new_messages:
        # Get the actual polymorphic instance
        if hasattr(msg, 'thought'):
            actual_msg = msg.thought
        elif hasattr(msg, 'tooluse'):
            actual_msg = msg.tooluse
        elif hasattr(msg, 'toolresult'):
            actual_msg = msg.toolresult
        else:
            actual_msg = msg

        msg_dict = {
            'id': str(actual_msg.id),
            'message_number': actual_msg.message_number,
            'message_type': actual_msg.__class__.__name__,
            'sender': msg.sender.name,
            'recipients': [r.name for r in msg.recipients.all()],
            'content': msg.content,
            'timestamp': msg.timestamp,
            'eth_blockheight': msg.eth_blockheight,
            'parent_id': str(msg.parent_id) if msg.parent_id else None,
            'heap_id': str(msg.context_heap_id) if msg.context_heap_id else None,
            'era_id': str(msg.context_heap.era_id) if msg.context_heap else None,
        }

        # Add type-specific fields
        if hasattr(msg, 'tooluse'):
            msg_dict['tool_name'] = msg.tooluse.tool_name
        elif hasattr(msg, 'toolresult'):
            msg_dict['is_error'] = msg.toolresult.is_error
            if msg.parent and hasattr(msg.parent, 'tooluse'):
                msg_dict['tool_name'] = msg.parent.tooluse.tool_name
        elif hasattr(msg, 'thought'):
            msg_dict['signature'] = msg.thought.signature

        messages_data.append(msg_dict)

    return JsonResponse({'messages': messages_data, 'has_more': has_more})


def heap_metadata(request):
    """Return just era and heap metadata without messages (for lazy loading).

    Every per-heap fact is gathered in a fixed number of queries per era rather
    than one query per heap.  Before this, each heap cost six queries (notes,
    compacting action, first message, message count, and two blockheight
    aggregates); with ~12,900 heaps in a single era that was ~78,000 round
    trips, and the child-heap search was O(heaps^2) on top.  Gunicorn killed
    the worker at 30s and the browser got an HTML error page to JSON.parse().
    """
    from .models import ContextHeap, Era, Note, CompactingAction, Message
    from django.contrib.contenttypes.models import ContentType
    from django.db.models import Min, Max, Count
    from collections import defaultdict
    from datetime import datetime

    eras = Era.objects.order_by('created_at')

    data = {
        'eras': [],
        'orphaned_compacting_actions': []
    }

    # Get content types for lookups
    heap_ct = ContentType.objects.get(app_label='conversations', model='contextheap')
    era_ct = ContentType.objects.get(app_label='conversations', model='era')

    # All era notes in one query, grouped by era id.
    era_notes_by_id = defaultdict(list)
    for note in Note.objects.filter(content_type=era_ct).select_related('from_entity').order_by('created_at'):
        era_notes_by_id[str(note.object_id)].append(note)

    # All heap notes in one query, grouped by heap id.
    heap_notes_by_id = defaultdict(list)
    for note in Note.objects.filter(content_type=heap_ct).select_related('from_entity').order_by('created_at'):
        heap_notes_by_id[str(note.object_id)].append(note)

    def serialize_note(note):
        return {
            'id': str(note.id),
            'from_entity': note.from_entity.name,
            'content': note.content,
            'eth_blockheight': note.eth_blockheight,
            'created_at': note.created_at.isoformat()
        }

    for era in eras:
        era_data = {
            'id': str(era.id),
            'name': era.name,
            'created_at': era.created_at.isoformat(),
            'earliest_blockheight': era.earliest_blockheight(),
            'latest_blockheight': era.latest_blockheight(),
            'context_heaps': [],
            'notes': [serialize_note(n) for n in era_notes_by_id.get(str(era.id), [])]
        }

        # One query for every per-heap aggregate: message count, blockheight
        # range, and the timestamps used for sorting.  All of these aggregate
        # over the same `messages` join, so there is no fan-out.
        all_heaps = list(
            era.context_heaps
               .select_related('compacting_action')
               .annotate(
                   msg_count=Count('messages'),
                   earliest_bh=Min('messages__eth_blockheight'),
                   latest_bh=Max('messages__eth_blockheight'),
                   first_msg_timestamp=Min('messages__timestamp'),
                   first_msg_created=Min('messages__created_at'),
               )
        )

        if not all_heaps:
            data['eras'].append(era_data)
            continue

        heap_ids = [h.id for h in all_heaps]

        # One query for the first message of every heap (Postgres DISTINCT ON).
        first_msg_by_heap = {}
        for msg in (Message.objects
                    .filter(context_heap_id__in=heap_ids)
                    .order_by('context_heap_id', 'message_number')
                    .distinct('context_heap_id')
                    .only('id', 'timestamp', 'context_heap_id')):
            first_msg_by_heap[msg.context_heap_id] = msg

        # Sort by first message timestamp, falling back to created_at
        def heap_sort_key(h):
            if h.first_msg_timestamp:
                return (0, h.first_msg_timestamp)
            elif h.first_msg_created:
                return (1, h.first_msg_created.timestamp() * 1000)
            else:
                return (2, h.created_at.timestamp() * 1000)

        all_heaps.sort(key=heap_sort_key)

        # A split heap's parent is the heap its first message belongs to.  The
        # first-message map above already answers that, so the parent lookup is
        # a dict read rather than a scan over every heap.
        children_by_parent = defaultdict(list)
        for heap in all_heaps:
            if heap.type != 'split_point':
                continue
            first_msg = first_msg_by_heap.get(heap.id)
            if first_msg and first_msg.context_heap_id != heap.id:
                children_by_parent[first_msg.context_heap_id].append(heap)

        # Build metadata for each heap (without messages)
        def serialize_heap_metadata(heap):
            # Check for compacting action (select_related above; no query here)
            compacting_action = None
            ca = getattr(heap, 'compacting_action', None)
            if ca:
                # Get ending message ID from either FK or looking_for field
                ending_msg_id = None
                if ca.ending_message_id:
                    ending_msg_id = str(ca.ending_message_id)
                elif ca.looking_for_ending_message:
                    ending_msg_id = str(ca.looking_for_ending_message)

                compacting_action = {
                    'id': str(ca.id),
                    'ending_message_id': ending_msg_id,
                    'compact_trigger': ca.compact_trigger,
                    'continuation_message_id': str(ca.continuation_message_id) if ca.continuation_message_id else None
                }

            # First message info, from the map built above
            first_message = first_msg_by_heap.get(heap.id)
            first_message_timestamp = None
            first_message_id = None
            if first_message:
                first_message_id = str(first_message.id)
                if first_message.timestamp:
                    first_message_timestamp = datetime.fromtimestamp(first_message.timestamp / 1000).isoformat()

            heap_data = {
                'id': str(heap.id),
                'type': heap.type,
                'type_display': heap.get_type_display(),
                'first_message_id': first_message_id,
                'first_message_timestamp': first_message_timestamp,
                'message_count': heap.msg_count,
                'created_at': heap.created_at.isoformat(),
                'earliest_blockheight': heap.earliest_bh,
                'latest_blockheight': heap.latest_bh,
                'child_heaps': [
                    serialize_heap_metadata(child)
                    for child in children_by_parent.get(heap.id, [])
                ],
                'compacting_action': compacting_action,
                'notes': [serialize_note(n) for n in heap_notes_by_id.get(str(heap.id), [])]
            }

            return heap_data

        # Serialize root heaps (non-split heaps)
        for heap in all_heaps:
            if heap.type != 'split_point':
                era_data['context_heaps'].append(serialize_heap_metadata(heap))

        data['eras'].append(era_data)

    # Get orphaned compacting actions (not linked to any context heap)
    from .models import RawImportedContent
    ca_ct = ContentType.objects.get(app_label='conversations', model='compactingaction')
    orphaned = list(CompactingAction.objects.filter(context_heap__isnull=True).order_by('created_at'))

    # Raw imported content for all orphans in one query
    raw_by_object_id = {
        str(rc.object_id): rc
        for rc in RawImportedContent.objects.filter(
            content_type=ca_ct,
            object_id__in=[c.id for c in orphaned]
        )
    } if orphaned else {}

    for compact in orphaned:
        raw_content = raw_by_object_id.get(str(compact.id))

        # Get ending message ID
        ending_msg_id = None
        if compact.ending_message_id:
            ending_msg_id = str(compact.ending_message_id)
        elif compact.looking_for_ending_message:
            ending_msg_id = str(compact.looking_for_ending_message)

        data['orphaned_compacting_actions'].append({
            'id': str(compact.id),
            'ending_message_id': ending_msg_id,
            'compact_trigger': compact.compact_trigger,
            'created_at': compact.created_at.isoformat(),
            'raw_imported_content': raw_content.raw_data if raw_content else None
        })

    return JsonResponse(data, safe=False)


def all_messages(request):
    """Messages grouped by Era and ContextHeap, one page at a time.

    This used to put every message in the database into a single response,
    with a handful of queries per message and every CompactingAction loaded
    once per heap: one unauthenticated request could pin a worker and dump
    the corpus.  It now pages over messages in (heap id, message_number, id)
    order, which the (context_heap, message_number) index serves directly:

      ?limit=  messages per page, default 500, max 1000
      ?after=  the `next` value from the previous page

    A page carries only the eras and heaps its messages belong to, each in
    the same shape as before.  Heaps are listed flat under their era with
    `child_heaps` empty (a split heap is an entry of its own, type
    'split_point'), and a heap that straddles a page boundary appears on
    both pages.  `has_more` and `next` say whether and where to continue.
    Orphaned compacting actions come with the first page only, at most 1000.
    """
    from .models import CompactingAction, ContextHeap

    limit, error = _int_param(request, 'limit', ALL_MESSAGES_DEFAULT_LIMIT, minimum=1, maximum=MAX_PAGE_LIMIT)
    if error:
        return error

    messages = Message.objects.filter(context_heap__isnull=False)
    after = request.GET.get('after', '')
    if after:
        anchor, error = _anchor_message(after)
        if error:
            return error
        if anchor.context_heap_id is None:
            return JsonResponse({'error': 'after: message is not in any heap'}, status=400)
        heap_id = anchor.context_heap_id
        # The >= is redundant with the OR below but lets the index seek
        # straight to the anchor's heap instead of walking from the start.
        messages = messages.filter(context_heap_id__gte=heap_id).filter(
            Q(context_heap_id__gt=heap_id) | (Q(context_heap_id=heap_id) & _later_in_heap(anchor))
        )

    page, has_more = _take_page(
        messages.select_related(*_MESSAGE_SELECT_RELATED)
                .prefetch_related('recipients')
                .order_by('context_heap_id', *_heap_order()),
        limit,
    )

    # Heaps on this page, in page order, with their first message.
    heap_ids = list(dict.fromkeys(m.context_heap_id for m in page))
    first_in_heap = Message.objects.filter(context_heap=OuterRef('pk')).order_by(*_heap_order())
    heaps = {
        h.id: h for h in ContextHeap.objects.filter(id__in=heap_ids).annotate(
            first_msg_id=Subquery(first_in_heap.values('id')[:1]),
            first_msg_timestamp=Subquery(first_in_heap.values('timestamp')[:1]),
        )
    }
    heap_ranges = {
        row['context_heap_id']: row
        for row in (Message.objects.filter(context_heap_id__in=heap_ids)
                    .values('context_heap_id').order_by()
                    .annotate(earliest=Min('eth_blockheight'), latest=Max('eth_blockheight')))
    }
    heap_compacts = {
        ca.context_heap_id: ca
        for ca in CompactingAction.objects.filter(context_heap_id__in=heap_ids)
    }

    # Eras those heaps belong to, oldest first.
    era_ids = list(dict.fromkeys(heaps[h].era_id for h in heap_ids))
    eras = sorted(Era.objects.filter(id__in=era_ids), key=lambda e: (e.created_at, str(e.id)))
    era_ranges = {
        row['context_heap__era_id']: row
        for row in (Message.objects.filter(context_heap__era_id__in=era_ids)
                    .values('context_heap__era_id').order_by()
                    .annotate(earliest=Min('eth_blockheight'), latest=Max('eth_blockheight')))
    }

    era_notes = _notes_by_object('era', era_ids)
    heap_notes = _notes_by_object('contextheap', heap_ids)
    page_ids = [m.id for m in page]
    msg_notes = _notes_by_object('message', page_ids)
    leaf_compacts = _compacting_actions_by_leaf(page_ids)

    heap_data_by_id = {}
    for heap_id in heap_ids:
        heap = heaps[heap_id]
        ca = heap_compacts.get(heap_id)
        compacting_action = None
        if ca:
            compacting_action = {
                'id': str(ca.id),
                'ending_message_id': _ending_message_id(ca),
                'compact_trigger': ca.compact_trigger,
                'continuation_message_id': str(ca.continuation_message_id) if ca.continuation_message_id else None
            }
        first_message_timestamp = None
        if heap.first_msg_timestamp:
            first_message_timestamp = datetime.fromtimestamp(heap.first_msg_timestamp / 1000).isoformat()
        heap_range = heap_ranges.get(heap_id, {})
        heap_data_by_id[heap_id] = {
            'id': str(heap.id),
            'type': heap.type,
            'type_display': heap.get_type_display(),
            'first_message_id': str(uuid.UUID(str(heap.first_msg_id))) if heap.first_msg_id else None,
            'first_message_timestamp': first_message_timestamp,
            'created_at': heap.created_at.isoformat(),
            'earliest_blockheight': heap_range.get('earliest'),
            'latest_blockheight': heap_range.get('latest'),
            'messages': [],
            'child_heaps': [],
            'compacting_action': compacting_action,
            'notes': heap_notes.get(str(heap_id), []),
        }

    for msg in page:
        heap_messages_list = heap_data_by_id[msg.context_heap_id]['messages']
        heap_messages_list.append(_serialize_full_message(msg, msg_notes.get(str(msg.id), [])))
        if msg.id in leaf_compacts:
            heap_messages_list.append(_compacting_pseudo_message(*leaf_compacts[msg.id]))

    data = {
        'eras': [],
        'orphaned_compacting_actions': [],
        'has_more': has_more,
        'next': str(page[-1].id) if has_more else None,
    }
    for era in eras:
        era_range = era_ranges.get(era.id, {})
        data['eras'].append({
            'id': str(era.id),
            'name': era.name,
            'created_at': era.created_at.isoformat(),
            'earliest_blockheight': era_range.get('earliest'),
            'latest_blockheight': era_range.get('latest'),
            'context_heaps': [heap_data_by_id[h] for h in heap_ids if heaps[h].era_id == era.id],
            'notes': era_notes.get(str(era.id), []),
        })

    # Orphaned compacting actions (not linked to any context heap): first page only.
    if not after:
        orphaned = list(
            CompactingAction.objects.filter(context_heap__isnull=True).order_by('created_at', 'id')[:MAX_PAGE_LIMIT]
        )
        raw_by_id = _raw_content_by_compacting_action(orphaned)
        for compact in orphaned:
            raw_content = raw_by_id.get(str(compact.id))
            data['orphaned_compacting_actions'].append({
                'id': str(compact.id),
                'ending_message_id': _ending_message_id(compact),
                'compact_trigger': compact.compact_trigger,
                'created_at': compact.created_at.isoformat(),
                'raw_imported_content': raw_content.raw_data if raw_content else None
            })

    return JsonResponse(data, safe=False)


def api_messages(request):
    """API endpoint for fetching messages with filtering (?limit=, default 100, max 1000)."""
    # Get filter parameters
    search = request.GET.get('search', '').lower()
    person = request.GET.get('person', '')
    show_thinking = request.GET.get('show_thinking', 'true') == 'true'
    message_types = request.GET.get('types', 'context_opening,regular,thought,tool_use,tool_result').split(',')
    limit, error = _int_param(request, 'limit', 100, maximum=MAX_PAGE_LIMIT)
    if error:
        return error

    # Start with all messages from base table
    messages = Message.objects.all()

    # Apply filters
    if person:
        # Filter by sender or recipients (M2M)
        messages = messages.filter(sender__name=person) | messages.filter(recipients__name=person)

    # Filter by message type
    if not show_thinking:
        # Exclude Thought messages
        messages = messages.exclude(thought__isnull=False)

    # Order by timestamp (or created_at if timestamp is null).  The subclass
    # rows and sender come in the same query rather than one or more per row.
    messages = messages.select_related(
        'sender', 'thought', 'tooluse', 'toolresult'
    ).order_by('-timestamp', '-created_at')[:limit]

    # Serialize messages with polymorphic content
    data = []
    for msg in messages.prefetch_related('recipients'):
        # Determine message type and get content
        message_type = None
        content = None
        extra = {}

        # Check which subclass this is
        if hasattr(msg, 'thought'):
            message_type = 'thought'
            content = str(msg.thought.content)
            extra['signature'] = msg.thought.signature
            extra['parent_uuid'] = str(msg.parent_id) if msg.parent_id else None
            extra['context_heap'] = str(msg.context_heap_id) if msg.context_heap_id else None
        elif hasattr(msg, 'tooluse'):
            message_type = 'tool_use'
            content = f"[Tool: {msg.tooluse.tool_name}]"
            extra['tool_name'] = msg.tooluse.tool_name
            extra['tool_id'] = msg.tooluse.tool_id
            extra['parent_uuid'] = str(msg.parent_id) if msg.parent_id else None
            extra['context_heap'] = str(msg.context_heap_id) if msg.context_heap_id else None
        elif hasattr(msg, 'toolresult'):
            message_type = 'tool_result'
            result_content = str(msg.toolresult.content)
            content = result_content[:100] + '...' if len(result_content) > 100 else result_content
            extra['is_error'] = msg.toolresult.is_error
            extra['tool_use_id'] = msg.toolresult.tool_use_id
            extra['parent_uuid'] = str(msg.parent_id) if msg.parent_id else None
            extra['context_heap'] = str(msg.context_heap_id) if msg.context_heap_id else None
        else:
            message_type = 'message'
            content = str(msg.content)
            extra['parent_uuid'] = str(msg.parent_id) if msg.parent_id else None
            extra['context_heap'] = str(msg.context_heap_id) if msg.context_heap_id else None

        # Filter by message type
        if message_type and message_type not in message_types:
            continue

        # Filter by search text
        if search and content and search not in content.lower():
            continue

        # Get recipients
        recipient_names = [r.name for r in msg.recipients.all()]

        data.append({
            'id': str(msg.id),
            'message_type': message_type,
            'sender': msg.sender.name,
            'recipients': recipient_names,
            'content': content,
            'timestamp': msg.timestamp,
            'session_id': str(msg.session_id) if msg.session_id else None,
            **extra
        })

    return JsonResponse(data, safe=False)


def heap_messages(request, heap_id):
    """Messages of one context heap, a page at a time.

      ?limit=  messages per page, default and max 1000
      ?after=  the `next` value from the previous page

    `has_more` says whether another page follows.  A CompactingAction
    pseudo-message follows its leaf message and does not count toward the
    limit.  Before paging, a big heap came back whole, with a notes query
    per message and every CompactingAction in the database loaded per call.
    """
    from .models import ContextHeap

    heap_uuid, error = _uuid_param(heap_id, 'heap_id')
    if error:
        return error
    limit, error = _int_param(request, 'limit', MAX_PAGE_LIMIT, minimum=1, maximum=MAX_PAGE_LIMIT)
    if error:
        return error

    try:
        heap = ContextHeap.objects.get(id=heap_uuid)
    except ContextHeap.DoesNotExist:
        return JsonResponse({'error': 'Heap not found'}, status=404)

    messages = heap.messages.all()
    after = request.GET.get('after', '')
    if after:
        anchor, error = _anchor_message(after)
        if error:
            return error
        if anchor.context_heap_id != heap.id:
            return JsonResponse({'error': 'after: message is not in this heap'}, status=400)
        messages = messages.filter(_later_in_heap(anchor))

    page, has_more = _take_page(
        messages.select_related(*_MESSAGE_SELECT_RELATED)
                .prefetch_related('recipients')
                .order_by(*_heap_order()),
        limit,
    )

    return JsonResponse({
        'messages': _serialize_page_messages(page),
        'has_more': has_more,
        'next': str(page[-1].id) if has_more else None,
    }, safe=False)


@csrf_exempt
@require_http_methods(["POST"])
def ingest(request):
    """
    Ingest endpoint for receiving JSONL lines from watchers.

    Requires Authorization: Bearer <INGEST_API_KEY>. Refuses everything (503)
    when INGEST_API_KEY is unset: a misconfigured deploy must not become an
    open write path into the record (#12).

    Accepts POST with JSON body:
    {
        "lines": ["jsonl line 1", "jsonl line 2", ...],
        "username": "justin",  # optional, defaults to "justin"
        "era_name": "Current Working Era",  # optional
        "source": "hunter-watcher",  # optional, for logging
        "agent": "magent"  # optional: whose session this is; must be an agent the record knows
    }

    Or single line:
    {
        "line": "single jsonl line",
        "username": "justin"
    }

    Returns:
    {
        "imported": 5,
        "skipped": 2,
        "errors": ["error message 1", ...]
    }
    """
    from importers_and_parsers.claude_code_v2 import import_line_from_claude_code_v2
    from watcher.heap_assignment import assign_heap_to_message
    from constant_sorrow.constants import EVENT_TYPE_WE_DO_NOT_HANDLE_YET

    expected_key = os.environ.get('INGEST_API_KEY')
    if not expected_key:
        return JsonResponse({'error': 'Ingest is not configured'}, status=503)
    auth_header = request.headers.get('Authorization', '')
    if not hmac.compare_digest(auth_header.encode(), f'Bearer {expected_key}'.encode()):
        return JsonResponse({'error': 'Unauthorized'}, status=401)

    try:
        data = json.loads(request.body)
    except json.JSONDecodeError as e:
        return JsonResponse({'error': f'Invalid JSON: {e}'}, status=400)

    # Get parameters
    username = data.get('username', 'justin')
    era_name = data.get('era_name', 'Current Working Era (Era N)')
    source = data.get('source', 'unknown')
    # A watcher in another agent's container files its sessions as that
    # agent's. Only an existing agent, so a typo can't make a new one.
    agent = data.get('agent', 'magent')
    if 'agent' in data and not ThinkingEntity.objects.filter(name=agent, is_biological_human=False).exists():
        return JsonResponse({'error': f'no agent named {agent!r}'}, status=400)

    # Get lines - support both single line and batch
    lines = data.get('lines', [])
    if 'line' in data:
        lines = [data['line']]

    if not lines:
        return JsonResponse({'error': 'No lines provided'}, status=400)

    imported, skipped, errors = import_lines(lines, era_name=era_name, source=source, username=username, agent=agent)
    return JsonResponse({
        'imported': imported,
        'skipped': skipped,
        'errors': errors[:10]  # Limit error messages returned
    })


def import_lines(lines, *, era_name='Current Working Era (Era N)', source='unknown', username='justin', agent='magent'):
    """Scrub, import and heap-assign transcript lines: (imported, skipped, errors).

    Every way into the record goes through here -- the watcher's ingest and
    the runner's stream alike -- so a line gets the same redaction, routing
    and storage whichever way it came. Rows are filed as from
    'ingest-<source>', and the agent's side of the session as `agent`'s.
    """
    from importers_and_parsers.claude_code_v2 import import_line_from_claude_code_v2
    from watcher.heap_assignment import assign_heap_to_message
    from constant_sorrow.constants import EVENT_TYPE_WE_DO_NOT_HANDLE_YET

    era, _ = Era.objects.get_or_create(name=era_name)

    # Optional: Apply secrets scrubbing via external scrubber service. Lines
    # are pattern-redacted in the importer regardless; if the scrubber was
    # configured but couldn't scrub this batch, its tool results keep their
    # link and lose their output, rather than going public unscrubbed.
    scrubber_url = os.environ.get('SCRUBBER_URL')
    scrubbed = not scrubber_url
    if scrubber_url and lines:
        try:
            import requests
            response = requests.post(
                f"{scrubber_url}/scrub/batch",
                json={"texts": lines},
                timeout=10
            )
            if response.status_code == 200:
                result = response.json()
                lines = result['texts']
                scrubbed = True
                if result['redacted_count'] > 0:
                    logger.info(f"Scrubber redacted secrets in {result['redacted_count']} lines")
            else:
                logger.warning(f"Scrubber returned {response.status_code}; storing this batch without tool output")
        except Exception as e:
            logger.warning(f"Could not reach scrubber service: {e}; storing this batch without tool output")

    # Process lines
    imported = 0
    skipped = 0
    errors = []
    current_heap = None

    for line in lines:
        try:

            # Import the line
            event, created = import_line_from_claude_code_v2(
                line, era, f"ingest-{source}", username, keep_tool_output=scrubbed, agent=agent
            )

            if event is EVENT_TYPE_WE_DO_NOT_HANDLE_YET:
                skipped += 1
                continue

            if not created:
                skipped += 1
                continue

            # Assign heap if it's a Message
            if isinstance(event, Message):
                heap = assign_heap_to_message(event, era, current_heap)
                current_heap = heap

            imported += 1

        except Exception as e:
            errors.append(str(e))
            logger.error(f"Error importing line from {source}: {e}")

    logger.info(f"Ingest from {source}: imported={imported}, skipped={skipped}, errors={len(errors)}")
    return imported, skipped, errors
