"""Pi (https://github.com/earendil-works/pi) as a harness the runner can wake.

Pi is one open-source harness for many model providers. The runner starts a
turn with `pi --mode json`, the way it starts Claude Code with `claude -p`,
and this module translates what Pi prints into record events
(docs/RECORD_EVENTS.md), memory-lane's own format, before anything is
posted. Nothing of Pi's format reaches memory-lane.

Written against a real run of Pi 1.1.0 (conversations/tests/fixtures/
pi-1.1.0-turn.jsonl). What that run showed, and the translation leans on:

  - Our turn (one wake) is Pi's agent run, `agent_start` to `agent_settled`.
    Pi's own `turn_start`/`turn_end` mark each model response inside it.
  - `message_end` is authoritative; `message_update` deltas are for display.
  - Pi echoes the prompt as a user message, so the record gets the wake
    prompt from the stream (Claude Code's `-p` never says it).
  - Messages carry no ids on the wire, only millisecond timestamps, so each
    event's id is derived from the session, the timestamp and the block.
  - Every assistant message carries its own usage and cost.
"""

import json
import logging
import os
import queue
import re
import shutil
import signal
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

STOP_REASONS = {'stop': 'end_turn', 'length': 'max_tokens', 'toolUse': 'tool_use', 'error': 'error',
                'aborted': 'aborted'}
LOOK_ONLY_TOOLS = 'read,grep,find,ls'
COMPACT = re.compile(r'/compact\s+(.+)\Z', re.S)  # what the runner sends for "/compact re <what to keep>"
EFFORTS = {'max': 'xhigh'}  # our effort names that Pi spells differently
# The names the model menu has always offered, as Pi should read them. Left
# to Pi, a bare 'opus' is fuzzy-matched across every vendor it knows, and
# Pi 1.1.0 picked Amazon Bedrock's.
ALIASES = {'opus': 'anthropic/claude-opus-5-5', 'sonnet': 'anthropic/claude-sonnet-5-5',
           'fable': 'anthropic/claude-fable-5-1', 'haiku': 'anthropic/claude-haiku-5-5'}
# Every model Pi's own library knows, by vendor (its public `compat` entry).
CATALOG_JS = """
import { getProviders, getModels } from '@earendil-works/pi-ai/compat';
const out = {};
for (const p of getProviders()) out[p] = getModels(p).map(m => [m.id, m.contextWindow || null]);
console.log(JSON.stringify(out));
"""


def iso(millis):
    return datetime.fromtimestamp(millis / 1000, tz=timezone.utc).isoformat()


def joined_text(content):
    if isinstance(content, str):
        return content
    return '\n'.join(b.get('text', '') for b in content or () if isinstance(b, dict) and b.get('type') == 'text')


def images_in(content):
    return [{'data': b['data'], 'mime_type': b.get('mimeType')} for b in content or ()
            if isinstance(b, dict) and b.get('type') == 'image' and b.get('data')]


