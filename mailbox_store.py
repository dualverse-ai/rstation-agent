"""Local, durable correspondence for an explicitly assigned supervision group.

This is trusted same-user operator tooling, not a public API or security boundary.
SQLite transactions contain only mailbox state; native CLI delivery happens later.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import secrets
import sqlite3
import time

FALLBACK_SECONDS = 24 * 3600
REFLECTION_SECONDS = 24 * 3600
PROTOCOL_VERSION = 2
REQUEST_KINDS = {'proposal', 'pivot', 'renewal'}
DECISIONS = {'approve', 'reject', 'revise'}


def utc(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


class Mailbox:
    def __init__(self, path):
        self.path = Path(path)

    @contextmanager
    def transaction(self):
        con = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        con.row_factory = sqlite3.Row
        try:
            con.execute('PRAGMA foreign_keys=ON')
            con.execute('BEGIN IMMEDIATE')
            self._upgrade_protocol(con)
            yield con
            con.commit()
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()

    def create(self, participants):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        con = sqlite3.connect(self.path)
        try:
            con.execute('PRAGMA journal_mode=WAL')
            con.executescript('''
                CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE participants (
                    actor TEXT PRIMARY KEY, label TEXT NOT NULL UNIQUE,
                    role TEXT NOT NULL, workspace TEXT NOT NULL UNIQUE
                );
                CREATE TABLE messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sender TEXT NOT NULL REFERENCES participants(actor),
                    recipient TEXT NOT NULL REFERENCES participants(actor),
                    text TEXT NOT NULL, kind TEXT NOT NULL, created REAL NOT NULL,
                    reply_to INTEGER REFERENCES messages(id), decision TEXT,
                    deadline REAL, outcome TEXT, decided REAL,
                    archived REAL, native_id INTEGER, automatic_notified INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX inbox ON messages(recipient, archived, id);
                CREATE INDEX undecided ON messages(outcome, deadline);
            ''')
            con.executemany('INSERT INTO participants VALUES (:actor,:label,:role,:workspace)', participants)
            con.executemany('INSERT INTO settings VALUES (?,?)', [
                ('active', 'true'), ('id', json.dumps(secrets.token_hex(12))),
                ('protocol_version', json.dumps(PROTOCOL_VERSION)),
                ('reflection', json.dumps({'due': time.time() + REFLECTION_SECONDS}))])
            con.commit()
        finally:
            con.close()
        self.path.chmod(0o600)

    @staticmethod
    def _upgrade_protocol(con):
        """Migrate once under the writer lock, retaining completed decisions."""
        row = con.execute("SELECT value FROM settings WHERE key='protocol_version'").fetchone()
        if row and json.loads(row[0]) >= PROTOCOL_VERSION:
            return
        con.execute("UPDATE messages SET deadline=created+? WHERE outcome='pending'",
                    (FALLBACK_SECONDS,))
        state = Mailbox._setting(con, 'reflection')
        # The old due time was six hours after creation/last completion.
        # Keep an already sampled reflection intact; only move its next cycle.
        if not state.get('prompt'):
            state['due'] += REFLECTION_SECONDS - 6 * 3600
            Mailbox._set(con, 'reflection', state)
        con.execute("INSERT OR REPLACE INTO settings VALUES ('protocol_version',?)",
                    (json.dumps(PROTOCOL_VERSION),))

    @staticmethod
    def _setting(con, key):
        return json.loads(con.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()[0])

    @staticmethod
    def _set(con, key, value):
        con.execute('UPDATE settings SET value=? WHERE key=?', (json.dumps(value), key))

    def setting(self, key):
        with self.transaction() as con:
            return self._setting(con, key)

    def participants(self):
        with self.transaction() as con:
            return [dict(r) for r in con.execute('SELECT * FROM participants ORDER BY actor')]

    def refresh_labels(self, labels):
        with self.transaction() as con:
            for actor, label in labels.items():
                if label:
                    con.execute('UPDATE participants SET label=? WHERE actor=?', (label, actor))

    @staticmethod
    def _person(con, name):
        row = con.execute('SELECT * FROM participants WHERE actor=? OR label=?', (name, name)).fetchone()
        if row is None:
            raise ValueError('That participant is not in this supervision group.')
        return row

    @staticmethod
    def _expire(con, now):
        con.execute("""UPDATE messages SET outcome=CASE WHEN kind='pivot'
                    THEN 'autorejected' ELSE 'autoaccepted' END, decided=deadline
                    WHERE outcome='pending' AND deadline<=?""", (now,))

    def send(self, actor, recipient, text, *, kind='message', reply_to=None, decision=None):
        if not text.strip():
            raise ValueError('The message is empty.')
        if kind not in REQUEST_KINDS | {'message'} or decision not in DECISIONS | {None}:
            raise ValueError('Invalid message kind or decision.')
        with self.transaction() as con:
            # Read the clock after acquiring the writer slot, including races
            # between a late decision and automatic acceptance.
            now = time.time()
            if not self._setting(con, 'active'):
                raise ValueError('Supervision has ended. Continue independent research.')
            self._expire(con, now)
            sender = self._person(con, actor)
            target = self._person(con, recipient)
            if sender['actor'] == target['actor'] or sender['role'] == target['role']:
                raise ValueError('Use this mailbox between a researcher and its supervisor.')
            if kind in REQUEST_KINDS and sender['role'] != 'researcher':
                raise ValueError('Only researchers submit proposal, pivot and renewal requests.')
            if decision and (sender['role'] != 'supervisor' or reply_to is None or kind != 'message'):
                raise ValueError('A supervisor decision must reply to a request.')
            if reply_to is not None:
                original = con.execute('SELECT * FROM messages WHERE id=?', (reply_to,)).fetchone()
                if original is None or original['sender'] != target['actor'] or original['recipient'] != sender['actor']:
                    raise ValueError('Reply to a message from this recipient.')
                if decision:
                    if original['outcome'] != 'pending':
                        raise ValueError('This request is already decided; send an ordinary follow-up instead.')
                    con.execute('UPDATE messages SET outcome=?,decided=? WHERE id=?', (decision, now, reply_to))
            deadline = now + FALLBACK_SECONDS if kind in REQUEST_KINDS else None
            result = con.execute('''INSERT INTO messages
                (sender,recipient,text,kind,created,reply_to,decision,deadline,outcome)
                VALUES (?,?,?,?,?,?,?,?,?)''', (sender['actor'], target['actor'], text.strip(), kind,
                    now, reply_to, decision, deadline, 'pending' if deadline else None))
            return dict(con.execute('SELECT * FROM messages WHERE id=?', (result.lastrowid,)).fetchone())

    def inbox(self, actor, *, history=False):
        with self.transaction() as con:
            now = time.time()
            self._expire(con, now)
            person = self._person(con, actor)
            where = '(recipient=? OR sender=?)' if history else 'recipient=? AND archived IS NULL'
            args = (person['actor'], person['actor']) if history else (person['actor'],)
            return [dict(r) for r in con.execute('SELECT * FROM messages WHERE ' + where + ' ORDER BY id', args)]

    def read(self, actor, number, *, archive=False):
        with self.transaction() as con:
            now = time.time()
            self._expire(con, now)
            person = self._person(con, actor)
            row = con.execute('SELECT * FROM messages WHERE id=? AND (sender=? OR recipient=?)',
                              (number, person['actor'], person['actor'])).fetchone()
            if row is None:
                raise ValueError('Message not found in your correspondence.')
            if archive:
                if row['recipient'] != person['actor']:
                    raise ValueError('Only the recipient can archive this message.')
                con.execute('UPDATE messages SET archived=? WHERE id=?', (now, number))
            return dict(row)

    def deliver(self):
        """Crash-safe outbox, with deduplication in each native input queue."""
        from messages import Messages
        with self.transaction() as con:
            if not self._setting(con, 'active'):
                return
            self._expire(con, time.time())
            group = self._setting(con, 'id')
            rows = [dict(r) for r in con.execute('''SELECT m.*,s.label AS sender_label,
                s.role AS sender_role,r.workspace FROM messages m
                JOIN participants s ON s.actor=m.sender JOIN participants r ON r.actor=m.recipient
                WHERE m.native_id IS NULL ORDER BY m.id''')]
            automatic = [dict(r) for r in con.execute("SELECT * FROM messages WHERE outcome IN ('autoaccepted','autorejected') AND automatic_notified=0")]
            people = {r['actor']: dict(r) for r in con.execute('SELECT * FROM participants')}
        # Never hold a DB transaction while acquiring another queue's lock.
        for row in rows:
            text = f"Private mailbox message {row['id']} from {row['sender_label']} ({row['kind']}).\n" + row['text']
            if row['deadline']:
                outcome = 'rejected' if row['kind'] == 'pivot' else 'accepted'
                text += ('\n\nRecorded submission: ' + utc(row['created']) + '. 24-hour response deadline: '
                         + utc(row['deadline']) + '. Without a substantive decision, this request is automatically ' + outcome + '.')
            if row['reply_to']:
                text += f"\nReply to message {row['reply_to']}." + (f" Decision: {row['decision']}." if row['decision'] else '')
            text += '\nUse ./mail to reply privately. Reading does not archive a message.'
            source = ('Message from your research supervisor (not a human-operator instruction)' if row['sender_role'] == 'supervisor'
                      else 'Message from your assigned researcher')
            number = Messages(row['workspace']).add(text, source=source, delivery_key=f'supervision:{group}:{row["id"]}')
            with self.transaction() as con:
                con.execute('UPDATE messages SET native_id=? WHERE id=? AND native_id IS NULL', (number, row['id']))
        for row in automatic:
            accepted = row['outcome'] == 'autoaccepted'
            outcome = 'accepted' if accepted else 'rejected'
            hours = (row['deadline'] - row['created']) / 3600
            guidance = ('Continue under that acceptance; a late response cannot revoke it.' if accepted else
                        'Continue your committed direction. You may submit a new pivot request when the supervisor returns.')
            for actor in (row['sender'], row['recipient']):
                Messages(people[actor]['workspace']).add(
                    f"Mailbox request {row['id']} ({row['kind']}) was automatically {outcome} at {utc(row['deadline'])}, after {hours:g} hours without a substantive decision. {guidance}",
                    source='Supervision mailbox', delivery_key=f'supervision:{group}:automatic:{row["id"]}:{actor}')
            with self.transaction() as con:
                con.execute('UPDATE messages SET automatic_notified=1 WHERE id=?', (row['id'],))

    def reflection(self, actor, pool, answer=None):
        with self.transaction() as con:
            if self._person(con, actor)['role'] != 'supervisor':
                raise ValueError('This reflection tool is for the supervisor.')
            if not self._setting(con, 'active'):
                raise ValueError('Supervision has ended.')
            now = time.time()
            state = self._setting(con, 'reflection')
            if answer is not None:
                if not state.get('prompt') or not answer.strip():
                    raise ValueError('Sample a due reflection before completing it with a written response.')
                previous = {**state, 'answer': answer.strip(), 'completed': now}
                self._set(con, 'reflection', {'due': now + REFLECTION_SECONDS, 'previous': previous})
                return previous
            if now < state['due']:
                return {'due': state['due']}
            if not state.get('prompt'):
                state.update(prompt=secrets.choice(pool), sampled=now)
                self._set(con, 'reflection', state)
            return state

    def end(self):
        with self.transaction() as con:
            self._expire(con, time.time())
            # Ending supervision releases remaining approval requirements.
            con.execute("UPDATE messages SET outcome='released',decided=? WHERE outcome='pending'", (time.time(),))
            self._set(con, 'active', False)
