"""Root-operated approval store for a single explicit Claude conversation."""
import json
import os
from pathlib import Path
import re
import stat
import sys
import uuid
from contextlib import ExitStack, contextmanager


APPROVALS = Path('/var/lib/rstation-worker-control/approvals')
LIMIT = 16384
OPEN = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK


def canonical_uuid(value):
    return isinstance(value, str) and len(value) == 36 and bool(re.fullmatch(
        r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', value))


def integer(value, minimum=0, maximum=2**64-1):
    return type(value) is int and minimum <= value <= maximum


def identifier(value, limit=200):
    return isinstance(value, str) and 0 < len(value) <= limit and bool(re.fullmatch(r'[!-~]+', value))


def digest(value):
    return isinstance(value, str) and bool(re.fullmatch('[0-9a-f]{64}', value))


def fields(value, *keys):
    return type(value) is dict and value.keys() == set(keys)


def valid_workspace(value):
    return (fields(value, 'path', 'device', 'inode') and isinstance(value['path'], str) and
            0 < len(value['path']) <= 4096 and value['path'].startswith('/') and
            not any(ord(c) < 32 for c in value['path']) and
            integer(value['device']) and integer(value['inode'], 1))


def valid_request(value):
    if not fields(value, 'id', 'spec', 'worker', 'workspace', 'identity', 'route', 'previous_launch',
                  'process_group', 'expected_block', 'status'):
        return False
    spec, identity = value['spec'], value['identity']
    return (canonical_uuid(value['id']) and value['status'] == 'pending' and
            isinstance(value['worker'], str) and bool(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}', value['worker'])) and
            valid_workspace(value['workspace']) and
            fields(spec, 'action', 'expected_session', 'expected_revision') and spec['action'] == 'fresh' and
            identifier(spec['expected_session']) and integer(spec['expected_revision'], maximum=2**63-2) and
            fields(identity, 'base_sha256', 'model', 'control_revision', 'session_id', 'pin', 'provider_revision', 'pool_sha256') and
            digest(identity['base_sha256']) and digest(identity['pool_sha256']) and identifier(identity['model']) and
            type(identity['control_revision']) is int and identity['control_revision'] == spec['expected_revision'] and
            identity['session_id'] == spec['expected_session'] and integer(identity['provider_revision']) and
            (identity['pin'] is None or integer(identity['pin'], 1, 4096)) and
            integer(value['route'], 1, 4096) and (identity['pin'] is None or value['route'] == identity['pin']) and
            integer(value['previous_launch']) and
            (value['process_group'] is None or integer(value['process_group'], 1, 2**31-1)) and
            (value['expected_block'] is None or identifier(value['expected_block'])))


def decode(data):
    if len(data) > LIMIT:
        raise ValueError('Control record exceeds size limit.')
    return json.loads(data)


def read_record(path, *, directory=None, trusted=False):
    with os.fdopen(os.open(path, OPEN, dir_fd=directory), 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or (trusted and (info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o444)):
            raise ValueError('Invalid control record permissions.')
        return decode(stream.read(LIMIT + 1))


@contextmanager
def approval_directory():
    flags = OPEN | os.O_DIRECTORY
    with ExitStack() as stack:
        fd = None
        for part in APPROVALS.parts:
            fd = os.open(part, flags, dir_fd=fd)
            stack.callback(os.close, fd)
            info = os.fstat(fd)
            if info.st_uid != 0 or info.st_mode & 0o022:
                raise ValueError('Untrusted approval directory.')
        yield fd


def approved(request_id):
    if not canonical_uuid(request_id):
        return None
    try:
        with approval_directory() as fd:
            record = read_record(request_id + '.json', directory=fd, trusted=True)
        if (fields(record, 'request', 'native_id') and valid_request(record['request']) and
                record['request']['id'] == request_id and canonical_uuid(record['native_id']) and
                record['native_id'] != record['request']['spec']['expected_session']):
            return record
    except (OSError, ValueError, RecursionError):
        pass
    return None


def issue(request):
    if os.geteuid() != 0:
        raise ValueError('Fresh conversation approval requires root.')
    if not valid_request(request):
        raise ValueError('Invalid fresh conversation proposal.')
    filename = request['id'] + '.json'
    with approval_directory() as fd:
        existing = approved(request['id'])
        if existing and existing['request'] == request:
            return existing
        record = dict(request=request, native_id=str(uuid.uuid4()))
        temp = '.approval-' + uuid.uuid4().hex
        handle = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
        try:
            with os.fdopen(handle, 'w') as stream:
                json.dump(record, stream)
                stream.flush()
                os.fchmod(stream.fileno(), 0o444)
                os.fsync(stream.fileno())
            os.link(temp, filename, src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
            os.fsync(fd)
        finally:
            os.unlink(temp, dir_fd=fd)
    return record


if __name__ == '__main__':
    try:
        if os.geteuid() != 0:
            raise ValueError()
        print(issue(decode(sys.stdin.buffer.read(LIMIT + 1)))['native_id'])
    except (ValueError, OSError, RecursionError):
        raise SystemExit('Approval failed. Check root privileges, the proposal, and the approval store.') from None
