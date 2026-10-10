"""
Conversation models for memory archive.

Structure:
- Era - groups related context heaps (e.g., "Era 0", "Era 1")
  - ContextHeap - a single context heap within an era
    - Message - messages accumulate in heaps until compacting

Message hierarchy (polymorphic):
- Message (concrete base) - common fields including content
  - Thought - signed thinking message
  - ToolUse - tool calls with parameters
  - ToolResult - tool execution results
"""

from django.db import models
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
import uuid, json, hashlib
from constant_sorrow.constants import EVENT_TYPE_WE_DO_NOT_HANDLE_YET


# ============================================================================
# Conversation Participant Models
# ============================================================================

class ConversationParticipant(models.Model):
    """
    Base model for anyone/anything that can send or receive messages.

    Participants include:
    - Thinking entities (humans, AI) - have deliberation and intention
    - Tools (slash commands, APIs) - just I/O machines
    - Oracles (blockchain data sources) - provide information
    - System components (compilers, linters, stdout) - automated responses

    Most details about a participant come from inward-pointing relationships
    from other models (messages sent, messages received, etc).
    """

    class ParticipantType(models.TextChoices):
        HUMAN = 'human', 'Human'
        AI = 'ai', 'AI'
        TOOL = 'tool', 'Tool'
        ORACLE = 'oracle', 'Oracle'
        SYSTEM = 'system', 'System'

    name = models.CharField(max_length=50, unique=True, primary_key=True)
    participant_type = models.CharField(
        max_length=20,
        choices=ParticipantType.choices,
        default=ParticipantType.HUMAN
    )

    class Meta:
        db_table = 'conversation_participants'

    def __str__(self):
        return self.name


class ThinkingEntity(ConversationParticipant):
    """
    A thinking entity - human or AI.

    Distinguished from other participants by having deliberation and intention.
    """


    is_biological_human = models.BooleanField(default=True)

    class Meta:
        db_table = 'thinking_entities'
        verbose_name_plural = 'thinking entities'


# ============================================================================
# Era and Context Heap Models
# ============================================================================

