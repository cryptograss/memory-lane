import json
import uuid as uuid_lib
import re
from pathlib import Path
from datetime import datetime
from django.core.management.base import BaseCommand
from django.utils.dateparse import parse_datetime
from django.utils import timezone
from conversations.models import (
    Era, ContextHeap, ContextHeapType,
    Message, Thought, ToolUse, ToolResult, ThinkingEntity,
    ConversationParticipant,
    CompactingAction, MotionSession
)
from constant_sorrow.constants import EVENT_TYPE_WE_DO_NOT_HANDLE_YET


def get_or_create_participant(name, participant_type):
    """
    Get or create a ConversationParticipant.

    Args:
        name: Participant name
        participant_type: One of: 'human', 'ai', 'tool', 'oracle', 'system'

    Returns:
        ConversationParticipant instance
    """
    participant, created = ConversationParticipant.objects.get_or_create(
        name=name,
        defaults={'participant_type': participant_type}
    )
    return participant

def parse_command_xml(text):
    """Parse XML command patterns into structured data."""

    # Check for command invocation
    command_name_match = re.search(r'<command-name>(.+?)</command-name>', text)
    if command_name_match:
        command_message_match = re.search(r'<command-message>(.+?)</command-message>', text)
        command_args_match = re.search(r'<command-args>(.*?)</command-args>', text, re.DOTALL)

        return {
            'type': 'slash_command',
            'command_name': command_name_match.group(1),
            'command_message': command_message_match.group(1) if command_message_match else '',
            'command_args': command_args_match.group(1).strip() if command_args_match else '',
            'raw_xml': text
        }

    # Check for command output
    stdout_match = re.search(r'<local-command-stdout>(.*?)</local-command-stdout>', text, re.DOTALL)
    if stdout_match:
        return {
            'type': 'command_output',
            'stdout': stdout_match.group(1),
            'raw_xml': text
        }

    # Not a recognized pattern, return as plain text
    return text


def handle_summary(event, filename):
        """
        Handle summary events from JSONL.

        Creates a Summary record for each summary event. These are just stored
        for reference and display - they don't affect heap structure.

        Args:
            event: Summary event dict with keys:
                - 'summary': The summary text
                - 'leafUuid': UUID of the last message before compact
            filename: Source filename (for logging)

        Returns:
            tuple: (Summary instance, created_bool)
        """
        from conversations.models import Summary, Message

        leaf_uuid = uuid_lib.UUID(event['leafUuid'])
        summary_text = event['summary']

        # Try to find the leaf message
        try:
            leaf_message = Message.objects.get(id=leaf_uuid)
        except Message.DoesNotExist:
            leaf_message = None

        # Check if we already have a Summary for this leaf
        # First check by FK, then by looking_for
        try:
            if leaf_message:
                summary = Summary.objects.get(leaf_message=leaf_message)
                created = False
            else:
                summary = Summary.objects.get(looking_for_leaf_message=leaf_uuid)
                created = False
        except Summary.DoesNotExist:
            # Create new Summary
            if leaf_message:
                summary = Summary.objects.create(
                    summary_text=summary_text,
                    leaf_message=leaf_message
                )
            else:
                summary = Summary.objects.create(
                    summary_text=summary_text,
                    looking_for_leaf_message=leaf_uuid
                )
            created = True

        # If we found an orphaned Summary and now have the message, link it
        if not created and leaf_message and summary.looking_for_leaf_message and not summary.leaf_message:
            summary.leaf_message = leaf_message
            summary.looking_for_leaf_message = None
            summary.save(update_fields=['leaf_message', 'looking_for_leaf_message'])

        return summary, created


def extract_timestamp(event):
    """Extract and parse timestamp from event, return as Unix timestamp (milliseconds)."""
    timestamp_str = event.get('timestamp')
    if timestamp_str:
        dt = parse_datetime(timestamp_str)
        if dt:
            # Convert to Unix timestamp in milliseconds
            return int(dt.timestamp() * 1000)
    return None

