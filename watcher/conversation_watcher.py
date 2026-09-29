#!/usr/bin/env python3
"""
Live conversation watcher for Claude Code JSONL logs.

Monitors JSONL files for new lines and imports them in real-time.
"""

import os
import sys
import time
import logging
import json
import requests
import threading
from pathlib import Path
from logging.handlers import RotatingFileHandler
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# Add magenta directory to path and change to it
MAGENTA_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(MAGENTA_DIR))
os.chdir(MAGENTA_DIR)

# Set up logging
LOG_DIR = MAGENTA_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)
LOG_FILE = LOG_DIR / "watcher.log"

# Create logger
logger = logging.getLogger("watcher")
logger.setLevel(logging.INFO)

# Console handler (for tmux/screen viewing)
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
console_formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
console_handler.setFormatter(console_formatter)

# File handler with rotation (max 10MB, keep 5 old files)
file_handler = RotatingFileHandler(LOG_FILE, maxBytes=10*1024*1024, backupCount=5)
file_handler.setLevel(logging.DEBUG)
file_formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(filename)s:%(lineno)d - %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
file_handler.setFormatter(file_formatter)

logger.addHandler(console_handler)
logger.addHandler(file_handler)

# Django setup is deferred until needed (for remote mode, we don't need DB access)
_django_initialized = False

def init_django():
    """Initialize Django if not already done. Only needed for local mode."""
    global _django_initialized
    if not _django_initialized:
        import django
        os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'memory_viewer.settings')
        django.setup()
        _django_initialized = True