class PiTranslator:
    """Pi's JSON events in, record events out, for one wake."""

    def __init__(self, session_id, turn=None, effort=None, prompt_from='mood-poller'):
        self.session_id, self.effort, self.prompt_from = session_id, effort, prompt_from
        self.turn = turn or str(uuid.uuid4())
        self.cost = 0.0
        self.usage = {'input': 0, 'output': 0, 'cache_read': 0, 'cache_write': 0}
        self.last_stop = None
        self.error = None  # the vendor's refusal, when a call failed: Pi keeps it on the message
        self.reply = ''
        self.started = time.monotonic()
        self.settled = False

    def id(self, millis, role, index=0):
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f'pi:{self.session_id}:{millis}:{role}:{index}'))

    def event(self, kind, at, **fields):
        return {'type': kind, 'at': at, 'turn': self.turn, **fields}

    def feed(self, raw):
        """The record events one of Pi's events stands for (often none)."""
        kind = raw.get('type') if isinstance(raw, dict) else None
        if kind == 'session':
            return [self.event('turn_started', raw.get('timestamp') or iso(time.time() * 1000),
                               cwd=raw.get('cwd'), effort=self.effort)]
        if kind == 'message_end':
            return self.message(raw.get('message') or {})
        if kind == 'compaction_end' and isinstance(raw.get('result'), dict):
            result = raw['result']
            now = time.time() * 1000
            return [self.event('compaction', iso(now), id=self.id(now, 'compaction'),
                               trigger='manual' if raw.get('reason') == 'manual' else 'auto',
                               tokens_before=result.get('tokensBefore'),
                               tokens_after=result.get('estimatedTokensAfter'), summary=result.get('summary', ''))]
        if kind == 'agent_settled':
            self.settled = True
            outcome = ('canceled' if raw.get('aborted') else 'failed' if self.last_stop == 'error' else 'completed')
            ended = self.event('turn_ended', iso(time.time() * 1000), id=str(uuid.uuid4()), outcome=outcome,
                               stop_reason=STOP_REASONS.get(self.last_stop, 'end_turn'),
                               cost_usd=round(self.cost, 6), usage=dict(self.usage),
                               duration_ms=int((time.monotonic() - self.started) * 1000))
            if outcome == 'failed' and self.error:
                ended['error'] = self.error
            return [ended]
        return []

    def message(self, message):
        role, millis = message.get('role'), message.get('timestamp') or int(time.time() * 1000)
        at = iso(millis)
        if role == 'user':
            return [self.event('user_message', at, id=self.id(millis, 'user'), text=joined_text(message.get('content')),
                               **{'from': self.prompt_from})]
        if role == 'toolResult':
            return [self.event('tool_result', at, id=self.id(millis, 'toolResult'), call_id=message.get('toolCallId'),
                               output=joined_text(message.get('content')), is_error=bool(message.get('isError')),
                               images=images_in(message.get('content')))]
        if role != 'assistant':
            return []  # the system prompt, and anything newer than this adapter
        usage = message.get('usage') or {}
        cost = float((usage.get('cost') or {}).get('total') or 0)
        self.cost += cost
        mine = {'input': usage.get('input'), 'output': usage.get('output'),
                'cache_read': usage.get('cacheRead'), 'cache_write': usage.get('cacheWrite')}
        for k, v in mine.items():
            self.usage[k] += int(v or 0)
        mine['cost'] = round(cost, 6)  # Pi's own price for the call: the vendor's, not a guess
        self.last_stop = message.get('stopReason')
        if self.last_stop == 'error':
            # Without this the Mood saw a turn end with no word and no reason: re on Anthropic,
            # 2026-10-18 and -19 ("400 ... Third-party apps now draw from your extra usage").
            self.error = str(message.get('errorMessage') or 'the model call failed')[:500]
        model = message.get('model')
        out = []
        blocks = [b for b in message.get('content') or () if isinstance(b, dict)]
        for i, block in enumerate(blocks):
            # The message's usage goes on its last block, whatever it is, so it's counted once.
            extra = {'usage': mine} if i == len(blocks) - 1 else {}
            if block.get('type') == 'text':
                out.append(self.event('assistant_message', at, id=self.id(millis, 'assistant', i), text=block.get('text', ''),
                                      model=model, **extra))
                if block.get('text', '').strip():
                    self.reply = block['text']
            elif block.get('type') == 'thinking':
                out.append(self.event('reasoning', at, id=self.id(millis, 'assistant', i), text=block.get('thinking', ''),
                                      signature=block.get('thinkingSignature', ''), model=model, **extra))
            elif block.get('type') == 'toolCall':
                out.append(self.event('tool_call', at, id=self.id(millis, 'assistant', i), call_id=block.get('id'),
                                      name=block.get('name'), input=block.get('arguments') or {}, model=model, **extra))
        return out


