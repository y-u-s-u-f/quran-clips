"""
qc.relay -- a local CONNECT relay, so ffmpeg can reach a proxied HTTPS URL.

Exists for one measured reason. `yt-dlp --download-sections` (the flag that
fetches only the requested seconds instead of a whole video) always hands the
range fetch to a CHILD ffmpeg, and **ffmpeg cannot CONNECT-tunnel https through
an authenticated proxy**: it returns

    [httpproxy] HTTP error 402 Payment Required

which reads like a billing failure and is not one. On the same endpoint and the
same signed URL, `curl -r 0-0` returns HTTP 206 and a 10 MB non-Google transfer
runs at full speed, so neither the proxy nor the URL is at fault.

Two other things that look like they should work and do not:

  * exporting `http_proxy` / `https_proxy` -- ffmpeg only honours those for plain
    `http://`, and googlevideo is `https://`. The child's `/proc/<pid>/environ`
    shows them set while the transfer sits at 0 bytes, because ffmpeg connects
    DIRECT while the signed URL is bound to the proxy exit.
  * `--force-keyframes-at-cuts` -- makes ffmpeg re-read around the cut points;
    over a proxy this produced a 262-byte stub at 0.75 B/s.

So: listen on 127.0.0.1 with NO authentication, and perform the upstream CONNECT
plus `Proxy-Authorization` here. ffmpeg then only has to talk to a plain local
proxy, which it does correctly, and `--download-sections` keeps working -- which
is the whole point, because pulling a 491s/184MB video to use 150s of it wastes
bandwidth on a metered residential pool.

Used as a context manager:

    with relay.serve(upstream_url) as local:
        ...  # local is "http://127.0.0.1:<port>"
"""
import base64
import contextlib
import select
import socket
import threading


def _pump(a, b):
    """Shuttle bytes both ways until either side closes."""
    try:
        while True:
            r, _, _ = select.select([a, b], [], [], 60)
            if not r:
                return
            for s in r:
                data = s.recv(65536)
                if not data:
                    return
                (b if s is a else a).sendall(data)
    except OSError:
        return


class _Relay(threading.Thread):
    daemon = True

    def __init__(self, upstream):
        super().__init__()
        scheme, _, rest = upstream.partition("://")
        creds, _, hostport = rest.rpartition("@")
        host, _, port = hostport.partition(":")
        self.up = (host, int(port or 8080))
        self.auth = (base64.b64encode(creds.encode()).decode() if creds else None)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(64)
        self.port = self.sock.getsockname()[1]
        self._stop = False

    @property
    def url(self):
        return "http://127.0.0.1:%d" % self.port

    def run(self):
        while not self._stop:
            try:
                client, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(client,),
                             daemon=True).start()

    def _handle(self, client):
        up = None
        try:
            req = b""
            while b"\r\n\r\n" not in req:
                chunk = client.recv(65536)
                if not chunk:
                    return
                req += chunk
            first = req.split(b"\r\n", 1)[0].decode("latin-1")
            parts = first.split()
            if len(parts) < 2:
                return
            up = socket.create_connection(self.up, timeout=30)
            headers = ""
            if self.auth:
                headers = "Proxy-Authorization: Basic %s\r\n" % self.auth

            if parts[0].upper() == "CONNECT":
                target = parts[1]
                up.sendall(("CONNECT %s HTTP/1.1\r\nHost: %s\r\n%s"
                            "Proxy-Connection: keep-alive\r\n\r\n"
                            % (target, target, headers)).encode())
                resp = b""
                while b"\r\n\r\n" not in resp:
                    chunk = up.recv(65536)
                    if not chunk:
                        return
                    resp += chunk
                if b" 200" not in resp.split(b"\r\n", 1)[0]:
                    client.sendall(resp)      # surface the upstream refusal
                    return
                client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            else:
                # Absolute-form plain HTTP: inject auth and forward verbatim.
                head, _, rest = req.partition(b"\r\n")
                up.sendall(head + b"\r\n" + headers.encode() + rest)
            _pump(client, up)
        except OSError:
            return
        finally:
            for s in (client, up):
                if s:
                    try:
                        s.close()
                    except OSError:
                        pass

    def shutdown(self):
        """Close the listener and wait for the accept loop to notice.

        Joining matters: without it a caller can return while the thread is still
        inside `accept()`, so the socket's closure is not yet observable and any
        assertion (or a subsequent bind) races the thread. The loop wakes as soon
        as `close()` makes `accept()` raise.
        """
        self._stop = True
        try:
            self.sock.close()
        except OSError:
            pass
        if self.is_alive():
            self.join(timeout=5)


@contextlib.contextmanager
def serve(upstream):
    """Run a relay to `upstream` for the duration of the block.

    Yields the local proxy URL, or None when `upstream` is None so a direct
    (unproxied) run needs no special-casing at the call site.
    """
    if not upstream:
        yield None
        return
    r = _Relay(upstream)
    r.start()
    try:
        yield r.url
    finally:
        r.shutdown()