class Era(models.Model):
    """
    A named era in conversation history.

    Sometimes, this represents a time when previous context was lost or otherwise not used.
    In others, it is significant change in the runtime environment of the client(s) being used by the agent(s).
    In still others, it represents a significant "life event" or inflection point for the agents' understanding and story.

    Eras group related context heaps together.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=100, unique=True)
    is_current = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'eras'
        ordering = ['created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['is_current'],
                condition=models.Q(is_current=True),
                name='unique_current_era'
            )
        ]

    def save(self, *args, **kwargs):
        if self.is_current:
            # Unset any other current era
            Era.objects.filter(is_current=True).exclude(pk=self.pk).update(is_current=False)
        super().save(*args, **kwargs)

    @classmethod
    def get_current(cls):
        """Returns the current era, or None if none is set."""
        return cls.objects.filter(is_current=True).first()

    def earliest_blockheight(self):
        """Returns the earliest blockheight from all messages in this era."""
        from django.db.models import Min
        result = Message.objects.filter(
            context_heap__era=self,
            eth_blockheight__isnull=False
        ).aggregate(earliest=Min('eth_blockheight'))
        return result['earliest']

    def latest_blockheight(self):
        """Returns the latest blockheight from all messages in this era."""
        from django.db.models import Max
        result = Message.objects.filter(
            context_heap__era=self,
            eth_blockheight__isnull=False
        ).aggregate(latest=Max('eth_blockheight'))
        return result['latest']

    def __str__(self):
        return self.name


class ContextHeapType(models.TextChoices):
    """Types of context heaps based on why they were created."""
    FRESH = 'fresh', 'Fresh conversation'
    POST_COMPACTING = 'post_compacting', 'After compacting'
    SPLIT_POINT = 'split_point', 'Context split'


class ContextHeap(models.Model):
    """
    This is the short-term memory of an AI ThinkingEntity.

    The 'heap' of context (in some circles, this is called a "Context Window") represents the
    conversational knowledge to which an LLM can have ready-access at any given prompt.

    Heaps are occasionally "compacted" in order to preserve the conversational and work flow.
    The CompactingActions represent the end of a heap and also usually the beginning of another.

    The 'type' field indicates why this context heap was created:
    - FRESH: Beginning of a new conversation
    - POST_COMPACTING: Started after a context compacting operation
    - SPLIT_POINT: Created due to export splits or model changes

    For SPLIT_POINT heaps, first_message points to the message in the parent heap
    where the split occurred (i.e., first_message.context_heap != self).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    era = models.ForeignKey(Era, models.CASCADE, related_name='context_heaps')
    type = models.CharField(
        max_length=20,
        choices=ContextHeapType.choices,
        default=ContextHeapType.FRESH
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def first_message(self):
        """Get the first message in this heap by message_number."""
        return self.messages.order_by('message_number').first()

    def add_event(self, event):
        """
        Add an event (Message or CompactingAction) to this heap with the next sequential message_number.

        Sets the event's context_heap and message_number (for Messages), then saves.
        For CompactingActions, just sets context_heap.
        """
        from conversations.models import Message

        if isinstance(event, Message):
            # Get the current max message_number in this heap
            last_message = self.messages.order_by('message_number').last()
            next_number = (last_message.message_number + 1) if last_message else 1

            # Set the message's heap and number
            event.context_heap = self
            event.message_number = next_number
            event.save()
        else:
            # CompactingAction - just set heap
            event.context_heap = self
            event.save()

        return event

    def check_timestamps_against_message_numbers(self):
        """
        Verify that timestamps (where they exist) are ordered consistently with message_numbers.

        Returns a dict with:
        - 'valid': bool - whether ordering is consistent
        - 'violations': list of (msg_num, timestamp, prev_msg_num, prev_timestamp) tuples
        """
        messages_with_timestamps = list(
            self.messages.filter(timestamp__isnull=False).order_by('message_number')
        )

        violations = []
        for i in range(1, len(messages_with_timestamps)):
            prev_msg = messages_with_timestamps[i-1]
            curr_msg = messages_with_timestamps[i]

            # Timestamps should increase with message_number
            if curr_msg.timestamp < prev_msg.timestamp:
                violations.append((
                    curr_msg.message_number,
                    curr_msg.timestamp,
                    prev_msg.message_number,
                    prev_msg.timestamp
                ))

        return {
            'valid': len(violations) == 0,
            'violations': violations
        }

    class Meta:
        db_table = 'context_heaps'
        ordering = ['created_at']

    def __str__(self):
        # TODO: What if there are no messages in this heap?
        return f"{self.era.name} - {self.get_type_display()} - Heap starting at msg #{self.first_message().message_number}"

    def parent_heap(self):
        """For SPLIT_POINT heaps, return the heap they split from."""
        if self.type != ContextHeapType.SPLIT_POINT:
            return None
        return self.first_message().context_heap  # Will be different from self

    def earliest_blockheight(self):
        """Returns the earliest blockheight from messages in this heap."""
        result = self.messages.filter(eth_blockheight__isnull=False).aggregate(
            earliest=models.Min('eth_blockheight')
        )
        return result['earliest']

    def latest_blockheight(self):
        """Returns the latest blockheight from messages in this heap."""
        result = self.messages.filter(eth_blockheight__isnull=False).aggregate(
            latest=models.Max('eth_blockheight')
        )
        return result['latest']


# ============================================================================
# Mood
# ============================================================================

class Mood(models.Model):
    """
    A Mood is what a conversation is *about*: a room people and agents share.

    It is deliberately orthogonal to the two groupings that already exist:

      Era         a phase of the relationship
      ContextHeap where a context window filled up and compacting occurred
      Mood        the subject a conversation belongs to

    A message can sit in all three. Heaps and eras are artifacts of how the
    machinery ran; a Mood is chosen by people, outlives any one session, and
    can span directories, repositories and backends.

    The slug is its name in URLs and APIs, and follows its title: renamed, a
    Mood gets a new slug, and the old one is kept as an alias (MoodAlias), so
    whatever still knows it by the old name -- a container's runner, a link --
    still finds it. The id never changes. (Until October 2026 this was a "Motion".)
    """

    slug = models.SlugField(max_length=100, unique=True)
    title = models.CharField(max_length=200, blank=True)
    description = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    eth_blockheight = models.BigIntegerField(
        null=True, blank=True,
        help_text='Block height at which this Mood was opened'
    )

    class Meta:
        db_table = 'moods'
        ordering = ['slug']

    @classmethod
    def by_slug(cls, slug):
        """The Mood called `slug`, now or before a rename; None if none."""
        mood = cls.objects.filter(slug=slug).first()
        if mood is None:
            alias = MoodAlias.objects.filter(slug=slug).select_related('mood').first()
            mood = alias.mood if alias else None
        return mood

    @classmethod
    def by_slug_or_404(cls, slug):
        from django.http import Http404
        mood = cls.by_slug(slug)
        if mood is None:
            raise Http404('no such Mood')
        return mood

    def earliest_blockheight(self):
        """Returns the earliest blockheight from messages in this Mood."""
        return self.messages.filter(eth_blockheight__isnull=False).aggregate(
            earliest=models.Min('eth_blockheight'))['earliest']

    def latest_blockheight(self):
        """Returns the latest blockheight from messages in this Mood."""
        return self.messages.filter(eth_blockheight__isnull=False).aggregate(
            latest=models.Max('eth_blockheight'))['latest']

    def thinking_entities(self):
        """Who has spoken in this Mood -- humans and agents, not tools."""
        return ThinkingEntity.objects.filter(sent_messages__mood=self).distinct()

    def claim(self, session_id):
        """Route a session's messages into this Mood as they arrive."""
        claim, _ = MoodSession.objects.update_or_create(session_id=session_id, defaults={'mood': self})
        return claim

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        _slugs['at'] = 0.0  # a new or renamed Mood: ids are looked up afresh

    @classmethod
    def free_slug(cls, title, mood=None):
        """A slug for `title` that names no other Mood, now or before a rename.
        `mood` may have it back: renamed back to an old name, it gets its old slug."""
        from django.utils.text import slugify
        base = slugify(title)[:60].strip('-') or 'mood'
        slug, n = base, 2
        while (cls.objects.filter(slug=slug).exclude(pk=getattr(mood, 'pk', None)).exists()
               or MoodAlias.objects.filter(slug=slug).exclude(mood=mood).exists()):
            slug, n = f'{base}-{n}', n + 1
        return slug

    def rename(self, title, description=None):
        """A new title, and the slug that goes with it; the old slug becomes an alias."""
        self.title = title
        if description is not None:
            self.description = description
        slug = Mood.free_slug(title, self)
        if slug != self.slug:
            MoodAlias.objects.get_or_create(slug=self.slug, defaults={'mood': self})
            MoodAlias.objects.filter(slug=slug, mood=self).delete()  # an old name of its own, taken back
            self.slug = slug
        self.save()

    def __str__(self):
        return self.title or self.slug


_slugs = {'at': 0.0, 'by_id': {}}


def mood_slug(mood_id):
    """The slug a Mood goes by now, from its id (None for None). Kept a few
    seconds per process: there are few Moods, and a rename shows soon enough."""
    import time
    if mood_id is None:
        return None
    if mood_id not in _slugs['by_id'] or time.monotonic() - _slugs['at'] > 5:
        _slugs['by_id'] = dict(Mood.objects.values_list('id', 'slug'))
        _slugs['at'] = time.monotonic()
    return _slugs['by_id'].get(mood_id)


class MoodAlias(models.Model):
    """A slug a Mood went by before it was renamed: still found by it."""

    slug = models.SlugField(max_length=100, unique=True)
    mood = models.ForeignKey(Mood, models.CASCADE, related_name='aliases')
    retired_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'mood_aliases'


class MoodSession(models.Model):
    """A runtime session claimed by a Mood.

    Sessions are instances of a process; Moods are subjects. One Mood
    collects as many sessions as the subject needed -- across compactions,
    resumes, machines, people and backends.

    This exists so routing happens at import time. A message lands in its
    Mood as it arrives, rather than being swept up by a later pass, which
    means a Mood is live rather than periodically reconciled.
    """

    session_id = models.UUIDField(primary_key=True)
    mood = models.ForeignKey(Mood, models.CASCADE, related_name='claimed_sessions')
    claimed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'mood_sessions'
        ordering = ['claimed_at']

    @classmethod
    def mood_for(cls, session_id):
        """The Mood claiming this session, or None. Used by importers."""
        if not session_id:
            return None
        claim = cls.objects.filter(session_id=session_id).select_related('mood').first()
        return claim.mood if claim else None

    @classmethod
    def claim_by_history(cls, session_id, message_uuid):
        """Claim an unclaimed session for the Mood of a message it carries.

        A resumed or forked session starts by copying its predecessor's
        history under the original uuids. When one of those is already in a
        Mood, the new session is a continuation of it and belongs there
        too; the copied lines come first, so the claim lands before the
        session's own new messages are imported. This is how a turn woken by
        the poller finds its way back into the Mood. Returns the Mood or
        None.
        """
        if not session_id or not message_uuid:
            return None
        row = Message.objects.filter(id=message_uuid).values('session_id', 'mood_id').first()
        if row is None or str(row['session_id']) == str(session_id):
            return None
        # Where that session lives now, if it has been claimed since; the
        # message's own Mood only if not.
        current = cls.mood_for(row['session_id'])
        mood_id = current.pk if current else row['mood_id']
        if mood_id is None:
            return None
        claim, _ = cls.objects.get_or_create(session_id=session_id, defaults={'mood_id': mood_id})
        return claim.mood

    def __str__(self):
        return f"{self.session_id} → {self.mood_id}"


# ============================================================================
# Message Models (Polymorphic)
# ============================================================================

class Message(models.Model):
    """
    Base class for all message types.

    All messages have content (JSONField to handle both text and structured data).
    All messages belong to a context heap.
    Messages can optionally have a parent for threading.
    """

    # Identity (from client)
    id = models.UUIDField(primary_key=True)
    message_number = models.IntegerField(null=True, blank=True)

    # Content - all messages have content
    content = models.JSONField()

    # Context - all messages belong to a heap
    context_heap = models.ForeignKey('ContextHeap', models.CASCADE, related_name='messages', null=True, blank=True)

    # Subject - what this message is about, independent of where the context
    # window happened to end. Nullable: most of the corpus predates Moods,
    # and SET_NULL so that retiring a Mood never destroys messages.
    mood = models.ForeignKey('Mood', models.SET_NULL, related_name='messages', null=True, blank=True)

    @property
    def mood_slug(self):
        """Its Mood's slug now (the key is its id, which never changes)."""
        return mood_slug(self.mood_id)

    # Threading - optional parent for message chains
    parent = models.ForeignKey('self', models.CASCADE, related_name='children', null=True, blank=True)
    looking_for_parent_id = models.UUIDField(null=True, blank=True, help_text='UUID of parent that was not found during import')

    # Participants
    sender = models.ForeignKey(ConversationParticipant, models.CASCADE, related_name='sent_messages')
    recipients = models.ManyToManyField(ConversationParticipant, related_name='received_messages')

    # Session context
    session_id = models.UUIDField(null=True, blank=True)

    # Temporal tracking
    timestamp = models.BigIntegerField(null=True, blank=True)
    eth_blockheight = models.BigIntegerField(null=True, blank=True)
    eth_block_offset = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    # Metadata
    model_backend = models.CharField(max_length=100, null=True, blank=True)
    effort = models.CharField(max_length=20, null=True, blank=True)  # e.g. 'high', 'xhigh', 'max'
    stop_reason = models.CharField(max_length=50, null=True, blank=True)
    source_file = models.CharField(max_length=255, null=True, blank=True)
    missing_from_markdown = models.BooleanField(default=False)

    # Usage tracking
    input_tokens = models.IntegerField(null=True, blank=True)
    output_tokens = models.IntegerField(null=True, blank=True)
    cache_creation_input_tokens = models.IntegerField(null=True, blank=True)
    cache_read_input_tokens = models.IntegerField(null=True, blank=True)

    # Flags
    is_sidechain = models.BooleanField(default=False)
    is_synthetic_error = models.BooleanField(default=False)  # Claude Code synthetic error response
    is_retry = models.BooleanField(default=False)  # User retry due to timeout/error
    is_continuation_message = models.BooleanField(default=False)  # System-injected summary at start of post-compact session
    apparent_duplicate = models.BooleanField(default=False, help_text='True if this message was encountered again during import with different content (likely whitespace differences)')

    # Environment context
    cwd = models.TextField(null=True, blank=True)
    git_branch = models.CharField(max_length=255, null=True, blank=True)
    client_version = models.CharField(max_length=50, null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=['session_id', 'timestamp']),
            models.Index(fields=['sender']),
            models.Index(fields=['mood', 'created_at']),
            # stamp_blockheights looks for these every few minutes; without
            # this each look is a full scan.
            models.Index(fields=['timestamp'], condition=models.Q(eth_blockheight__isnull=True),
                         name='message_unstamped_ts'),
        ]
        unique_together = [['context_heap', 'message_number']]

    def __str__(self):
        recipient_names = ','.join(r.name for r in self.recipients.all()) if self.pk else '?'
        return f"{self.sender}→{recipient_names} at {self.timestamp}: {str(heap.messages.all()[0].content)[:40]}"

    @property
    def has_children(self):
        """Check if this message has any children."""
        return hasattr(self, 'children') and self.children.exists()

    def get_descendants(self):
        """Recursively get all descendants of this message."""
        if not hasattr(self, 'children'):
            return []
        descendants = []
        for child in self.children.all():
            descendants.append(child)
            descendants.extend(child.get_descendants())
        return descendants
    
    def set_parent_id(self, parent_id):
        # Get parent if exists
        parent_uuid = uuid.UUID(parent_id)
        try:
            self.parent = Message.objects.get(id=parent_uuid)
            found = True
        except Message.DoesNotExist:
            # Parent doesn't exist yet - store UUID for later linking
            self.looking_for_parent_id = parent_uuid
            found = False
        self.save()
        return found

    @staticmethod
    def sanitize_content(content):
        """
        Remove null bytes and other problematic characters from content.
        PostgreSQL JSON fields cannot contain \u0000 (null bytes).
        """
        if isinstance(content, str):
            # Remove null bytes
            content = content.replace('\x00', '')
        elif isinstance(content, list):
            content = [Message.sanitize_content(item) for item in content]
        elif isinstance(content, dict):
            return {k: Message.sanitize_content(v) for k, v in content.items()}
        return content

    @classmethod
    def detect_event_type_claude_code_v2(cls, event_line):
        """
        Takes a line from a claude code vs JSONL file, returns its message type.
        """
        try:
            event = json.loads(event_line)
        except json.JSONDecodeError:
            raise

        if event['type'] == 'summary':
            event_type = "summary"
        elif event['type'] == 'system':
            if event['subtype'] == "compact_boundary":
                event_type = "compact_boundary"
            elif event['subtype']:
                event_type = "local_command"
            else:
                raise RuntimeError("What other system types are there?")
        elif event['type'] == 'file-history-snapshot':
            event_type = 'file-history-snapshot'
            event['uuid'] = event['messageId']
        elif 'message' not in event:
            # Session metadata (attachment, custom-title, agent-name, mode,
            # pr-link, queue-operation, ...), which newer clients write
            # alongside the conversation. Not a message; skip it quietly
            # rather than raise KeyError on every batch (#13).
            return EVENT_TYPE_WE_DO_NOT_HANDLE_YET, False
        else:
            message = event['message']
            role = message['role']
            content = message['content']

            # Check if content is a string (command messages, uncertain messages)
            if type(content) == str:
                # Check if it's a command pattern
                if event.get('isCompactSummary') or content.startswith("This session is being continued"):
                    # The summary a compacted session goes on from: newer
                    # clients write it as a plain string, and flag it.
                    event_type = "continuation"
                elif content.startswith("<command"):
                    event_type = "command"
                elif content.startswith("<local-command-stdout"):
                    event_type = "command result - success"
                else:
                    # It seems like this scenario can happen when a message wasn't successfully sent
                    # due to a client error or network problem.
                    event_type = "uncertain message"
                return event_type, event
            
            if content[0]['type'] == 'image':
                # TODO: Handle image attachments.
                return EVENT_TYPE_WE_DO_NOT_HANDLE_YET, False

            # Content is an array - check if it has multiple items
            if len(content) > 1:
                if content[-1]['type'] == "tool_use":
                    # This is a tool use with arbitrary amounts of thinking/text preamble before it
                    event_type = "tool use with preamble"
                elif content[-1]['type'] == "text":
                    # This is a response with thinking/deliberation before the final text
                    event_type = "thought-out response"
                elif content[0]['type'] == 'image':
                    # event_type = "message with image attachment"
                    event_type = EVENT_TYPE_WE_DO_NOT_HANDLE_YET
                else:
                    raise RuntimeError("Content has more than one member.  Not sure what to do.")
                return event_type, event

            first_content = content[0]

            if first_content['type'] == 'text':
                if first_content['text'].startswith("This session is being continued"):
                    # TODO: Modify participants?  #12
                    event_type = "continuation"
                elif first_content['text'].startswith("Caveat: The messages below were generated by the user"):
                    event_type = "caveat"
                elif first_content['text'].startswith("<command"):
                    event_type = "command"
                elif first_content['text'].startswith("<local-command-stdout"):
                    event_type = "command result - success"
                else:
                    event_type = "regular message"
            ######
            ### All the types other than "text", where we have some strucutred data to determine type.
            ######
            elif first_content['type'] == 'thinking':
                event_type = "thought"
            elif first_content['type'] == 'tool_use':
                event_type = "tool use"
            elif first_content['type'] == 'tool_result':
                event_type = "tool result"
            else:
                assert False

        return event_type, event
    
    def has_no_parent_wants_no_parent(self):
        return self.parent is None and self.looking_for_parent_id is None

    def highest_known_parent(self):
        parent = self.parent
        while True:
            if parent.parent is None:
                return parent
            else:
                parent = parent.parent
        return parent

    @classmethod
    def _store_raw_content(cls, message, json_data, extra_fields):
        """Helper to store raw JSON for a message."""
        from django.contrib.contenttypes.models import ContentType
        from conversations.models import RawImportedContent
        import uuid as uuid_lib

        # Sanitize raw_data to remove null bytes before storing
        sanitized_data = cls.sanitize_content(json_data)

        message_ct = ContentType.objects.get_for_model(message)
        RawImportedContent.objects.create(
            id=uuid_lib.uuid4(),
            content_type=message_ct,
            object_id=message.id,
            raw_data=sanitized_data,
            source_file_id=extra_fields.get('source_file_id')
        )