class EventsPoster:
    """Posts a turn's record events to its Mood's stream, in batches, off the
    reading thread. Same contract as mood_poller.StreamPoster."""

    def __init__(self, base, key, slug, session_id, harness='pi', forked_from=None, http=requests, retries=3,
                 pause=1.0):
        self.url = f"{base.rstrip('/')}/api/moods/{slug}/stream/"
        self.headers = {'Authorization': f'Bearer {key}'}
        self.slug, self.session_id, self.harness, self.forked_from = slug, session_id, harness, forked_from
        self.full_tools = None  # set by the runner: whether this wake has the agent's full tools
        self.http, self.retries, self.pause = http, retries, pause
        self.queue = queue.Queue()
        self.sent = self.failed = 0
        self.errors = []
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def put(self, event):
        if not isinstance(event, dict):
            return
        if event.get('type') == 'result':  # the runner closing a turn that failed to run
            event = {'type': 'turn_ended', 'id': str(uuid.uuid4()), 'at': datetime.now(timezone.utc).isoformat(),
                     'outcome': 'failed', 'error': event.get('subtype')}
        self.queue.put(event)

    def close(self, wait=60):
        self.queue.put(None)
        self.thread.join(wait)

    def run(self):
        done = False
        while not done:
            batch = [self.queue.get()]
            while len(batch) < 50:
                try:
                    batch.append(self.queue.get_nowait())
                except queue.Empty:
                    break
            done = None in batch
            events = [e for e in batch if e is not None]
            if events:
                self.send(events)

    def send(self, events):
        body = {'format': 'events/1', 'harness': self.harness, 'session_id': self.session_id, 'events': events}
        if self.forked_from:
            body['forked_from'] = self.forked_from
        if self.full_tools is not None:
            body['full_tools'] = self.full_tools
        for attempt in range(self.retries):
            try:
                response = self.http.post(self.url, json=body, headers=self.headers, timeout=30)
                if response.status_code == 200:
                    self.sent += len(events)
                    self.errors += response.json().get('errors') or []
                    return True
                logger.warning(f'{self.slug}: stream post answered {response.status_code}')
                if response.status_code in (400, 401, 403, 404, 413, 503):
                    break
            except requests.RequestException as e:
                logger.warning(f'{self.slug}: stream post failed: {e}')
            time.sleep(self.pause * 2 ** attempt)
        self.failed += len(events)
        return False


