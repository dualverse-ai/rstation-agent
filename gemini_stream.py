"""Opt-in, per-launch SSE compatibility for the native Gemini client.

This is an explicit loopback API endpoint, not a CONNECT/TLS interceptor.
Upstream HTTPS uses the normal certificate verification and selected route.
"""
from contextlib import contextmanager
from http.client import HTTPException
import http.server
import re
import secrets
import threading
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


class SSEComments:
    """Remove SSE comment lines, normalize CR/LF, preserve all other fields."""
    def __init__(self):
        self.pending = b''
        self.event = False

    def feed(self, chunk, *, final=False):
        self.pending += chunk
        output = bytearray()
        while True:
            match = re.search(b'[\r\n]', self.pending)
            if match is None:
                break
            pos = match.start()
            if self.pending[pos:] == b'\r' and not final:
                break  # CRLF can straddle network reads.
            width = 2 if self.pending[pos:pos + 2] == b'\r\n' else 1
            line, self.pending = self.pending[:pos], self.pending[pos + width:]
            if line.startswith(b':'):
                continue
            if line:
                self.event = True
                output.extend(line + b'\n')
            elif self.event:
                output.extend(b'\n')
                self.event = False
        if final and self.pending:
            # Do not turn a truncated upstream event into a complete one.
            if not self.pending.startswith(b':'):
                output.extend(self.pending)
            self.pending = b''
        return bytes(output)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # Never forward provider credentials to another origin.


class GeminiStream:
    def __init__(self, base_url):
        parsed = urlsplit(base_url)
        if (parsed.scheme not in {'http', 'https'} or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError('Invalid Gemini upstream URL')
        self.base_url = base_url.rstrip('/')
        self.prefix = '/' + secrets.token_urlsafe(32)
        self.server = None

    def __enter__(self):
        bridge = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *args):
                pass  # Neither credentials nor request content belongs in logs.

            def do_POST(self):
                if not self.path.startswith(bridge.prefix + '/'):
                    self.send_error(404)
                    return
                if self.headers.get('Transfer-Encoding'):
                    self.send_error(411)
                    return
                try:
                    size = int(self.headers['Content-Length'])
                    if size < 0:
                        raise ValueError
                except (TypeError, ValueError):
                    self.send_error(411)
                    return
                body = self.rfile.read(size)
                if len(body) != size:
                    self.close_connection = True
                    return
                excluded = {'host', 'connection', 'content-length', 'accept-encoding',
                            'transfer-encoding', 'proxy-authorization'}
                headers = {k: v for k, v in self.headers.items() if k.lower() not in excluded}
                headers['Accept-Encoding'] = 'identity'
                request = Request(bridge.base_url + self.path[len(bridge.prefix):],
                                  body, headers, method='POST')
                # Environment proxy handling preserves the launcher's explicit
                # model routing; NO_PROXY already includes the loopback client.
                opener = build_opener(ProxyHandler(), NoRedirect())
                started = False
                try:
                    try:
                        response = opener.open(request)
                    except HTTPError as error:
                        response = error  # Preserve HTTP failures for native retries.
                    with response:
                        sse = (200 <= response.status < 300
                               and response.headers.get_content_type() == 'text/event-stream'
                               and response.headers.get('Content-Encoding', 'identity') == 'identity')
                        self.send_response(response.status)
                        for key, value in response.headers.items():
                            # Do not let the native client follow a redirect
                            # outside this endpoint with its provider key.
                            if key.lower() not in {'connection', 'transfer-encoding', 'content-length', 'location'}:
                                self.send_header(key, value)
                        self.send_header('Connection', 'close')
                        self.end_headers()
                        started = True
                        self.close_connection = True
                        normalizer = SSEComments()
                        while True:
                            chunk = response.read1(65536)
                            data = normalizer.feed(chunk, final=not chunk) if sse else chunk
                            if data:
                                self.wfile.write(data)
                                self.wfile.flush()
                            if not chunk:
                                break
                except (OSError, ValueError, HTTPException):
                    self.close_connection = True
                    if not started:
                        self.send_error(502, 'Gemini upstream connection failed')

        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}{self.prefix}'
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


@contextmanager
def native_environment(kind, config, environment):
    if kind != 'gemini' or not config.get('gemini_sse_compat', False):
        yield None  # Inherit exactly the original environment when disabled.
        return
    with GeminiStream(environment['GOOGLE_GEMINI_BASE_URL']) as bridge:
        yield dict(environment, GOOGLE_GEMINI_BASE_URL=bridge.url)