class Thought(Message):
    """
    Thinking message - represents the interal monologue of an AI ThinkingEntity.

    These are apparently sometimes signed (perhaps cryptographically?) by the vendor of LLM clients.
    """

    signature = models.TextField()

    def __str__(self):
        preview = str(self.content)[:50] + '...' if len(str(self.content)) > 50 else str(self.content)
        return f"[Thought] {preview}"


class ToolUse(Message):
    """
    Tool call message.

    Records when the assistant calls a tool with specific parameters.
    Links to ToolResult via tool_id.
    """

    tool_name = models.CharField(max_length=100)
    tool_id = models.CharField(max_length=100)  # "toolu_01Eu..." - not globally unique, only unique within conversation

    def __str__(self):
        return f"[ToolUse] {self.tool_name} ({self.tool_id})"

    def get_result(self):
        """Get the corresponding ToolResult message."""
        try:
            return ToolResult.objects.get(tool_use_id=self.tool_id)
        except ToolResult.DoesNotExist:
            return None


class ToolResult(Message):
    """
    Tool execution result.

    Links back to the ToolUse message via tool_use_id.
    Content contains output/stdout/stderr as JSON.
    """

    tool_use_id = models.CharField(max_length=100, db_index=True)  # Links to ToolUse.tool_id
    is_error = models.BooleanField(default=False)

    def __str__(self):
        status = "ERROR" if self.is_error else "OK"
        preview = str(self.content)[:50] + '...' if len(str(self.content)) > 50 else str(self.content)
        return f"[ToolResult] {status}: {preview}"

    def get_tool_use(self):
        """Get the corresponding ToolUse message."""
        try:
            return ToolUse.objects.get(tool_id=self.tool_use_id)
        except ToolUse.DoesNotExist:
            return None


