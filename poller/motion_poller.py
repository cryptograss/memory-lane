"""Wake an agent when someone addresses it in a Motion and nobody answered.

No model runs here. This reads memory-lane's public Motion API, decides
whether a turn is owed, and if so starts exactly one.

A mention counts as owed when all of these hold:

  - it names the agent, and someone other than the agent wrote it;
  - the grace period has passed (default 10 minutes) and the agent has not
    written in that Motion since. A live session answers within the grace
    period, so no second voice is woken on top of it;
  - fewer than --max-wakes-per-hour turns have been woken already.

Mentions in the same Motion are answered together in one turn.

The turn resumes the session in which the agent last spoke in that Motion,
forked under a new session id so a live process on the original is never
written underneath. The fork's file repeats that history under the
original uuids, which is how memory-lane routes the new turn back into the
Motion (MotionSession.claim_by_history). The prompt is wrapped in
<motion-wake>, so the view hides it, the mentions endpoint ignores it, and
the importer attributes it to 'motion-poller' rather than to the owner of
the container. Woken turns can read but not run commands or edit; see the
"Speaking in a Motion" etiquette on the Magenta 26 Million wiki page.

Harness independence: everything specific to Claude Code is in
ClaudeCodeWaker. Another harness needs another waker, nothing else.

    python poller/motion_poller.py --agent magent            # run forever
    python poller/motion_poller.py --agent magent --once --dry-run
"""

import argparse
import json
import logging
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

logger = logging.getLogger('motion_poller')

DEFAULT_BASE = 'https://memory-lane.maybelle.cryptograss.live'
ETIQUETTE = 'https://pickipedia.xyz/wiki/Cryptograss:Magenta_26_Million#Speaking_in_a_Motion'
SILENT = '<silent/>'


def parse_time(iso):
    return datetime.fromisoformat(iso.replace('Z', '+00:00'))


class MotionAPI:
    def __init__(self, base_url, http=requests):
        self.base = base_url.rstrip('/')
        self.http = http

    def _get(self, path, **params):
        response = self.http.get(f'{self.base}{path}', params=params, timeout=30)
        response.raise_for_status()
        return response.json()

    def mentions(self, agent, since=None):
        params = {'limit': 200}
        if since:
            params['since'] = since
        return self._get(f'/api/mentions/{agent}/', **params)['mentions']

    def turns_after(self, slug, message_id):
        return self._get(f'/api/motions/{slug}/turns/', after=message_id)['turns']

    def sessions(self, slug, sender):
        return [s['session_id'] for s in self._get(f'/api/motions/{slug}/sessions/', sender=sender)['sessions']]


class ClaudeCodeWaker:
    """Starts one Claude Code turn by forking a session that exists on this machine."""

    TOOLS = 'Read,Grep,Glob'
    # Pre-approved in settings for interactive use; a woken turn must not write.
    DENIED = [
        'mcp__pickipedia__create-page', 'mcp__pickipedia__update-page',
        'mcp__pickipedia__delete-page', 'mcp__pickipedia__undelete-page',
        'mcp__pickipedia__upload-file', 'mcp__pickipedia__upload-file-from-url',
        'mcp__pickipedia__add-wiki', 'mcp__pickipedia__remove-wiki', 'mcp__pickipedia__set-wiki',
    ]

    def __init__(self, projects_dir='~/.claude/projects', claude='claude', timeout=900, model=None):
        self.projects_dir = Path(projects_dir).expanduser()
        self.claude = claude
        self.timeout = timeout
        self.model = model

    def find(self, session_id):
        matches = list(self.projects_dir.glob(f'*/{session_id}.jsonl'))
        return matches[0] if matches else None

    def can_wake(self, session_id):
        return self.find(session_id) is not None

    @staticmethod
    def cwd_of(path):
        """The working directory the session last ran in; resume must start there."""
        cwd = None
        with open(path) as f:
            for line in f:
                try:
                    cwd = json.loads(line).get('cwd') or cwd
                except json.JSONDecodeError:
                    continue
        return cwd

    def command(self, session_id, new_session_id, prompt):
        cmd = [self.claude, '-p', '--resume', session_id, '--fork-session',
               '--session-id', new_session_id, '--tools', self.TOOLS,
               '--permission-mode', 'dontAsk', '--disallowedTools', *self.DENIED]
        if self.model:
            cmd += ['--model', self.model]
        return cmd + ['--', prompt]

    def wake(self, session_id, prompt):
        path = self.find(session_id)
        new_session_id = str(uuid.uuid4())
        cwd = self.cwd_of(path)
        if not cwd or not os.path.isdir(cwd):
            cwd = str(Path.home())
        result = subprocess.run(self.command(session_id, new_session_id, prompt), cwd=cwd,
                                capture_output=True, text=True, timeout=self.timeout)
        if result.returncode != 0:
            raise RuntimeError(f'claude exited {result.returncode}: {result.stderr.strip()[:500]}')
        return new_session_id, result.stdout.strip()


