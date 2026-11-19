"""Small loopback HTTP proxy: route model hosts separately from research traffic."""
import select
import socket
import socketserver
import ssl
import threading
from urllib.parse import urlsplit

class Router:
    def __init__(self, model_hosts, research_proxy=None, model_proxy=None, direct_hosts=(), host_proxies=None):
        self.model_hosts = set(model_hosts)
        self.research_proxy, self.model_proxy = research_proxy, model_proxy
        self.direct_hosts = set(direct_hosts) | {"localhost", "127.0.0.1", "::1"}
        self.server = None
        self.host_proxies = host_proxies or {}

    def upstream(self, host):
        if host in self.host_proxies:return self.host_proxies[host]
        # Model hosts may themselves be local relays.
        if host in self.model_hosts: return self.model_proxy
        if host in self.direct_hosts: return None
        return self.research_proxy

    def __enter__(self):
        router = self
        class Handler(socketserver.StreamRequestHandler):
            timeout = 30
            def handle(self):
                remote = None
                connected = False
                try:
                    first = self.rfile.readline(65537)
                    if len(first) > 65536: return
                    method, target, version = first.decode("latin1").strip().split(" ",2)
                    headers = []
                    size = 0
                    while True:
                        line = self.rfile.readline(65537); size += len(line)
                        if size > 65536: return
                        if line in (b"\r\n", b"\n", b""): break
                        # Do not leak authentication supplied to a local proxy onward.
                        if not line.lower().startswith(b"proxy-authorization:"): headers.append(line)
                    parsed = urlsplit("//"+target if method == "CONNECT" else target)
                    host = parsed.hostname
                    port = parsed.port or (443 if method == "CONNECT" else 80)
                    if not host: return
                    upstream = router.upstream(host)
                    if upstream:
                        p = urlsplit(upstream)
                        remote = socket.create_connection((p.hostname, p.port or (443 if p.scheme=="https" else 80)), timeout=30)
                        if p.scheme == "https":
                            remote = ssl.create_default_context().wrap_socket(remote, server_hostname=p.hostname)
                    else:
                        remote = socket.create_connection((host,port), timeout=30)
                    if method == "CONNECT":
                        if upstream:
                            remote.sendall(first + b"Host: " + target.encode("latin1") + b"\r\n\r\n")
                            response = b""
                            while b"\r\n\r\n" not in response:
                                part = remote.recv(1)
                                if not part or len(response)>65536: return
                                response += part
                            if b" 200 " not in response.split(b"\r\n",1)[0]:
                                self.connection.sendall(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n"); return
                        self.connection.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                        connected = True
                    else:
                        destination = target if upstream else (parsed.path or "/") + (("?"+parsed.query) if parsed.query else "")
                        remote.sendall(f"{method} {destination} {version}\r\n".encode("latin1")+b"".join(headers)+b"\r\n")
                        # Copy a bounded request body before tunneling. CLIs use CONNECT
                        # for HTTPS; plain HTTP APIs use Content-Length or chunking.
                        length = next((int(h.split(b":",1)[1]) for h in headers if h.lower().startswith(b"content-length:")), 0)
                        chunked = any(h.lower().startswith(b"transfer-encoding:") and b"chunked" in h.lower() for h in headers)
                        while length:
                            part = self.rfile.read(min(length,65536))
                            if not part: return
                            remote.sendall(part); length -= len(part)
                        if chunked:
                            while True:
                                line=self.rfile.readline(1024); remote.sendall(line)
                                count=int(line.split(b";",1)[0],16)
                                if count:
                                    part=self.rfile.read(count+2); remote.sendall(part)
                                else:
                                    while True:
                                        line=self.rfile.readline(65536); remote.sendall(line)
                                        if line in (b"\r\n",b"\n",b""): break
                                    break
                    # Preserve streaming responses; the socket timeout detects a
                    # silent connection, not the overall research lifetime.
                    sockets = [self.connection,remote]
                    while True:
                        ready,_,_=select.select(sockets,[],[],600)
                        if not ready: return
                        for source in ready:
                            data=source.recv(65536)
                            if not data: return
                            (remote if source is self.connection else self.connection).sendall(data)
                except (OSError, ValueError):
                    # After CONNECT, only the encrypted peer stream belongs on
                    # this socket. A plaintext HTTP error corrupts TLS/HTTP2.
                    if not connected:
                        try: self.connection.sendall(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n")
                        except OSError: pass
                finally:
                    if remote: remote.close()
        class Server(socketserver.ThreadingTCPServer):
            allow_reuse_address=True
            daemon_threads=True
        self.server=Server(("127.0.0.1",0),Handler)
        threading.Thread(target=self.server.serve_forever,daemon=True).start()
        self.url=f"http://127.0.0.1:{self.server.server_address[1]}"
        return self
    def __exit__(self,*args):
        self.server.shutdown(); self.server.server_close()
