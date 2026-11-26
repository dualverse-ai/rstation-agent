"""Keep the native CLI input open; operator messages never need API credentials."""
import argparse
from collections import deque
import json
import os
from pathlib import Path
import queue
import signal
import subprocess
import sys
import threading
import time

from antigravity import completed_turn, refresh_context_budget
from common import pid_token, save, read
from messages import Messages, prompt
from live_watchdog import NativeWatchdog, descendants, group_has_work
from subagents import reasoning_effort


def emit(event):
    print(json.dumps(event, ensure_ascii=False), flush=True)


def native_command(kind, command):
    """Preserve the launcher's provider, tools, model and isolation settings."""
    if kind == 'codex':
        result = [command[0]]
        index = 1
        while index < command.index('exec'):
            arg = command[index]
            if arg in {'-c', '--config'}:
                result.extend(command[index:index + 2]); index += 2
            elif arg == '--search':
                result.extend(['-c', 'web_search="live"']); index += 1
            elif arg in {'--cd', '--sandbox', '--add-dir'}:
                index += 2  # cwd/sandbox are supplied in thread/start or resume.
            else:
                raise ValueError('Unsupported Codex launch option: ' + arg)
        return result + ['app-server', '--listen', 'stdio://']
    result = list(command)
    if kind == 'gemini':
        result[result.index('-p') + 1] = ''
    else:
        # The shared Claude backend places the initial prompt after --model NAME.
        index = result.index('--model') + 2
        del result[index]
        result.append('--replay-user-messages')
    return result + ['--input-format', 'stream-json']