class MotionPoller:

    def __init__(self, api, waker, agent='magent', state_path=None, grace=600,
                 max_wakes_per_hour=4, dry_run=False, now=None):
        self.api = api
        self.waker = waker
        self.agent = agent.lower()
        self.state_path = Path(state_path) if state_path else None
        self.grace = timedelta(seconds=grace)
        self.max_wakes_per_hour = max_wakes_per_hour
        self.dry_run = dry_run
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.state = self.load()

    # --- state ---------------------------------------------------------------

    def load(self):
        if self.state_path and self.state_path.exists():
            return json.loads(self.state_path.read_text())
        # First run: start from now, never from history.
        return {'since': self.now().isoformat(), 'handled': [], 'wakes': []}

    def save(self):
        if self.state_path and not self.dry_run:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(json.dumps(self.state, indent=1))

    def recent_wakes(self):
        cutoff = self.now() - timedelta(hours=1)
        return [w for w in self.state['wakes'] if parse_time(w) > cutoff]

    # --- one pass ------------------------------------------------------------

    def poll_once(self):
        """Look once; wake at most one turn per Motion. Returns the wakes made."""
        mentions = self.api.mentions(self.agent, since=self.state['since'])
        handled = set(self.state['handled'])
        pending = {}
        for m in sorted(mentions, key=lambda m: m['turn']['created_at']):
            turn = m['turn']
            if turn['sender'] == self.agent or turn['id'] in handled:
                continue
            pending.setdefault(m['motion'], []).append(turn)

        wakes = []
        for slug, owed in pending.items():
            outcome = self.consider(slug, owed)
            if outcome is None:
                continue  # not yet; look again next pass
            handled.update(t['id'] for t in owed)
            if outcome == 'woken':
                wakes.append(slug)

        self.state['handled'] = sorted(handled)
        self.advance_since(mentions, handled)
        self.save()
        return wakes

    def consider(self, slug, owed):
        """'answered', 'woken', 'unreachable', or None to retry later."""
        first = owed[0]
        if self.now() - parse_time(first['created_at']) < self.grace:
            return None
        if any(t['sender'] == self.agent for t in self.api.turns_after(slug, first['id'])):
            logger.info(f'{slug}: already answered')
            return 'answered'
        if len(self.recent_wakes()) >= self.max_wakes_per_hour:
            logger.warning(f'{slug}: owed a turn, but {self.max_wakes_per_hour} wakes this hour already')
            return None

        session = next((s for s in self.api.sessions(slug, self.agent) if self.waker.can_wake(s)), None)
        if session is None:
            logger.warning(f'{slug}: owed a turn, but no session of {self.agent} in it exists here')
            return 'unreachable'

        prompt = self.prompt(slug, owed)
        if self.dry_run:
            logger.info(f'{slug}: would wake {session} with:\n{prompt}')
            return 'woken'
        new_session, reply = self.waker.wake(session, prompt)
        self.state['wakes'].append(self.now().isoformat())
        logger.info(f'{slug}: woke {session} as {new_session}; replied {"silence" if reply == SILENT else f"{len(reply)} chars"}')
        return 'woken'

    def prompt(self, slug, owed):
        minutes = int(self.grace.total_seconds() // 60)
        lines = [f'<motion-wake motion="{slug}">',
                 f'You were woken by the Motion poller: nobody answered this in {minutes} minutes.', '']
        for turn in owed:
            lines.append(f"[{turn['sender']}, {turn['created_at'][:16]}Z] {turn['text']}")
        lines += ['',
                  f'Etiquette: {ETIQUETTE}',
                  'Answer in the Motion by replying normally; your reply is recorded there.',
                  f'If nothing is worth saying, reply with exactly {SILENT}',
                  'This turn can read but cannot run commands or edit anything.',
                  '</motion-wake>']
        return '\n'.join(lines)

    def advance_since(self, mentions, handled):
        """Move `since` up to the newest mention with nothing unhandled before it."""
        since = self.state['since']
        ordered = sorted((m['turn'] for m in mentions), key=lambda t: t['created_at'])
        seen = {t['id']: t['created_at'] for t in ordered}
        owed = [parse_time(t['created_at']) for t in ordered
                if t['sender'] != self.agent and t['id'] not in handled]
        for turn in ordered:
            # The API returns created_at > since, so since must stay strictly
            # before anything still owed, even a mention at the same instant.
            if owed and parse_time(turn['created_at']) >= owed[0]:
                break
            since = turn['created_at']
        self.state['since'] = since
        # Only ids newer than `since` can come back from the API; forget the rest.
        self.state['handled'] = sorted(i for i in handled
                                       if i in seen and parse_time(seen[i]) > parse_time(since))
        self.state['wakes'] = self.recent_wakes()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--agent', default='magent')
    parser.add_argument('--base', default=os.environ.get('MEMORY_LANE_URL', DEFAULT_BASE))
    parser.add_argument('--state', default='~/.local/state/magenta/motion_poller.json')
    parser.add_argument('--interval', type=int, default=60, help='Seconds between looks')
    parser.add_argument('--grace', type=int, default=600, help='Seconds to leave for a live session to answer')
    parser.add_argument('--max-wakes-per-hour', type=int, default=4)
    parser.add_argument('--model', default=None)
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--dry-run', action='store_true', help='Log what would be woken; change nothing')
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
    poller = MotionPoller(MotionAPI(args.base), ClaudeCodeWaker(model=args.model), agent=args.agent,
                          state_path=Path(args.state).expanduser(), grace=args.grace,
                          max_wakes_per_hour=args.max_wakes_per_hour, dry_run=args.dry_run)
    while True:
        try:
            poller.poll_once()
        except Exception as e:  # keep looking; one bad pass is not a reason to stop
            logger.error(f'poll failed: {e}')
        if args.once:
            return 0
        time.sleep(args.interval)


if __name__ == '__main__':
    sys.exit(main())