# ============================================================================
# Compacting Action
# ============================================================================

class CompactingActionManager(models.Manager):
    """Custom manager for CompactingAction with smart lookup methods."""

    def get_or_create_by_id_or_message(self, id_or_message, **defaults):
        """
        Get or create a CompactingAction by either a UUID or Message object.

        This method intelligently searches for an existing CompactingAction:
        1. If a Message object is provided, checks if it has a related CompactingAction
        2. If a UUID is provided, checks for CAs with that ending_message or looking_for_ending_message
        3. If found and orphaned (looking_for_ending_message set), links the message
        4. If not found, creates a new CA with the provided defaults

        Args:
            id_or_message: Either a UUID object or a Message instance
            **defaults: Default field values for creating new CompactingAction

        Returns:
            tuple: (CompactingAction instance, created_bool)

        Examples:
            # By Message object
            ca, created = CompactingAction.objects.get_or_create_by_id_or_message(
                message_obj,
                compact_trigger='manual'
            )

            # By UUID
            ca, created = CompactingAction.objects.get_or_create_by_id_or_message(
                uuid.UUID('...'),
                compact_trigger='auto'
            )
        """
        from uuid import UUID

        if type(id_or_message) == str:
            id_or_message = UUID(id_or_message)

        # Determine if we have a Message or UUID
        if isinstance(id_or_message, Message):
            message = id_or_message
            message_id = message.id
        elif isinstance(id_or_message, UUID):
            message_id = id_or_message
            try:
                message = Message.objects.get(id=message_id)
            except Message.DoesNotExist:
                message = None
        else:
            raise TypeError(f"Expected Message or UUID, got {type(id_or_message)}")

        # Try to find existing CompactingAction
        compacting_action = None

        # First, check if there's a CA with this as its ending_message FK
        if message:
            try:
                compacting_action = self.get(ending_message=message)
            except self.model.DoesNotExist:
                pass

        # Second, check if there's an orphaned CA looking for this message ID
        if not compacting_action:
            try:
                compacting_action = self.get(looking_for_ending_message=message_id)
            except self.model.DoesNotExist:
                pass

        # If we found an orphaned CA and now have the message, link it
        if compacting_action and message:
            compacting_action.ending_message = message
            compacting_action.looking_for_ending_message = None  # Clear the orphan flag  # TODO: Do we want this?  Are we looking it up anywhere?
            compacting_action.context_heap = message.context_heap  # TODO: This doesn't seem DRY.  Not sure what else to do yet.
            compacting_action.save(update_fields=['ending_message', 'looking_for_ending_message'])

        # If we found a CA (orphaned or not), return it
        if compacting_action:
            created = False
        else:
            created = True

        if created:
            # No existing CA found - create a new one
            create_kwargs = defaults.copy()

            if message:
                # We have the message, set the FK
                create_kwargs['ending_message'] = message
                if message.context_heap:
                    create_kwargs['context_heap'] = message.context_heap  # Don't love it; this can be a method or something.
            else:
                # Message doesn't exist yet, mark as looking for it
                create_kwargs['looking_for_ending_message'] = message_id

            compacting_action = self.create(**create_kwargs)

        if compacting_action.ending_message:
                if not compacting_action.context_heap:
                    # TODO: Restore this assertion - is this an impossible siutaiton or not?
                    assert False
                    pass

        return compacting_action, created