class PiWaker:
    """Starts one Pi turn, new or forked from a session Pi keeps on this
    machine. Same interface as mood_poller.ClaudeCodeWaker, except that what
    reaches `on_event` is already record events."""

    def __init__(self, pi='pi', model=None, session_dir='~/.local/state/magenta/pi-sessions',
                 new_cwd='~/workspace', quiet_limit=1500, version=None):
        self.pi, self.model = pi, model
        self.session_dir = Path(session_dir).expanduser()
        self.new_cwd, self.quiet_limit = new_cwd, quiet_limit
        self.version = version
        self._local = threading.local()

    @property
    def last_result(self):
        return getattr(self._local, 'last_result', None)

    @last_result.setter
    def last_result(self, value):
        self._local.last_result = value

    def harness(self):
        if self.version is None:
            try:
                self.version = subprocess.run([self.pi, '--version'], capture_output=True, text=True,
                                              timeout=30).stdout.strip().split()[-1]
            except (OSError, subprocess.SubprocessError, IndexError):
                self.version = ''
        return f'pi {self.version}'.strip()

    def package(self):
        """Pi's own package directory (where its libraries resolve from), or None."""
        found = shutil.which(self.pi) or self.pi
        for parent in Path(found).resolve().parents:
            manifest = parent / 'package.json'
            if manifest.is_file():
                try:
                    if json.loads(manifest.read_text()).get('name') == '@earendil-works/pi-coding-agent':
                        return parent
                except ValueError:
                    pass
        return None

    def run_quietly(self, cmd, cwd=None):
        try:
            return subprocess.run(cmd, cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                  timeout=120).stdout
        except (OSError, subprocess.SubprocessError):
            return ''

    def catalog(self):
        """{'harness', 'vendors': [{'name', 'usable', 'models': [[id, context], ...]}]}: every model this
        Pi knows, and whether the keys here reach each vendor (what `pi --list-models` lists is what
        it can run). Never the keys themselves. None if Pi says nothing."""
        known = {}
        package = self.package()
        if package is not None:
            try:
                known = json.loads(self.run_quietly(['node', '--input-type=module', '-e', CATALOG_JS], cwd=package)
                                   or '{}')
            except ValueError:
                known = {}
        usable = {}
        lines = self.run_quietly([self.pi, '--list-models']).splitlines()
        if lines and lines[0].split()[:2] == ['provider', 'model']:
            for line in lines[1:]:
                parts = line.split()
                if len(parts) >= 2:
                    usable.setdefault(parts[0], []).append(parts[1])
        if not known and not usable:
            return None
        vendors = []
        for name in sorted(set(known) | set(usable)):
            models = [list(m)[:2] for m in known.get(name, [])]
            ids = {m[0] for m in models}
            models += [[m, None] for m in usable.get(name, []) if m not in ids]  # a vendor of its own (models.json)
            vendors.append({'name': name, 'usable': name in usable, 'models': models})
        return {'harness': self.harness(), 'vendors': vendors}

    def find(self, session_id):
        matches = sorted(self.session_dir.glob(f'*_{session_id}.jsonl'))
        return matches[0] if matches else None

    def can_wake(self, session_id):
        return self.find(session_id) is not None

    def last_touched(self, session_id):
        """When `session_id` last ran (its file's last write), or None."""
        path = self.find(session_id)
        try:
            return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc) if path else None
        except OSError:
            return None

    def cwd_for(self, session_id):
        path = self.find(session_id)
        if path is None:
            return None
        with open(path) as f:
            header = json.loads(f.readline() or '{}')
        cwd = header.get('cwd')
        return cwd if cwd and os.path.isdir(cwd) else None

    def compact(self, session_id, keep, new_session_id=None, on_event=None, model=None, stop=None):
        """Compact a session, keeping what `keep` asks: Pi's RPC mode, whose `compact` is what its own
        /compact runs (its print mode takes '/compact' as words). Forked like a turn, so the session it
        leaves is the next wake's. (new session id, ''): what it kept is the compaction event."""
        cwd = self.cwd_for(session_id)
        if cwd is None:
            raise RuntimeError(f'pi session {session_id} cannot be resumed from here')
        new_session_id = new_session_id or str(uuid.uuid4())
        translator = PiTranslator(new_session_id)

        def say(events):
            for event in events:
                if on_event:
                    try:
                        on_event(event)
                    except Exception as e:  # never let reporting stop the turn
                        logger.warning(f'on_event failed: {e}')

        cmd = [self.pi, '--mode', 'rpc', '--session-dir', str(self.session_dir), '--session-id', new_session_id,
               '--fork', session_id]
        if model or self.model:
            cmd += ['--model', ALIASES.get(model or self.model, model or self.model)]
        say([translator.event('turn_started', iso(time.time() * 1000), cwd=cwd)])
        proc = subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, start_new_session=True)
        done = self._watch(proc, stop)
        answer = None
        try:
            proc.stdin.write(json.dumps({'id': 'compact', 'type': 'compact', 'customInstructions': keep}) + '\n')
            proc.stdin.flush()
            for line in proc.stdout:
                done.heard = time.monotonic()
                try:
                    raw = json.loads(line)
                except ValueError:
                    continue
                if raw.get('type') == 'response' and raw.get('id') == 'compact':
                    answer = raw
                    break
                say(translator.feed(raw))
        except BrokenPipeError:
            pass
        finally:
            try:
                proc.stdin.close()  # Pi's orderly exit
            except BrokenPipeError:
                pass
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGTERM)
                proc.wait()
            done.set()
        if answer is None:
            err = (proc.stderr.read() or '').strip()[:500]
            self.last_result = {'subtype': 'error', 'run_cost_usd': 0.0, 'local_command': 'compact'}
            raise RuntimeError(f'pi exited {proc.returncode} without compacting: {err}')
        cost = float((((answer.get('data') or {}).get('usage') or {}).get('cost') or {}).get('total') or 0)
        ok = bool(answer.get('success'))
        say([translator.event('turn_ended', iso(time.time() * 1000), id=str(uuid.uuid4()),
                              outcome='completed' if ok else 'failed', stop_reason='end_turn', cost_usd=round(cost, 6),
                              duration_ms=int((time.monotonic() - translator.started) * 1000),
                              **({} if ok else {'error': f"/compact: {answer.get('error') or 'failed'}"}))])
        self.last_result = {'subtype': 'success' if ok else 'error', 'run_cost_usd': cost, 'local_command': 'compact'}
        return new_session_id, ''

    def _watch(self, proc, stop):
        """Ends `proc` if the Mood stops it or it says nothing for quiet_limit seconds. The Event it
        returns: set() it when the process is done; its `heard` is when it last said something."""
        done = threading.Event()
        done.heard = time.monotonic()

        def watch():
            while not done.wait(3):
                why = ('stopped' if stop and stop() else
                       'silent too long' if time.monotonic() - done.heard > self.quiet_limit else None)
                if why:
                    logger.warning(f'ending pi turn: {why}')
                    try:
                        os.killpg(proc.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    return

        threading.Thread(target=watch, daemon=True).start()
        return done

    def command(self, session_id, new_session_id, prompt, model=None, effort=None, full=False):
        cmd = [self.pi, '--mode', 'json', '--session-dir', str(self.session_dir), '--session-id', new_session_id]
        if session_id:
            cmd += ['--fork', session_id]
        model = model or self.model
        if model:
            cmd += ['--model', ALIASES.get(model, model)]
        if effort:
            cmd += ['--thinking', EFFORTS.get(effort, effort)]
        if not full:
            cmd += ['--tools', LOOK_ONLY_TOOLS]  # reading only; MCP tools stay
        return cmd + [prompt]

    def wake(self, session_id, prompt, new_session_id=None, on_event=None, model=None, effort=None, full=False,
             stop=None, **ignored):
        """Run one turn; (new session id, its reply). '/compact <what to keep>' compacts instead."""
        keep = COMPACT.match(prompt or '')
        if keep and session_id:
            return self.compact(session_id, keep.group(1).strip(), new_session_id=new_session_id, on_event=on_event,
                                model=model, stop=stop)
        if session_id is None:
            cwd = str(Path(self.new_cwd).expanduser())
        else:
            cwd = self.cwd_for(session_id)
            if cwd is None:
                raise RuntimeError(f'pi session {session_id} cannot be resumed from here')
        new_session_id = new_session_id or str(uuid.uuid4())
        self.session_dir.mkdir(parents=True, exist_ok=True)
        translator = PiTranslator(new_session_id, effort=effort)
        cmd = self.command(session_id, new_session_id, prompt, model=model, effort=effort, full=full)
        proc = subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, start_new_session=True)
        done = self._watch(proc, stop)
        try:
            for line in proc.stdout:  # Pi frames on LF only; one JSON object per line
                done.heard = time.monotonic()
                try:
                    raw = json.loads(line)
                except ValueError:
                    continue
                for event in translator.feed(raw):
                    if on_event:
                        try:
                            on_event(event)
                        except Exception as e:  # never let reporting stop the turn
                            logger.warning(f'on_event failed: {e}')
            proc.wait()
        finally:
            done.set()
        if not translator.settled:
            # The runner closes the turn in the Mood when a wake raises.
            err = (proc.stderr.read() or '').strip()[:500]
            self.last_result = {'subtype': 'error', 'run_cost_usd': translator.cost}
            raise RuntimeError(f'pi exited {proc.returncode} without settling: {err}')
        self.last_result = {'subtype': 'error' if translator.error else 'success', 'run_cost_usd': translator.cost,
                            'stop_reason': translator.last_stop}
        if translator.error:
            self.last_result['error'] = translator.error
        return new_session_id, translator.reply.strip()