def import_line_from_claude_code_v2(line, era, filename, username='justin'):

        # Get entities
        # Get the user's ThinkingEntity (create if doesn't exist)
        user, _ = ThinkingEntity.objects.get_or_create(
            name=username,
            defaults={'is_biological_human': True}
        )
        # magent is always the AI assistant
        magent, _ = ThinkingEntity.objects.get_or_create(
            name='magent',
            defaults={'is_biological_human': False}
        )

        event_type, event = Message.detect_event_type_claude_code_v2(line)

        if event_type == EVENT_TYPE_WE_DO_NOT_HANDLE_YET:
            return EVENT_TYPE_WE_DO_NOT_HANDLE_YET, False

        # Extract timestamp (common to all message types)
        timestamp = extract_timestamp(event)

        # Fields every message carries, extracted once.
        #
        # These were being dropped: each get_or_create below listed its own
        # defaults and none of them included session_id, so 99.6% of the
        # corpus has no session at all and nothing could be grouped by
        # conversation. `motion` is resolved here too, so a message lands in
        # its Motion as it arrives rather than waiting for a later pass.
        session_id = event.get('sessionId')
        common = {
            'session_id': session_id,
            # A subagent's transcript shares its parent's sessionId, and its
            # prompts (written by the agent) arrive as user-role lines. Only
            # this flag tells them apart from the human's own words.
            'is_sidechain': bool(event.get('isSidechain')),
            'cwd': event.get('cwd'),
            'git_branch': event.get('gitBranch'),
            'client_version': event.get('version'),
            'motion': MotionSession.motion_for(session_id),
            'created_at': timezone.now(),
        }

        if event_type == "summary":
                compacting_action, created = handle_summary(event, filename)
                return compacting_action, created

        if event_type == "compact_boundary":
            # Extract compact metadata
            compact_metadata = event.get('compactMetadata', {})
            boundary_uuid = uuid_lib.UUID(event['uuid'])
            logical_parent_uuid = event.get('logicalParentUuid')

            # Create or update CompactingAction
            # TODO: Does this close a heap?  TODO: This is probably resolved by the logic of the caller.  Is it?
            compacting_action, created = CompactingAction.objects.get_or_create_by_id_or_message(
                id_or_message=logical_parent_uuid,
                compact_trigger = compact_metadata.get('trigger'),
                pre_compact_tokens = compact_metadata.get('preTokens'),
            )

            return compacting_action, created

        # Get UUID
        msg_uuid = uuid_lib.UUID(event['uuid'])

        # Create appropriate message type based on event_type
        if event_type == "thought":
            sender = magent  # TODO: #12
            content = event['message']['content']
            signature = content[0]['signature']
            message, created = Thought.objects.get_or_create(
                id=msg_uuid,
                defaults={
                    'sender': sender,
                    'source_file': filename,
                    'content': content,
                    'signature': signature,
                    'timestamp': timestamp,
                    **common,
                }
            )
            # Thoughts are internal deliberation - magent talking to self
            if created:
                message.recipients.add(magent)
        elif event_type == "tool use":
            if event['type'] == "assistant" and event['userType'] == "external":
                sender = magent
            else:
                assert False

            content_items = event['message']['content']
            tool_use_item = content_items[0]  # Single tool_use in content array

            message, created = ToolUse.objects.get_or_create(
                id=msg_uuid,
                defaults={
                    'sender': sender,
                    'source_file': filename,
                    'tool_name': tool_use_item.get('name', ''),
                    'tool_id': tool_use_item.get('id', ''),
                    'content': tool_use_item.get('input', {}),
                    'timestamp': timestamp,
                    **common,
                }
            )
            # Tool use is magent invoking a tool
            if created:
                tool_participant = get_or_create_participant(tool_use_item.get('name', 'unknown-tool'), 'tool')
                message.recipients.add(tool_participant)

        elif event_type == "tool use with preamble":
            if event['type'] == "assistant" and event['userType'] == "external":
                sender = magent
            else:
                assert False

            content_items = event['message']['content']
            tool_use_item = content_items[-1]  # Last item is always the tool_use

            # Collect all thinking and text items that came before
            preamble = {
                'thinking': [],
                'text': []
            }
            for item in content_items[:-1]:
                if item['type'] == 'thinking':
                    preamble['thinking'].append(item.get('thinking', ''))
                elif item['type'] == 'text':
                    preamble['text'].append(item.get('text', ''))

            # Store tool input and preamble in content field
            content = {
                'tool_input': tool_use_item.get('input', {}),
                'preamble': preamble
            }

            message, created = ToolUse.objects.get_or_create(
                id=msg_uuid,
                defaults={
                    'sender': sender,
                    'source_file': filename,
                    'tool_name': tool_use_item.get('name', ''),
                    'tool_id': tool_use_item.get('id', ''),
                    'content': content,
                    'timestamp': timestamp,
                    **common,
                }
            )
            # Tool use is magent invoking a tool
            if created:
                tool_participant = get_or_create_participant(tool_use_item.get('name', 'unknown-tool'), 'tool')
                message.recipients.add(tool_participant)

        elif event_type == "thought-out response":
            if event['type'] == "assistant" and event['userType'] == "external":
                # Earlier format - with type as assistant?
                sender = magent
            elif event['type'] == "user" and event['userType'] == "external":
                # TODO: What's different here that caused this to be "user" - still seems to be a thought and response.
                sender = magent
            else:
                assert False

            content_items = event['message']['content']
            final_text = content_items[-1]['text']  # Last item is the actual response text

            # Collect all thinking items that came before
            preamble = {
                'thinking': []
            }
            for item in content_items[:-1]:
                if item['type'] == 'thinking':
                    preamble['thinking'].append(item.get('thinking', ''))

            # Store response text and thinking preamble in content field
            content = {
                'text': final_text,
                'preamble': preamble
            }

            message, created = Message.objects.get_or_create(
                id=msg_uuid,
                defaults={
                    'sender': sender,
                    'source_file': filename,
                    'content': content,
                    'timestamp': timestamp,
                    **common,
                }
            )
            # Thought-out response is magent responding to user
            if created:
                message.recipients.add(user)

        elif event_type == "tool result":
            # Tool result comes from the tool itself, not from a thinking entity
            # We'll need to look up the tool name from the parent ToolUse
            # For now, use a generic participant - we'll refine this when we link parent/child
            sender = get_or_create_participant('tool-result', 'tool')

            message, created = ToolResult.objects.get_or_create(
                id=msg_uuid,
                defaults={
                    'sender': sender,
                    'source_file': filename,
                    'content': event.get('content', ''),
                    'is_error': event.get('is_error', False),
                    'tool_use_id': event.get('tool_use_id', ''),
                    'timestamp': timestamp,
                    **common,
                }
            )
            # Tool result goes back to magent
            if created:
                message.recipients.add(magent)

        elif event_type == "continuation":
            # sender and recipient are both magent, like a thought.
            message, created = Message.objects.get_or_create(
                id=msg_uuid,
                defaults={
                    'sender': magent,
                    'source_file': filename,
                    'content': event['message']['content'],
                    'is_continuation_message': True,
                    'timestamp': timestamp,
                    **common,
                }
            )
            # Continuation is magent to user (resuming after compact)
            if created:
                message.recipients.add(user)
        elif event_type == "regular message":
            role = event['message']['role']
            content = event['message']['content']

            #### This block is clearly broken - we need real logic for this.
            if role == 'user':
                sender = user
                recipient = magent
            elif role == 'assistant':
                sender = magent
                recipient = user
            else:
                assert False

            message, created = Message.objects.get_or_create(
                id=msg_uuid,
                defaults={
                    'sender': sender,
                    'source_file': filename,
                    'content': content,
                    'timestamp': timestamp,
                    **common,
                }
            )

            # Add recipient
            if created:
                message.recipients.add(recipient)

        elif event_type == "uncertain message":
            # TODO: #12
            role = event['message']['role']
            content = event['message']['content']
            if role == "user":
                sender = user
                recipient = magent
            else:
                assert False # Not sure what this can be?

            # TODO: Gracefully handle these situations (which probably arise from client errors or network problems)
            message, created = Message.objects.get_or_create(
                id=msg_uuid,
                defaults={
                    'sender': sender,
                    'source_file': filename,
                    'content': content,
                    'timestamp': timestamp,
                    **common,
                }
            )
            # Add recipient for uncertain messages
            if created:
                message.recipients.add(recipient)
        elif event_type == "caveat":
            content = event['message']['content']

            if len(content) > 1:
                assert False # We don't really have a plan here either.

            message, created = Message.objects.get_or_create(
                id=msg_uuid,
                defaults={
                    'sender': magent,
                    'source_file': filename,
                    'content': content[0]['text'],
                    'timestamp': timestamp,
                    **common,
                }
            )
            message.recipients.add(magent)
        elif event_type in ("command", "command result - success"):
            # Parse command XML from event content
            content = event.get('message', {}).get('content', '')
            # Content can be a string or an array with text dict
            if isinstance(content, str):
                text_content = content
            else:
                text_content = content[0].get('text', '') if content else ''
            parsed_content = parse_command_xml(text_content)

            # Determine sender and recipient based on message content
            if isinstance(parsed_content, dict):
                if parsed_content.get('type') == 'slash_command':
                    # Slash command invocation - from user to SlashCommand tool
                    sender = user
                    recipient = get_or_create_participant('SlashCommand', 'tool')
                elif parsed_content.get('type') == 'command_output':
                    # Command output - from system stdout back to user
                    sender = get_or_create_participant('stdout', 'system')
                    recipient = user
                else:
                    # Meta caveat message - from magent to magent
                    sender = magent
                    recipient = magent
            else:
                # Plain text meta message
                sender = magent
                recipient = magent

            message, created = Message.objects.get_or_create(
                id=msg_uuid,
                defaults={
                    'sender': sender,
                    'source_file': filename,
                    'content': parsed_content,
                    'timestamp': timestamp,
                    **common,
                }
            )

            # Add recipient
            if created:
                message.recipients.add(recipient)
        elif event_type == 'file-history-snapshot':
            pass # TODO: Preserve this somehow.
            return EVENT_TYPE_WE_DO_NOT_HANDLE_YET, False
        elif event_type == 'local_command':
            # Store local_command events to preserve parent chains.
            # These are system events like "Status dialog dismissed" that serve as
            # bridge nodes between user messages. Without them, the parent chain breaks.
            sender = get_or_create_participant('system', 'system')
            content = event.get('content', '')

            message, created = Message.objects.get_or_create(
                id=msg_uuid,
                defaults={
                    'sender': sender,
                    'source_file': filename,
                    'content': content,
                    'timestamp': timestamp,
                    **common,
                }
            )
            if created:
                message.recipients.add(user)
        else:
            assert False
            self.stdout.write(self.style.WARNING(f'Unknown event type: {event_type}'))

        if common['is_sidechain'] and not created and not message.is_sidechain:
            # Imported before isSidechain was read; a replay corrects it.
            Message.objects.filter(id=message.id).update(is_sidechain=True)
            message.is_sidechain = True

        apparent_parent_id = event['parentUuid']
        if apparent_parent_id is not None:
            message.set_parent_id(apparent_parent_id)

        return message, created