class CompactingAction(models.Model):
    """
    Records when a context heap was closed via compacting.

    Points to the ContextHeap that was closed.
    Not all context heaps have a CompactingAction - some end naturally.

    context_heap can be null during import when we find summaries before
    we've imported the context heap they belong to.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    context_heap = models.OneToOneField(
        ContextHeap,
        models.CASCADE,
        null=True,
        blank=True,
        related_name='compacting_action'
    )

    ending_message = models.OneToOneField(
        Message,
        models.SET_NULL,
        null=True,
        blank=True,
        related_name='compacting_action_ending_here',
        db_column='ending_message_fk_id'  # Temporary name to avoid conflict during migration
    )
    looking_for_ending_message = models.UUIDField(null=True, blank=True, help_text='UUID of ending message that was not found during import')

    continuation_message = models.ForeignKey(
        Message,
        models.SET_NULL,
        null=True,
        blank=True,
        related_name='continuation_for_compacting_action'
    )  # The system-injected summary message at start of next heap
    compact_trigger = models.CharField(max_length=50, null=True, blank=True)
    pre_compact_tokens = models.IntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = CompactingActionManager()

    class Meta:
        db_table = 'compacting_actions'

    def __str__(self):
        trigger = self.compact_trigger or 'unknown'
        tokens = f"{self.pre_compact_tokens:,}" if self.pre_compact_tokens else '?'
        heap = f"heap {str(self.context_heap_id)[:8]}" if self.context_heap else "orphaned"
        return f"Compact ({trigger}, {tokens} tokens, {heap})"

    def get_boundary_message(self):
        """Get the message at the compact boundary (leaf message)."""
        if not self.compact_boundary_message_id:
            return None
        try:
            return Message.objects.get(id=self.compact_boundary_message_id)
        except Message.DoesNotExist:
            return None

    @classmethod
    def from_jsonl_claude_code_v2(cls, event, ending_message_id, summary, context_heap=None):
        """
        Create or get CompactingAction from Claude Code v2 summary data with deduplication.

        Generates deterministic UUID from summary data hash to prevent duplicates on reimport.

        Args:
            summary_data: Dict with keys like:
                - summary: Text summary of the compact
                - leafUuid: UUID of the compact boundary message
                - type: Should be 'summary'
            **extra_fields: Additional fields like context_heap, ending_message_id, continuation_message

        Returns:
            tuple: (compacting_action, created_bool)

        Example summary_data:
            {
                "type": "summary",
                "summary": "Discussion about memory systems and...",
                "leafUuid": "00000000-0000-0000-0000-000000000003"
            }
        """
        # Generate deterministic ID from hash of summary data
        canonical_json = json.dumps(event, sort_keys=True)
        hash_digest = hashlib.sha256(canonical_json.encode()).digest()
        ca_id = uuid.UUID(bytes=hash_digest[:16])

        try:
            # Did we already make a CompactingAction from a compact boundary?
            CompactingAction.objects.get(compact_boundary_message_id=ending_message_id)
            assert False
        except CompactingAction.DoesNotExist:
            pass
        
        if context_heap is not None:
            try:
                existing_compacting_action_for_this_heap = CompactingAction.objects.get(context_heap=context_heap)
            except CompactingAction.MultipleObjectsReturned:
                # We _already_ have more than one compacting action for this heap - this isn't supposed to be possible.
                existing_cas = CompactingAction.objects.filter(context_heap=context_heap)
                print(f"\n{'='*60}")
                print(f"ERROR: Heap {str(context_heap.id)[:8]} already has {existing_cas.count()} CompactingActions!")
                print(f"{'='*60}")
                for existing_ca in existing_cas:
                    print(f"  Existing CA {str(existing_ca.id)[:8]}:")
                    print(f"    ending_message_id: {str(existing_ca.ending_message_id)[:8] if existing_ca.ending_message_id else 'None'}")
                    print(f"    compact_boundary_message_id: {str(existing_ca.compact_boundary_message_id)[:8] if existing_ca.compact_boundary_message_id else 'None'}")
                    print(f"    summary: {existing_ca.summary[:80] if existing_ca.summary else 'None'}...")
                print(f"\nTrying to create NEW CA:")
                print(f"  ending_message_id: {str(ending_message_id)[:8] if ending_message_id else 'None'}")
                print(f"  summary from event: {event.get('summary', 'N/A')[:80]}...")
                print(f"\nHeap info:")
                print(f"  Messages in heap: {context_heap.messages.count()}")
                first = context_heap.first_message()
                last = context_heap.messages.order_by('message_number').last()
                if first:
                    print(f"  First message: {str(first.id)[:8]} (msg #{first.message_number})")
                if last:
                    print(f"  Last message: {str(last.id)[:8]} (msg #{last.message_number})")
                print(f"{'='*60}\n")
                raise RuntimeError("Multiple CompactingActions for same heap - see debug output above")
            except CompactingAction.DoesNotExist:
                existing_compacting_action_for_this_heap = None

        if context_heap and existing_compacting_action_for_this_heap:
            ####
            # TODO: This heap has probably already been compacted and we failed to start a new one.abs
            # There's probably a new one one here:
            print(f"ContextHeap {context_heap.id} is already compacted with {context_heap.messages.count()} messages.")
            print(f"...but {event} is triggering another compact for it.")
            
            if type(event) == dict:
                print(f"Summary is a dict: {event}")
            else:
                parent = event.parent
                print(f"Parent's heap is {parent.context_heap.id}")

            try:
                previous_compacting_action = event.parent.context_heap.compacting_action
                assert False
                #### Say something about previous compacting action
            except ContextHeap.compacting_action.RelatedObjectDoesNotExist:
                print("Parent's heap is not compacted - is that the heap we're supposed to be compacting?!")
            except AttributeError:
                #### The event is probably a dict (a summary) and doesn't even have a parent.
                pass

            existing_ender_id = existing_compacting_action_for_this_heap.ending_message_id
            new_ender_id = event['leafUuid']

            existing_ender = Message.objects.get(id=existing_ender_id)
            new_ender = Message.objects.get(id=new_ender_id)

            probably_a_heap_opener_in_here = context_heap.messages.all()

            # Check if both enders are in this heap
            existing_ender_in_heap = existing_ender in probably_a_heap_opener_in_here
            new_ender_in_heap = new_ender in probably_a_heap_opener_in_here

            print("#####################################")
            print(f"EXISTING ENDER: {str(existing_ender_id)[:8]} - msg #{existing_ender.message_number if existing_ender_in_heap else 'NOT IN HEAP'}")
            print(f"NEW ENDER:      {str(new_ender_id)[:8]} - msg #{new_ender.message_number if new_ender_in_heap else 'NOT IN HEAP'}")

            if existing_ender_in_heap and new_ender_in_heap:
                if existing_ender.message_number < new_ender.message_number:
                    print(f"EXISTING ENDER comes FIRST (msg #{existing_ender.message_number} < #{new_ender.message_number})")
                    print(f"→ New ender should probably start a new heap")
                else:
                    print(f"NEW ENDER comes FIRST (msg #{new_ender.message_number} < #{existing_ender.message_number})")
                    print(f"→ Existing ender should probably start a new heap")
            elif existing_ender_in_heap:
                print(f"Only EXISTING ENDER is in this heap")
            elif new_ender_in_heap:
                print(f"Only NEW ENDER is in this heap")
            else:
                print(f"NEITHER ender is in this heap?!")

            print(f"\n{len(probably_a_heap_opener_in_here)} events in heap - one of them is probably an unrecognized heap opener.")
            print("------------==========---------")
            for counter, possible_opener in enumerate(probably_a_heap_opener_in_here):
                marker = ""
                if possible_opener.id == existing_ender_id:
                    marker = " ← EXISTING ENDER"
                elif possible_opener.id == new_ender_id:
                    marker = " ← NEW ENDER"

                if possible_opener.parent is None:
                    marker += " [NO PARENT - POSSIBLE OPENER]"
                if possible_opener.is_continuation_message:
                    marker += " [CONTINUATION MSG]"

                # print('##################')
                # print(f'------EVENT {counter} ({possible_opener.__class__}) msg #{possible_opener.message_number}{marker}-------')
                # print(possible_opener.content)
                # print('##################')
            print("#####################################")
            raise ValueError("How can we compact a context heap that's already been compacted?")

        # Use get_or_create for deduplication
        compact, created = cls.objects.get_or_create(
            id=ca_id,
            defaults={
            "ending_message_id": ending_message_id,
            "summary": summary,
            "context_heap": context_heap
            }
        )

        # # Store raw JSONL data for debugging (only on creation)
        # if created:
        #     from django.contrib.contenttypes.models import ContentType
        #     compact_ct = ContentType.objects.get_for_model(compact)
        #     RawImportedContent.objects.create(
        #         id=uuid.uuid4(),
        #         content_type=compact_ct,
        #         object_id=compact.id,
        #         raw_data=event,
        #         source_file_id=extra_fields.get('source_file_id')
        #     )

        return compact, created

    def has_post_compact_messages(self):
        """Check if messages exist after the boundary in the same heap."""
        if not self.context_heap_id or not self.compact_boundary_message_id:
            return False

        boundary_msg = self.get_boundary_message()
        if not boundary_msg or not boundary_msg.message_number:
            return False

        # Check if any messages exist after the boundary in this heap
        post_compact_count = Message.objects.filter(
            context_heap_id=self.context_heap_id,
            message_number__gt=boundary_msg.message_number
        ).count()

        return post_compact_count > 0

    def split_heap(self):
        """
        Split the heap at the compact boundary.

        Creates a new POST_COMPACTING heap and moves all messages after
        the boundary to it. Returns the new heap or None if no split needed.
        """
        if not self.has_post_compact_messages():
            return None

        boundary_msg = self.get_boundary_message()
        old_heap = self.context_heap

        # Get all messages after the boundary
        post_compact_messages = Message.objects.filter(
            context_heap_id=old_heap.id,
            message_number__gt=boundary_msg.message_number
        ).order_by('message_number')

        if not post_compact_messages.exists():
            return None

        # Create new heap; its first_message() is derived from the moved messages
        new_heap = ContextHeap.objects.create(
            era=old_heap.era,
            type=ContextHeapType.POST_COMPACTING
        )

        # Move all post-compact messages to new heap
        # Reset message numbers starting from 1
        for i, msg in enumerate(post_compact_messages, start=1):
            msg.context_heap = new_heap
            msg.message_number = i
            msg.save()

        return new_heap


# ============================================================================
# Summary Model
# ============================================================================

class Summary(models.Model):
    """
    A summary generated during conversation compacting.

    These are separate from CompactingActions - they're just the AI-generated
    text summaries that appear in the JSONL export, stored for reference.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    summary_text = models.TextField()

    # Link to the actual leaf message if it exists
    leaf_message = models.ForeignKey(
        Message,
        models.SET_NULL,
        null=True,
        blank=True,
        related_name='summaries_ending_here'
    )
    looking_for_leaf_message = models.UUIDField(
        null=True,
        blank=True,
        help_text='UUID of the leaf message that was not found during import (from leafUuid)'
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'summaries'
        verbose_name_plural = 'summaries'

    def __str__(self):
        return f"Summary: {self.summary_text[:50]}..." if len(self.summary_text) > 50 else f"Summary: {self.summary_text}"


# ============================================================================
# Supporting Models
# ============================================================================

class Topic(models.Model):
    """A topic that can be tagged on messages."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=100, unique=True)
    category = models.CharField(max_length=50, default='misc')
    description = models.TextField(null=True, blank=True)

    class Meta:
        db_table = 'topics'
        ordering = ['name']

    def __str__(self):
        return self.name


class MessageTopic(models.Model):
    """Many-to-many relationship between messages and topics with relevance."""

    message_id = models.UUIDField()
    topic = models.ForeignKey(Topic, models.CASCADE)
    relevance = models.IntegerField(default=5, help_text="1-10 scale")

    class Meta:
        db_table = 'message_topics'
        unique_together = ['message_id', 'topic']

    def __str__(self):
        return f"{self.message_id}: {self.topic.name} ({self.relevance})"


class Note(models.Model):
    """
    Notes about various objects (messages, context windows, eras).

    Notes are authored by thinking entities (humans or AI) and can be attached to
    any model using generic foreign keys.

    Examples: import metadata, editorial comments, corrections, context about
    incomplete conversations, compacting decisions.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # Generic foreign key to attach notes to any model
    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE)
    object_id = models.UUIDField()
    about = GenericForeignKey('content_type', 'object_id')

    # Who wrote this note
    from_entity = models.ForeignKey(ThinkingEntity, models.CASCADE, related_name='authored_notes')

    content = models.TextField()
    eth_blockheight = models.BigIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'notes'
        indexes = [
            models.Index(fields=['content_type', 'object_id']),
        ]
        ordering = ['created_at']

    def __str__(self):
        preview = self.content[:50] + '...' if len(self.content) > 50 else self.content
        return f"Note by {self.from_entity}: {preview}"


