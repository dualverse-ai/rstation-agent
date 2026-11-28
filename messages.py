"""Durable operator messages for the private CLI workers."""
from datetime import datetime, timezone
import os
from pathlib import Path
from common import lock, read, save


def now():
    return datetime.now(timezone.utc).isoformat()


class Messages:
    def __init__(self, workspace):
        self.path = Path(workspace) / '.runtime/operator_messages.json'
        self.lock_path = self.path.with_suffix('.lock')

    def _update(self, action):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with lock(self.lock_path) as handle:
            owner = None
            if os.geteuid() == 0:
                # Root sends must leave both files accessible to the worker.
                parent = self.path.parent.stat()
                owner = (parent.st_uid, parent.st_gid)
                os.fchown(handle.fileno(), *owner)
            data = read(self.path, {'next': 1, 'messages': []})
            result = action(data)
            save(self.path, data, owner=owner)
            if any(m.get('owner_message') for m in data['messages']) or (self.path.parent / 'owner_history.json').exists():
                from owner_history import sync_safely
                sync_safely(self.path.parent.parent, data, owner)
            return result

    def add(self, text, *, source=None, delivery_key=None, priority=None, owner_reference=None):
        if not text.strip():
            raise ValueError('The message is empty.')
        owner_message = None
        if owner_reference is not None:
            from owner_history import provenance
            owner_message = provenance(owner_reference)
        def add(data):
            if delivery_key:
                existing = next((m for m in data['messages'] if m.get('delivery_key') == delivery_key), None)
                if existing:
                    if owner_message is not None and (existing.get('owner_message') != owner_message or existing['text'] != text):
                        raise ValueError('Duplicate owner delivery conflicts with the recorded message.')
                    return existing['id']
            number = data['next']
            data['next'] += 1
            data['messages'].append(dict(id=number, text=text if owner_message else text.strip(), status='queued', queued_at=now()))
            if owner_message is not None:
                data['messages'][-1]['owner_message'] = owner_message
            if source:
                data['messages'][-1]['source'] = source
            if delivery_key:
                data['messages'][-1]['delivery_key'] = delivery_key
            if priority == 'owner':
                message = data['messages'].pop()
                message['priority'] = 'owner'
                # Physical order also supports live wrappers loaded before upgrade.
                index = next((i for i, m in enumerate(data['messages'])
                              if m['status'] == 'queued' and m.get('priority') != 'owner'), len(data['messages']))
                data['messages'].insert(index, message)
            return number
        return self._update(add)

    def list(self):
        return read(self.path, {'messages': []})['messages']

    def pending(self, *, conversation=None):
        queued = [m for m in self.list() if m['status'] == 'queued' and
                  (conversation is None or m.get('conversation') == conversation or
                   not any(key in m for key in ('sent_at', 'received_at', 'first_received_at')))]
        # Supervisor mailbox notifications can accumulate while a researcher is
        # busy. Direct operator input must not wait behind that backlog. These
        # keys are set by the mailbox/scheduler, never inferred from message text
        # or its display label. Stable sorting preserves FIFO within each class
        # and leaves the durable history and delivery receipts unchanged.
        return sorted(queued, key=lambda m: m.get('delivery_key', '').startswith(
            ('supervision:', 'supervision-reflection:')))

    def mark(self, numbers, status, *, conversation=None):
        if not numbers:
            return
        def update(data):
            for message in data['messages']:
                if message['id'] in numbers and message['status'] != 'delivered':
                    stamp = now()
                    if status == 'sent' and conversation is not None:
                        message['conversation'] = conversation
                    if status == 'received':
                        message.setdefault('first_received_at', message.get('received_at', stamp))
                    message.update(status=status, **{status + '_at': stamp})
        self._update(update)

    def recover(self, *, conversation=None):
        # A crashed CLI may have read an instruction without finishing its turn.
        # Re-send it in the saved conversation rather than silently losing it.
        def reset(data):
            for message in data['messages']:
                if message['status'] in {'sent', 'received'} and message.get('conversation') == conversation:
                    message['status'] = 'queued'
        self._update(reset)

    def cancel_queued(self, prefix):
        def cancel(data):
            for message in data['messages']:
                if message['status'] == 'queued' and message.get('delivery_key', '').startswith(prefix):
                    message.update(status='cancelled', cancelled_at=now())
        self._update(cancel)


def prompt(message):
    if message.get('source'):
        return message['source'] + ':\n\n' + message['text']
    return 'Message from your operator:\n\n' + message['text']