class ConversationWatcher(FileSystemEventHandler):
    """Watch JSONL files and import new messages."""

    def __init__(self, watch_dir, era, remote_endpoint=None, batch_size=10, batch_interval=2.0):
        """
        Initialize watcher.

        Args:
            watch_dir: Directory to watch (e.g., /project-logs/justin/)
            era: Era instance to import into (can be None if using remote mode)
            remote_endpoint: Optional URL to POST lines to (e.g., https://memory-lane.maybelle.cryptograss.live/api/ingest/)
            batch_size: Number of lines to batch before sending (for remote mode)
            batch_interval: Max seconds to wait before flushing batch
        """
        self.watch_dir = Path(watch_dir)
        self.era = era
        self.file_positions = {}  # Track last position read for each file
        self.current_heap = None  # Track current heap for edge cases

        # Remote mode settings
        self.remote_endpoint = remote_endpoint
        self.batch_size = batch_size
        self.batch_interval = batch_interval
        self.pending_lines = []  # Buffer for batching
        self.last_flush_time = time.time()
        # Two threads touch pending_lines: the filesystem-event thread appends
        # (on_modified -> import_line) and the main loop's timer flushes. The
        # lock makes swapping the batch out atomic, so a line appended while a
        # POST is in flight is never wiped by the post-flush clear. That wipe
        # silently dropped 2 of 12 human prompts in one six-hour window.
        self.batch_lock = threading.Lock()
        # Undelivered batches are re-queued rather than dropped; this bounds
        # memory if the endpoint stays down for a long time.
        self.max_pending = 5000

        # Extract username from watch directory path
        # Expected format: /project-logs/username/...
        parts = self.watch_dir.parts
        if 'project-logs' in parts:
            idx = parts.index('project-logs')
            if idx + 1 < len(parts):
                self.username = parts[idx + 1]
            else:
                self.username = 'unknown'
        else:
            # Fallback - try to extract from path
            self.username = parts[-2] if len(parts) >= 2 else 'unknown'

        logger.info(f"Watcher initialized for {watch_dir} (user: {self.username})")
        if remote_endpoint:
            logger.info(f"Remote mode: POSTing to {remote_endpoint}")
        elif era:
            logger.info(f"Local mode: Importing into era: {era.name} ({era.id})")

    def on_modified(self, event):
        """Handle file modification events."""
        if event.is_directory:
            return

        filepath = Path(event.src_path)

        # Only watch .jsonl files
        if filepath.suffix != '.jsonl':
            return

        logger.debug(f"File modified: {filepath.name}")
        self.process_new_lines(filepath)

    def process_new_lines(self, filepath):
        """Process new lines added to file since last read."""
        # Get last known position
        last_position = self.file_positions.get(str(filepath), 0)

        try:
            with open(filepath, 'r') as f:
                # Seek to last position
                f.seek(last_position)

                # Read new lines
                line_count = 0
                for line in f:
                    line = line.strip()
                    if not line:
                        continue

                    line_count += 1
                    try:
                        self.import_line(line, filepath.name)
                    except KeyError as e:
                        # Unknown format - save as raw data for later analysis
                        self.save_unparseable_line(line, filepath.name, str(e))
                    except Exception as e:
                        logger.error(f"Error importing line from {filepath.name}: {e}", exc_info=True)
                        # Also save this as unparseable
                        self.save_unparseable_line(line, filepath.name, str(e))

                # Update position
                self.file_positions[str(filepath)] = f.tell()

                if line_count > 0:
                    logger.info(f"Processed {line_count} new lines from {filepath.name}")

        except Exception as e:
            logger.error(f"Error reading file {filepath}: {e}", exc_info=True)

    def save_unparseable_line(self, line, filename, error_msg):
        """
        Save unparseable content for later analysis.

        Args:
            line: JSONL line that couldn't be parsed
            filename: Source filename
            error_msg: Error message
        """
        # Skip saving if in remote mode (let remote handle it)
        if self.remote_endpoint:
            logger.warning(f"Unparseable line from {filename} (error: {error_msg}) - skipping in remote mode")
            return

        init_django()
        from conversations.models import RawImportedContent
        from django.contrib.contenttypes.models import ContentType
        import json

        try:
            # Try to get UUID if present
            data = json.loads(line)
            uuid_str = data.get('uuid', None)

            # Save as RawImportedContent without linking to a specific object
            raw = RawImportedContent.objects.create(
                raw_data=data,
                content_type=None,
                object_id=None
            )
            logger.warning(f"Unparseable line saved as RawImportedContent {raw.id} from {filename} (error: {error_msg})")
        except Exception as e:
            logger.error(f"Failed to save unparseable line from {filename}: {e}")

    def import_line(self, line, filename):
        """
        Import a single JSONL line.

        Args:
            line: JSONL line string
            filename: Source filename
        """
        if self.remote_endpoint:
            # Remote mode: batch lines and POST to endpoint
            with self.batch_lock:
                self.pending_lines.append(line)
                full = len(self.pending_lines) >= self.batch_size

            # Flush if batch is full or enough time has passed
            if full or time.time() - self.last_flush_time > self.batch_interval:
                self.flush_batch()
        else:
            # Local mode: import directly to database
            self._import_line_local(line, filename)

    def _import_line_local(self, line, filename):
        """Import line directly to local database."""
        init_django()
        from constant_sorrow.constants import EVENT_TYPE_WE_DO_NOT_HANDLE_YET
        from conversations.models import Message
        from importers_and_parsers.claude_code_v2 import import_line_from_claude_code_v2
        from watcher.heap_assignment import assign_heap_to_message

        # Parse and create message using existing logic
        event, created = import_line_from_claude_code_v2(line, self.era, filename, self.username)

        # Check if this is an event type we don't handle yet
        if event is EVENT_TYPE_WE_DO_NOT_HANDLE_YET:
            logger.debug(f"Skipping unhandled event type from {filename}")
            return

        if not created:
            logger.debug(f"Already imported: {event.id}")
            return

        # If it's a Message (not CompactingAction or Summary), assign heap
        if isinstance(event, Message):
            heap = assign_heap_to_message(event, self.era, self.current_heap)
            self.current_heap = heap  # Update current heap tracker
            logger.debug(f"Imported message {str(event.id)[:8]} → heap {str(heap.id)[:8]}")
        else:
            logger.info(f"Imported {event.__class__.__name__} {str(event.id)[:8]}")

    def flush_batch(self):
        """Send batched lines to remote endpoint.

        The batch is swapped out under the lock before the POST, so lines
        appended by the other thread during the request land in a fresh list
        and go out next time. An undelivered batch goes back to the front of
        the queue and is retried at the next flush interval.
        """
        with self.batch_lock:
            batch, self.pending_lines = self.pending_lines, []
        if not batch:
            return

        # Build headers with optional API key auth
        headers = {}
        api_key = os.environ.get('INGEST_API_KEY')
        if api_key:
            headers['Authorization'] = f'Bearer {api_key}'

        delivered = False
        try:
            response = requests.post(
                self.remote_endpoint,
                json={
                    'lines': batch,
                    'username': self.username,
                    'source': f'hunter-watcher-{self.username}'
                },
                headers=headers,
                timeout=30
            )

            if response.status_code == 200:
                delivered = True
                result = response.json()
                errors = result.get('errors') or []
                # The endpoint returns at most 10 errors, so this count is a floor.
                logger.info(f"Remote ingest: sent={len(batch)}, imported={result.get('imported', 0)}, "
                            f"skipped={result.get('skipped', 0)}, errors={len(errors)}")
                if errors:
                    logger.warning(f"Remote ingest errors (first 3 of {len(errors)}): {errors[:3]}")
            else:
                logger.error(f"Remote ingest failed: {response.status_code} - {response.text[:200]}")

        except requests.RequestException as e:
            logger.error(f"Failed to POST to remote endpoint: {e}")

        if not delivered:
            self.requeue(batch)

        self.last_flush_time = time.time()

    def requeue(self, batch):
        """Put an undelivered batch back at the front of the queue.

        Bounded by max_pending: if the endpoint is down long enough to exceed
        it, the oldest lines are dropped, and loudly. Everything dropped is
        still in the JSONL on disk and can be replayed.
        """
        with self.batch_lock:
            self.pending_lines = batch + self.pending_lines
            overflow = len(self.pending_lines) - self.max_pending
            if overflow > 0:
                logger.error(f"Pending queue over {self.max_pending}; dropping {overflow} oldest lines")
                self.pending_lines = self.pending_lines[overflow:]
            pending = len(self.pending_lines)
        logger.warning(f"Re-queued {len(batch)} undelivered lines; {pending} pending")

    def scan_existing_files(self):
        """Scan existing files to establish baseline positions.

        Normally only lines written from now on are ingested. With
        WATCHER_REPLAY_FROM_START set, every existing line is ingested first.
        The importer is idempotent by message uuid, so a replay recovers
        anything a previous watcher dropped and duplicates nothing; the cost
        is a burst of POSTs whose results are mostly 'skipped'.
        """
        replay = os.environ.get('WATCHER_REPLAY_FROM_START', '').lower() in ('1', 'true', 'yes')
        logger.info("Scanning existing files..." + (" REPLAY: ingesting every existing line" if replay else ""))

        file_count = 0
        for filepath in self.watch_dir.rglob('*.jsonl'):
            if replay:
                self.file_positions[str(filepath)] = 0
                self.process_new_lines(filepath)
            else:
                # Just seek to end - we only want NEW lines from this point forward
                with open(filepath, 'r') as f:
                    f.seek(0, 2)  # Seek to end
                    self.file_positions[str(filepath)] = f.tell()

            logger.debug(f"Tracking {filepath.name} from position {self.file_positions[str(filepath)]}")
            file_count += 1

        if replay:
            self.flush_batch()
        logger.info(f"Tracking {file_count} JSONL files")