class ConversationFile(models.Model):
    """Tracks which messages came from which conversation files."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    filename = models.CharField(max_length=255)
    file_path = models.TextField(null=True, blank=True)
    beginning_message_id = models.UUIDField(null=True, blank=True)
    ending_message_id = models.UUIDField(null=True, blank=True)
    checksum = models.CharField(max_length=64, null=True, blank=True)
    message_count = models.IntegerField(null=True, blank=True)
    imported_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'conversation_files'
        ordering = ['-imported_at']

    def __str__(self):
        return f"{self.filename} ({self.message_count} messages)"


class RawImportedContent(models.Model):
    """
    Stores raw imported data for debugging purposes.

    Can be attached to any model (Message, CompactingAction, etc.)
    to preserve the original import format for troubleshooting.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # Generic foreign key to attach to any object (nullable for orphaned content)
    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE, null=True, blank=True)
    object_id = models.UUIDField(null=True, blank=True)
    about = GenericForeignKey('content_type', 'object_id')

    # The raw data as imported
    raw_data = models.JSONField()

    # Import metadata
    source_file = models.ForeignKey(ConversationFile, models.SET_NULL, null=True, blank=True, related_name='raw_imports')
    imported_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'raw_imported_content'
        indexes = [
            models.Index(fields=['content_type', 'object_id']),
        ]

    def __str__(self):
        return f"Raw data for {self.content_type} {str(self.object_id)[:8]}"