class LiveCLI:
    def __init__(self, args):
        self.args = args
        self.messages = Messages(args.workspace)
        self.messages.recover()
        self.events = queue.Queue()
        self.output = bytearray()
        self.sent = {}  # message number -> text, only for this native process
        self.received = set()
        self.missing_replay_after_result = False
        self.inputs = deque()  # Gemini's user_input events follow stdin order.
        self.current = None
        self.busy = False
        self.next_continue = 0
        self.thread = None
        self.turn = None
        self.request_id = 0
        self.requests = {}
        self.child = None
        self.gemini_events = []
        self.is_supervisor = read(Path(args.workspace) / '.runtime/supervision.json', {}).get('supervisor', False)
        self.startup_prompt = None
        config, self.run_worker = self.run_config()
        self.watchdog = NativeWatchdog(config.get('idle_seconds', 1800))

    def run_config(self):
        work = Path(self.args.workspace)
        resolved = work.resolve()
        data_root = Path(os.environ.get('RSTATION_AGENT_DATA', str(Path(__file__).resolve().parent / 'agent_data'))).expanduser()
        try:
            paths = [work.parent / 'run.json', *data_root.glob('*/run.json')]
        except (OSError, RuntimeError):
            paths = [work.parent / 'run.json']
        matches = []
        seen = set()
        for path in paths:
            try:
                identity = path.resolve()
            except (OSError, RuntimeError):
                continue
            if identity in seen:
                continue
            seen.add(identity)
            try:
                config = read(path)
            except (OSError, ValueError, UnicodeError):
                continue
            if not isinstance(config, dict):
                continue
            for worker in config.get('workers', []):
                if not isinstance(worker, dict) or not worker.get('name'):
                    continue
                try:
                    worker_path = (path.parent / worker['name']).resolve()
                except (OSError, RuntimeError, ValueError):
                    continue
                if worker_path == resolved:
                    matches.append((config, worker))
        if len(matches) == 1:
            return matches[0]
        try:
            direct = read(work.parent / 'run.json', {})
        except (OSError, ValueError, UnicodeError):
            direct = {}
        if direct.get('role') == 'supervisor':
            return direct, None
        return {}, None

    def continue_ready(self):
        if self.is_supervisor:
            from supervision import sleeping
            if sleeping(self.args.workspace):
                return False
        return time.monotonic() >= self.next_continue

    def write(self, event):
        self.output.extend((json.dumps(event, ensure_ascii=False) + '\n').encode())

    def request(self, method, params, message=None):
        self.request_id += 1
        self.requests[self.request_id] = (method, message)
        self.write(dict(id=self.request_id, method=method, params=params))

    def codex_model(self):
        # Main researchers adopt operator model changes only at turn boundaries.
        # A controller loaded before the edit can still pass its old --model.
        work = Path(self.args.workspace)
        config, worker = self.run_config()
        if not self.is_supervisor and config.get('role') != 'supervisor' and worker and worker.get('kind') == 'codex':
            from worker_control import effective_worker
            state = read(work / '.runtime/state.json', {})
            return effective_worker(worker, state).get('model') or self.args.model
        return self.args.model

    def send(self, text, number=None):
        if not self.busy:
            self.watchdog.activity()
        if self.args.kind == 'codex':
            params = dict(threadId=self.thread, input=[dict(type='text', text=text)])
            if self.turn:
                params['expectedTurnId'] = self.turn
                self.request('turn/steer', params, number)
            else:
                model = self.codex_model()
                params.update(model=model, effort=reasoning_effort('codex', model))
                self.busy = True
                self.request('turn/start', params, number)
        else:
            key = 'event' if self.args.kind == 'gemini' else 'type'
            self.write({key: 'user', 'message': {'role': 'user', 'content': text}})
            self.inputs.append(number)
            self.busy = True
        if number is not None:
            self.sent[number] = text
            self.messages.mark([number], 'sent')

    def send_pending(self, messages, initial_prompt=None):
        # Claude and Antigravity read instructions at turn boundaries. Carry a
        # bounded backlog together so one long turn cannot starve later mail.
        batch = []
        size = len(initial_prompt) + 2 if initial_prompt else 0
        for message in messages:
            text = prompt(message)
            if batch and (self.args.kind == 'codex' or size + 2 + len(text) > 65536):
                break
            batch.append((message['id'], text))
            size += len(text) + (2 if len(batch) > 1 else 0)
        text = '\n\n'.join(part for _, part in batch)
        if initial_prompt:
            text = initial_prompt + '\n\n' + text
        self.send(text, batch[0][0])
        if len(batch) > 1:
            numbers = tuple(number for number, _ in batch)
            self.inputs[-1] = numbers
            self.sent.update({number: text for number in numbers})
            self.messages.mark(list(numbers), 'sent')

    def accepted(self, number):
        if number is not None:
            numbers = list(number) if isinstance(number, tuple) else [number]
            self.received.update(numbers)
            self.messages.mark(numbers, 'received')
            if self.sent.keys() <= self.received:
                self.missing_replay_after_result = False

    def can_send_message(self, message):
        # Claude can drop streamed stdin received during an active turn, leaving
        # its missing replay receipt to block every later operator message.
        # Keep those instructions durable until the native result closes the turn.
        if self.args.kind == 'claude' and self.busy:
            return False
        # Other backends accept steering before the current turn completes.
        return self.sent.keys() <= self.received

    def finish(self):
        self.messages.mark(self.received, 'delivered')
        for number in self.received:
            self.sent.pop(number, None)
        self.received.clear()
        self.current = None
        self.busy = bool(self.sent)
        # A successful native result does not acknowledge an input. Claude can
        # finish background work without replaying a streamed operator message.
        # Recover the saved session only after it becomes safely quiescent.
        self.missing_replay_after_result = self.args.kind == 'claude' and bool(self.sent)
        self.next_continue = time.monotonic() + 5

    def codex_event(self, event):
        if 'id' in event and event['id'] in self.requests:
            method, number = self.requests.pop(event['id'])
            if 'error' in event:
                if method == 'turn/steer':
                    # Completion can race with delivery; start a new turn in
                    # the same process instead of losing the instruction.
                    self.messages.mark([number], 'queued')
                    self.sent.pop(number, None)
                    self.turn = None
                    self.busy = False
                    return
                emit({'type': 'error', 'message': str(event['error'])})
                raise RuntimeError('Codex request failed: ' + method)
            result = event.get('result', {})
            if method == 'initialize':
                self.write({'method': 'initialized', 'params': {}})
                params = dict(model=self.codex_model(), cwd=self.args.workspace,
                              approvalPolicy='never', sandbox='danger-full-access')
                if self.args.conversation:
                    params.update(threadId=self.args.conversation, excludeTurns=True)
                self.request('thread/resume' if self.args.conversation else 'thread/start', params)
            elif method in {'thread/start', 'thread/resume'}:
                self.thread = result['thread']['id']
                emit({'type': 'thread.started', 'thread_id': self.thread})
                if self.is_supervisor and not self.continue_ready():
                    self.startup_prompt = self.args.prompt
                else:
                    self.send(self.args.prompt)
            elif method in {'turn/start', 'turn/steer'}:
                self.turn = result.get('turnId') or result.get('turn', {}).get('id') or self.turn
                self.accepted(number)
            return
        method, params = event.get('method'), event.get('params', {})
        if method == 'turn/started':
            self.turn = params['turn']['id']; self.busy = True
            emit({'type': 'turn.started'})
        elif method == 'turn/completed':
            self.turn = None
            if params['turn']['status'] != 'completed':
                emit({'type': 'turn.failed', 'error': params['turn'].get('error')})
                raise RuntimeError('Codex turn failed')
            emit({'type': 'turn.completed'})
            self.finish()
        elif method in {'item/started', 'item/completed'}:
            item = dict(params['item'])
            kinds = {'agentMessage': 'agent_message', 'commandExecution': 'command_execution',
                     'fileChange': 'file_change', 'mcpToolCall': 'mcp_tool_call',
                     'webSearch': 'web_search', 'collabAgentToolCall': 'collab_tool_call'}
            item['type'] = kinds.get(item['type'], item['type'])
            emit({'type': method.replace('/', '.'), 'item': item})
        elif method == 'error':
            emit({'type': 'error', 'message': str(params.get('error'))})
        elif 'id' in event and method:
            # Unexpected interactive tools must receive an answer, not hang.
            self.write({'id': event['id'], 'error': {'code': -32601,
                        'message': 'This researcher runs unattended; continue without interactive input.'}})
        else:
            # Retain deltas and tool progress for monitoring and idle detection.
            emit(event)

    def stream_event(self, event):
        emit(event)
        # Retry notices and tool heartbeats are not model progress.
        step = event.get('step_update', {})
        if (event.get('type') in {'assistant', 'user', 'stream_event', 'result'}
                or step.get('step_type') in {'agent_response', 'tool', 'user_input'}
                and step.get('state') in {'DONE', 'ACTIVE'}):
            self.watchdog.activity()
        if self.args.kind == 'gemini':
            step = event.get('step_update', {})
            if step.get('step_type') == 'user_input' and step.get('state') == 'DONE':
                self.gemini_events = []
                self.current = self.inputs.popleft() if self.inputs else None
                self.accepted(self.current)
            if step:
                self.gemini_events.append({'step_update': {k: step[k] for k in ('step_type', 'step_index', 'state', 'text_delta') if k in step}})
            if event.get('event') == 'result':
                self.gemini_events.append(event)
                if not completed_turn(self.gemini_events):
                    raise RuntimeError('Gemini turn failed')
                self.finish()
        else:
            if event.get('type') == 'user':
                content = event.get('message', {}).get('content')
                if isinstance(content, list):
                    content = ''.join(p.get('text', '') for p in content if p.get('type') == 'text')
                for number, text in self.sent.items():
                    if text == content:
                        self.accepted(number)
            if event.get('type') == 'result':
                if event.get('is_error') or event.get('subtype') != 'success':
                    raise RuntimeError('Claude turn failed')
                self.finish()

    def run(self):
        from gemini_stream import native_environment
        config, _ = self.run_config()
        with native_environment(self.args.kind, config, os.environ) as environment:
            return self.run_native(environment)

    def run_native(self, environment=None):
        if self.args.kind == 'gemini':
            refresh_context_budget(self.args.workspace, self.args.model)
        native = native_command(self.args.kind, self.args.command)
        self.child = subprocess.Popen(native, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      text=True, bufsize=1, env=environment)
        info = Path(self.args.workspace) / '.runtime/live_input.json'
        save(info, dict(pid=self.child.pid, token=pid_token(self.child.pid), backend=self.args.kind))
        os.set_blocking(self.child.stdin.fileno(), False)
        def read_output():
            try:
                for line in self.child.stdout:
                    self.events.put(line)
            finally:
                self.events.put(None)
        threading.Thread(target=read_output, daemon=True).start()
        if self.args.kind == 'codex':
            self.request('initialize', {'clientInfo': {'name': 'rstation_workers', 'version': '0.1.0'}})
        else:
            pending = self.messages.pending()
            if pending:
                # Fresh sessions still need the complete task, together with
                # queued guidance, before their first potentially long turn.
                self.send_pending(pending, None if self.args.conversation else self.args.prompt)
            else:
                self.send(self.args.prompt)
        input_closed = False
        try:
            while True:
                if self.output:
                    try:
                        count = os.write(self.child.stdin.fileno(), self.output[:65536])
                        del self.output[:count]
                    except BlockingIOError:
                        pass
                    except BrokenPipeError:
                        # A native CLI can close stdin while its final stdout
                        # events are still queued. Drain them before recovery;
                        # unacknowledged messages remain durable for the next CLI.
                        input_closed = True
                        self.output.clear()
                try:
                    line = self.events.get(timeout=.1)
                except queue.Empty:
                    line = ''
                if line is None:
                    return self.child.wait() or 1  # An unrequested native exit needs recovery.
                if line:
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(event, dict):
                        if self.args.kind == 'codex': self.codex_event(event)
                        else: self.stream_event(event)
                if self.args.kind in {'claude', 'gemini'}:
                    from worker_control import checkpoint_requested
                    waiting = not self.busy and (checkpoint_requested(self.args.workspace) or
                              self.is_supervisor and not self.continue_ready())
                    recovery = self.watchdog.check(self.child.pid, waiting=waiting,
                                                   missing_replay=self.missing_replay_after_result)
                    if recovery:
                        emit({'type': 'watchdog', **recovery})
                        if recovery['action'] == 'resume_native':
                            # Close the race with a tool starting after inspection.
                            os.kill(self.child.pid, signal.SIGSTOP)
                            try:
                                if (not descendants(self.child.pid) and
                                        not group_has_work(os.getpgrp(), {os.getpid(), self.child.pid})):
                                    raise RuntimeError('Native CLI stalled; resume saved conversation')
                            finally:
                                os.kill(self.child.pid, signal.SIGCONT)
                from worker_control import offer_checkpoint
                if offer_checkpoint(self):
                    continue
                ready = self.args.kind != 'codex' or (self.thread is not None and (not self.busy or self.turn is not None))
                # Long messages cannot block output, tools or the controller.
                if ready and not self.output and not input_closed:
                    pending = self.messages.pending()
                    if pending and self.can_send_message(pending[0]):
                        if self.is_supervisor:
                            from supervision import wake
                            wake(self.args.workspace)
                        self.send_pending(pending)
                    elif not pending and not self.busy and self.continue_ready():
                        self.send(self.startup_prompt or 'Continue.')
                        self.startup_prompt = None
        finally:
            self.child.stdin.close()
            if self.child.poll() is None:
                self.child.terminate()
                try: self.child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.child.kill(); self.child.wait()
            self.child.stdout.close()
            info.unlink(missing_ok=True)


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--kind', choices=['codex', 'claude', 'gemini'], required=True)
    p.add_argument('--workspace', required=True)
    p.add_argument('--prompt-file', required=True)
    p.add_argument('--model', required=True)
    p.add_argument('--conversation', default='')
    p.add_argument('command', nargs=argparse.REMAINDER)
    args = p.parse_args()
    args.command = args.command[1:] if args.command[:1] == ['--'] else args.command
    args.prompt = Path(args.prompt_file).read_text()
    def stop(*_): raise SystemExit(0)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        raise SystemExit(LiveCLI(args).run())
    except (OSError, RuntimeError, ValueError) as exc:
        print('Live CLI stopped: ' + type(exc).__name__ + ': ' + str(exc), file=sys.stderr)
        raise SystemExit(1)


if __name__ == '__main__':
    main()