def main():
    """Run the watcher service."""
    # Configuration
    WATCH_DIR = os.getenv('CLAUDE_LOGS_DIR', '/home/magent/.claude/project-logs')
    REMOTE_ENDPOINT = os.getenv('WATCHER_REMOTE_ENDPOINT', '')  # e.g., https://memory-lane.maybelle.cryptograss.live/api/ingest/

    # Support for multi-user directories
    # If WATCH_DIR contains multiple colon-separated paths, watch all of them
    watch_dirs = [Path(d.strip()) for d in WATCH_DIR.split(':') if d.strip()]

    # Determine mode
    if REMOTE_ENDPOINT:
        logger.info(f"Running in REMOTE mode - POSTing to {REMOTE_ENDPOINT}")
        era = None  # Don't need local era for remote mode
    else:
        # Local mode - need Django database
        init_django()
        from conversations.models import Era
        era = Era.get_current()
        if era is None:
            logger.error("No current era set in database! Set an era's is_current=True via admin or shell.")
            raise SystemExit(1)
        logger.info(f"Using current era from database: {era.name}")

    # Create observer
    observer = Observer()
    watchers = []

    # Set up watchers for each directory
    for watch_dir in watch_dirs:
        if not watch_dir.exists():
            logger.warning(f"Watch directory does not exist: {watch_dir}")
            continue

        logger.info(f"Setting up watcher for: {watch_dir}")
        watcher = ConversationWatcher(
            watch_dir, era,
            remote_endpoint=REMOTE_ENDPOINT if REMOTE_ENDPOINT else None
        )
        watchers.append(watcher)

        # Scan existing files to establish baseline
        watcher.scan_existing_files()

        # Schedule observer for this directory
        observer.schedule(watcher, str(watch_dir), recursive=True)

    # Start observing all directories
    observer.start()

    logger.info(f"Watching {len(watch_dirs)} directories for new messages...")
    logger.info("Press Ctrl+C to stop")

    try:
        while True:
            time.sleep(1)
            # Periodically flush any pending batches (for remote mode)
            for watcher in watchers:
                if watcher.remote_endpoint and watcher.pending_lines:
                    if time.time() - watcher.last_flush_time > watcher.batch_interval:
                        watcher.flush_batch()
    except KeyboardInterrupt:
        # Flush any remaining lines before stopping
        for watcher in watchers:
            if watcher.remote_endpoint:
                watcher.flush_batch()
        observer.stop()
        logger.info("Stopping watcher...")

    observer.join()


if __name__ == '__main__':
    main()