class Device(models.Model):
    """
    A browser or phone allowed to write into Moods as one person.

    Reading Moods needs nothing. Writing needs a device, enrolled one of
    two ways, which decides its tier:

      key   the person's SSH key, the one hunter knows them by
            (services/mood_auth.py): everything, including what makes
            the machines act -- waking agents, starting and archiving Moods
      wiki  signing in with PickiPedia (services/wiki_auth.py): chatting,
            and mentioning people; an @agent from a wiki device is just text

    Only a hash of the token is stored; the token lives in the device's cookie.
    """
    TIERS = (('key', 'SSH key'), ('wiki', 'PickiPedia'))

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    entity = models.ForeignKey(ThinkingEntity, models.CASCADE, related_name='devices')
    label = models.CharField(max_length=100, blank=True)
    tier = models.CharField(max_length=10, choices=TIERS, default='key')
    token_hash = models.CharField(max_length=64, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'devices'

    def __str__(self):
        return f"{self.entity_id} on {self.label or 'a device'}"


class ReadMark(models.Model):
    """
    How far one person has read in one Mood: the newest moment they had it
    open. Kept here rather than in a browser, so a phone knows what the
    laptop read -- every device's unread counts start from the same place.
    Only ever moves forward.
    """

    entity = models.ForeignKey(ThinkingEntity, models.CASCADE, related_name='read_marks')
    mood = models.ForeignKey('Mood', models.CASCADE, related_name='read_marks')
    seen_at = models.DateTimeField()

    class Meta:
        db_table = 'read_marks'
        constraints = [models.UniqueConstraint(fields=['entity', 'mood'], name='one_read_mark_per_person_per_mood')]


class PushSubscription(models.Model):
    """
    Where to send one device's notifications with magenta closed: the
    browser's Web Push endpoint and the keys to encrypt for it
    (services/push.py). Made when its person turns the bell on there; gone
    when they turn it off, or the push service says it's gone.
    """

    device = models.ForeignKey(Device, models.CASCADE, related_name='push_subscriptions')
    endpoint = models.TextField(unique=True)
    p256dh = models.CharField(max_length=200)
    auth = models.CharField(max_length=100)
    created_at = models.DateTimeField(auto_now_add=True)
    last_sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'push_subscriptions'

    def __str__(self):
        return f"push to {self.device}"


class LoginCode(models.Model):
    """A one-time link that turns a proven SSH signature into a Device."""

    code_hash = models.CharField(max_length=64, primary_key=True)
    entity = models.ForeignKey(ThinkingEntity, models.CASCADE, related_name='login_codes')
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'login_codes'


class BlockAnchor(models.Model):
    """
    A real Ethereum block and its timestamp, fetched from a node.

    Sparse on purpose: eth_blockheight elsewhere is interpolated between
    anchors (see conversations/services/eth_blocks.py), so the record never
    needs a node at ingest time.
    """

    number = models.BigIntegerField(primary_key=True)
    timestamp = models.BigIntegerField(db_index=True, help_text='Unix seconds')

    class Meta:
        db_table = 'block_anchors'
        ordering = ['number']

    def __str__(self):
        return f"Block {self.number} @ {self.timestamp}"


class Media(models.Model):
    """
    An image in a Mood, stored once by the hash of its bytes.

    Posted from the composer, pasted into a terminal session, or returned
    by a tool (a screenshot): messages refer to it by URL,
    /moods/media/<sha256>.<ext>, so the record's text stays text. Only
    raster formats, checked by their first bytes, never by what the
    uploader says they are.
    """

    sha256 = models.CharField(max_length=64, primary_key=True)
    mime = models.CharField(max_length=40)
    data = models.BinaryField()
    size = models.IntegerField()
    added_by = models.ForeignKey(ThinkingEntity, models.SET_NULL, null=True, blank=True, related_name='media')
    created_at = models.DateTimeField(auto_now_add=True)
    # What whoever shared it lets others do with it (services/media.LICENSES):
    # CC BY-SA 4.0 unless they made it CC0, said where it's attached.
    license = models.CharField(max_length=20, default='cc-by-sa-4.0')

    class Meta:
        db_table = 'media'

    EXTENSIONS = {'image/png': 'png', 'image/jpeg': 'jpg', 'image/gif': 'gif', 'image/webp': 'webp',
                  # Voice memos, and messages read aloud (services/voice.py).
                  'audio/webm': 'webm', 'audio/ogg': 'ogg', 'audio/mp4': 'm4a', 'audio/mpeg': 'mp3',
                  'audio/wav': 'wav',
                  # A Yarn clip, kept here (services/yarn_kept.py).
                  'video/mp4': 'mp4'}

    @property
    def url(self):
        return f'/moods/media/{self.sha256}.{self.EXTENSIONS[self.mime]}'


class YarnClip(models.Model):
    """A Yarn clip kept here: its id on Yarn, and the copy of its video stored as Media.
    Fetched once, by the server (services/yarn_kept.py), so pressing ▶ doesn't send a
    browser to Yarn, whose Cloudflare stops people with a human check."""

    clip = models.UUIDField(primary_key=True)
    media = models.ForeignKey(Media, models.CASCADE, related_name='yarn_clips')
    # Its captioned GIF, the card's picture: kept too, as Yarn's Cloudflare stops it as it stops the video.
    still = models.ForeignKey(Media, models.SET_NULL, null=True, blank=True, related_name='yarn_stills')
    kept_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'yarn_clips'


class MessageChange(models.Model):
    """An edit to something said in a Mood, or its deletion: who, and when --
    never what it said before (services/retract.py). The newest one stands."""

    message = models.OneToOneField(Message, models.CASCADE, primary_key=True, related_name='change')
    mood = models.ForeignKey('Mood', models.CASCADE, related_name='message_changes')
    kind = models.CharField(max_length=10)  # 'edited' or 'deleted'
    by = models.CharField(max_length=50)
    at = models.DateTimeField(auto_now=True, db_index=True)
    # Where it had already gone that can't be called back: agents a wake carried it to.
    reached = models.JSONField(default=list, blank=True)

    class Meta:
        db_table = 'message_changes'


class SealedCopy(models.Model):
    """What an edit or deletion took out of a Mood, sealed to the recovery key
    (services/sealing.py): the server can't read it, nothing here deletes it,
    and whoever holds the key's private half can put it back (manage.py unseal,
    or the page). Who took it back and when are plain, so takebacks can be
    counted and limited. `digest` is the SHA-256 of exactly what was sealed --
    a random salt inside, so it gives nothing away -- and lets the page put
    back what it opened: only the very words that were sealed match it."""

    message_id = models.UUIDField(db_index=True)  # not a key: kept whatever happens to the message
    mood_slug = models.CharField(max_length=100)
    kind = models.CharField(max_length=10)  # 'deleted', 'edited', or 'replaced' (by a putting back)
    by = models.CharField(max_length=50, db_index=True)
    at = models.DateTimeField(auto_now_add=True, db_index=True)
    sealed = models.TextField()
    digest = models.CharField(max_length=64)

    class Meta:
        db_table = 'sealed_copies'


class Setting(models.Model):
    """
    One change to a knob: what was set, for which agent in which Mood, by whom.

    The newest row for a (mood, agent, key) is in force; the rest are its
    history, and so the audit trail -- rows are only ever added. A null
    mood means every Mood; a null agent means every agent (or, for a
    moderation key like 'banned', the person it names is in `agent`). See
    conversations/services/settings.py for the knobs and how they resolve.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    mood = models.ForeignKey('Mood', models.CASCADE, null=True, blank=True, related_name='settings')
    agent = models.ForeignKey(ThinkingEntity, models.CASCADE, null=True, blank=True, related_name='settings')
    key = models.CharField(max_length=40)
    value = models.JSONField()
    set_by = models.ForeignKey(ThinkingEntity, models.SET_NULL, null=True, blank=True, related_name='settings_set')
    note = models.CharField(max_length=200, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'settings'
        indexes = [models.Index(fields=['key', 'mood', 'agent', 'created_at'])]

    def __str__(self):
        return f"{self.key}={self.value!r} ({self.mood_id or '*'}/{self.agent_id or '*'})"


# ============================================================================
# Import Tracking Constants
# ============================================================================

# Model types to track during imports (used by import commands and tests)
TYPES_TO_TRACK = (Message, ContextHeap, ThinkingEntity, CompactingAction, ToolUse, ToolResult, Thought)
