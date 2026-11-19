"""Read-only workspace tools for Codex council sessions (MCP over stdio)."""
import json
import os
from pathlib import Path
import sys


class Files:
    def __init__(self, root, *peers):
        self.root = Path(root).resolve()
        self.roots = [self.root, *(Path(peer).resolve() for peer in peers)]

    def path(self, value):
        path = Path(value)
        path = (path if path.is_absolute() else self.root/path).resolve()
        root = next((root for root in self.roots if path.is_relative_to(root)), None)
        if root is None:
            raise ValueError('Use a path within the listed researcher workspaces: '+', '.join(map(str,self.roots)))
        parts = path.relative_to(root).parts
        if any(part.startswith('.') or part.lower() in {'credentials.json', 'auth.json', 'tokens.json'} for part in parts):
            raise ValueError('Private files and CLI configuration are outside council research access.')
        return path

    def call(self, name, args):
        path = self.path(args.get('path', '.'))
        if name == 'list_directory':
            start = max(0, int(args.get('offset', 0)))
            entries = []
            for entry in sorted(path.iterdir(), key=lambda p: p.name):
                try: self.path(entry)
                except ValueError: continue
                entries.append(entry)
            return '\n'.join(p.name+('/' if p.is_dir() else '') for p in entries[start:start+200]) + (
                '\nMore entries: use offset '+str(start+200) if len(entries)>start+200 else '')
        if name == 'read_file':
            start = max(1, int(args.get('start_line', 1)))
            count = min(300, max(1, int(args.get('line_count', 150))))
            lines = []
            with path.open(encoding='utf-8') as stream:
                for number, line in enumerate(iter(lambda: stream.readline(20001), ''), 1):
                    if len(line) > 20000:
                        raise ValueError('This file contains very long lines or binary data; read its text summary instead.')
                    if number >= start+count:
                        lines.append('More lines: continue at '+str(number)); break
                    if number >= start:
                        lines.append(str(number)+': '+line.rstrip('\n')[:2000])
            return '\n'.join(lines)
        if name == 'search_text':
            query = args['query']
            if not query: raise ValueError('Provide a nonempty literal search query.')
            matches = []
            # No shell, imports of research code, or traversal through symlinks.
            def walk():
                for directory, dirs, names in os.walk(path, followlinks=False):
                    dirs[:] = [d for d in dirs if not d.startswith('.') and d not in {'node_modules', '__pycache__'}]
                    for name in names:
                        if not name.startswith('.'): yield Path(directory)/name
            paths = [path] if path.is_file() else walk()
            for candidate in paths:
                if candidate.is_symlink() or not candidate.is_file(): continue
                if candidate.stat().st_size > 2*1024*1024: continue
                try:
                    candidate = self.path(candidate)
                    with candidate.open(encoding='utf-8') as stream:
                        for number, line in enumerate(stream, 1):
                            if query in line:
                                label = os.path.relpath(candidate, self.root)
                                matches.append(f'{label}:{number}: {line.strip()[:1000]}')
                                if len(matches) >= 100:
                                    return '\n'.join(matches)+'\nFirst 100 matches; narrow the path or query.'
                except (OSError, UnicodeError, ValueError): continue
            return '\n'.join(matches) or 'No matches.'
        raise ValueError('Unknown read-only tool.')


def tools():
    def tool(name, description, properties, required):
        return dict(name=name, description=description, inputSchema=dict(type='object',properties=properties,required=required),
                    annotations=dict(readOnlyHint=True,destructiveHint=False,openWorldHint=False))
    path={'type':'string','description':'Absolute path or path relative to the researcher workspace.'}
    return [tool('list_directory','List researcher workspace files; use offset for more entries.',
                 {'path':path,'offset':{'type':'integer'}},['path']),
            tool('read_file','Read a text or source file without executing it. Continue with start_line for longer files.',
                 {'path':path,'start_line':{'type':'integer'},'line_count':{'type':'integer'}},['path']),
            tool('search_text','Find literal text in research source and documents. Narrow path to a relevant project directory.',
                 {'path':path,'query':{'type':'string'}},['path','query'])]


def serve(root, *peers):
    files=Files(root, *peers)
    for line in sys.stdin:
        message=json.loads(line)
        if 'id' not in message: continue
        method=message.get('method'); params=message.get('params',{})
        if method=='initialize':
            result=dict(protocolVersion=params.get('protocolVersion','2024-11-05'),capabilities={'tools':{}},
                        serverInfo={'name':'research-files','version':'0.1.0'},
                        instructions='Read-only access to researcher workspaces '+', '.join(map(str,files.roots))+'. Relative paths use '+str(files.root)+'. Read paths supplied in the question; list and search when more context is needed.')
        elif method=='tools/list': result={'tools':tools()}
        elif method=='tools/call':
            try: result={'content':[{'type':'text','text':files.call(params['name'],params.get('arguments',{}))}]}
            except (OSError,ValueError,KeyError,TypeError) as exc:
                result={'isError':True,'content':[{'type':'text','text':str(exc)}]}
        elif method=='ping': result={}
        else:
            print(json.dumps({'jsonrpc':'2.0','id':message['id'],'error':{'code':-32601,'message':'Method not found'}}),flush=True); continue
        print(json.dumps({'jsonrpc':'2.0','id':message['id'],'result':result}),flush=True)


if __name__=='__main__': serve(*sys.argv[1:])
