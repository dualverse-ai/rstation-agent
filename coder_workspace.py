"""Small isolated code snapshots and conflict-checked application."""
import difflib
import hashlib
import os
from pathlib import Path
import shutil
import stat
import tempfile

from common import lock, read, save

SKIP={'node_modules','__pycache__','venv','gpu-run','council','coder','AGENTS.md','CLAUDE.md','GEMINI.md',
      'RESEARCH_INSTRUCTIONS.md','COUNCIL.md','STORAGE_USAGE.txt'}
LIMIT=64*1024*1024


class SnapshotError(ValueError):
    """Actionable snapshot validation messages safe to show to the researcher."""


def allowed(parts):
    return all(not p.startswith('.') and p not in SKIP and not any(s in p.lower() for s in ('credential','api_key','api-key','secret')) for p in parts)


def target_path(work,path):
    work=Path(work).resolve();target=(work/path).resolve()
    if not target.is_relative_to(work) or not allowed(target.relative_to(work).parts) or not target.is_dir():
        raise SnapshotError('Choose a code directory within your workspace with --path.')
    return target


def files(root):
    root=Path(root);result={};size=0
    for parent,dirs,names in os.walk(root,followlinks=False):
        dirs[:]=[d for d in dirs if allowed((d,)) and not (Path(parent)/d).is_symlink()]
        for name in names:
            p=Path(parent)/name;rel=p.relative_to(root).as_posix()
            if not allowed((name,)) or p.is_symlink():continue
            if not stat.S_ISREG(p.stat().st_mode):continue
            size+=p.stat().st_size
            if size>LIMIT or len(result)>=10000:
                raise SnapshotError('Code snapshot exceeds 64 MB or 10000 files; choose a smaller --path. Read large research data at its original path.')
            result[rel]=dict(sha256=hashlib.sha256(p.read_bytes()).hexdigest(),mode=stat.S_IMODE(p.stat().st_mode))
    return result


def prepare(root,work,prompt):
    root=Path(root);record=root/'files.json';dest=root/'workspace'
    if record.exists():return read(record)
    first=prompt.splitlines()[0]
    path=first.removeprefix('Code directory: ') if first.startswith('Code directory: ') else '.'
    source=target_path(work,path);manifest=files(source)
    # Rebuild only an uncommitted initial snapshot left by a setup interruption.
    if dest.exists():shutil.rmtree(dest)
    dest.mkdir()
    for name in manifest:
        out=dest/name;out.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(source/name,out)
    if files(dest)!=manifest or files(source)!=manifest:
        raise SnapshotError('Code changed while taking the snapshot; retry when the source is stable.')
    snapshot=dict(source=str(source),base=manifest)
    save(record,snapshot);return snapshot


def changes(root,*,apply=False):
    root=Path(root);snapshot=read(root/'files.json')
    if not snapshot:raise ValueError('No code snapshot is available.')
    source=Path(snapshot['source']);scratch=root/'workspace';base=snapshot['base'];current=files(scratch)
    names=sorted(n for n in base.keys()|current.keys() if base.get(n)!=current.get(n))
    def safe_target(name):
        target=source/name
        if not target.resolve().is_relative_to(source.resolve()) or any(p.is_symlink() for p in [target,*target.parents] if p!=source.parent):
            raise ValueError('Changed target is a symlink: '+name)
        return target
    with lock(root/'apply.lock'):
        def check(name):
            target=safe_target(name)
            actual=dict(sha256=hashlib.sha256(target.read_bytes()).hexdigest(),mode=stat.S_IMODE(target.stat().st_mode)) if target.is_file() else None
            if target.exists() and not target.is_file():raise ValueError('Changed target is not a file: '+name)
            if actual!=base.get(name):raise ValueError('Researcher file changed since snapshot: '+name+'. Ask coder to reconcile in a new task.')
        for name in names:check(name)
        report=[]
        for name in names:
            before=(source/name).read_bytes() if name in base else b''
            after=(scratch/name).read_bytes() if name in current else b''
            try:
                report.extend(difflib.unified_diff(before.decode().splitlines(True),after.decode().splitlines(True),fromfile='a/'+name,tofile='b/'+name))
            except UnicodeError:report.append('Binary file changed: '+name+'\n')
            if name in base and name in current and base[name]['mode']!=current[name]['mode']:report.append('Mode changed: '+name+'\n')
        if not apply:return ''.join(report) or 'No changes.'
        # Backups support rollback if an individual filesystem operation fails.
        originals={n:((source/n).read_bytes(),base[n]['mode']) if n in base else None for n in names}
        applied=[]
        try:
            for name in names:
                check(name)
                target=safe_target(name)
                if name not in current:target.unlink()
                else:write_file(target,(scratch/name).read_bytes(),current[name]['mode'])
                applied.append(name)
        except BaseException:
            for name in reversed(applied):
                target=safe_target(name);old=originals[name]
                # Never roll back an edit made by the researcher after our write.
                actual=dict(sha256=hashlib.sha256(target.read_bytes()).hexdigest(),mode=stat.S_IMODE(target.stat().st_mode)) if target.is_file() else None
                if actual!=current.get(name):continue
                if old:write_file(target,*old)
                else:target.unlink(missing_ok=True)
            raise
        snapshot['base']=current;save(root/'files.json',snapshot)
        return 'Applied '+str(len(names))+' changed files.'


def write_file(path,content,mode):
    path.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(prefix='.coder-',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as f:f.write(content);f.flush();os.fsync(f.fileno());os.fchmod(f.fileno(),mode)
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)
