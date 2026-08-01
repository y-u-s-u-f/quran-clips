"""Tests for egress routing (`qc.proxy`) and the local CONNECT relay (`qc.relay`).

The behaviour that matters and is easy to regress silently:

  * the escalation ORDER is static -> datacenter -> fail, with no direct attempt
    once a pool is configured;
  * one video id sticks to one exit, because a signed googlevideo URL embeds the
    exit that resolved it;
  * proxy passwords never appear in a log line or an error message;
  * no configuration at all still means a plain direct download.

`qc.relay` is exercised end to end against a local upstream, so the CONNECT
handshake is really performed rather than mocked.
"""
import os
import socket
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from qc import config, proxy, relay                            # noqa: E402


class _Env:
    """Set QC_* vars for one test without leaking into the next."""

    def __init__(self, **kw):
        self.kw = kw

    def __enter__(self):
        self.saved = dict(os.environ)
        os.environ.update({k: v for k, v in self.kw.items() if v is not None})
        for k, v in self.kw.items():
            if v is None:
                os.environ.pop(k, None)
        config.reset()
        proxy._sticky.clear()
        return self

    def __exit__(self, *a):
        os.environ.clear()
        os.environ.update(self.saved)
        config.reset()
        proxy._sticky.clear()


STATIC = "u:p@10.0.0.1:1,u:p@10.0.0.2:2"
DC = "d:q@10.9.0.1:9"


class Chain(unittest.TestCase):
    def test_no_pool_means_one_direct_attempt(self):
        with _Env(QC_PROXY_STATIC=None, QC_PROXY_DATACENTER=None,
                  QC_PROXY_ENABLED=None, QC_ENV_FILE="/nonexistent"):
            self.assertFalse(proxy.enabled())
            self.assertEqual(proxy.attempts(), [("direct", None)])

    def test_static_is_tried_before_datacenter(self):
        with _Env(QC_PROXY_STATIC=STATIC, QC_PROXY_DATACENTER=DC,
                  QC_ENV_FILE="/nonexistent"):
            tiers = [t for t, _ in proxy.chain()]
            self.assertEqual(tiers, ["static", "static", "datacenter"])

    def test_no_direct_fallback_once_a_pool_exists(self):
        with _Env(QC_PROXY_STATIC=STATIC, QC_ENV_FILE="/nonexistent"):
            self.assertTrue(all(u for _, u in proxy.attempts()))

    def test_always_with_empty_pool_is_a_named_error(self):
        with _Env(QC_PROXY_ENABLED="always", QC_PROXY_STATIC=None,
                  QC_PROXY_DATACENTER=None, QC_ENV_FILE="/nonexistent"):
            with self.assertRaises(SystemExit) as cm:
                proxy.attempts()
            self.assertIn("QC_PROXY_STATIC", str(cm.exception))

    def test_never_ignores_a_configured_pool(self):
        with _Env(QC_PROXY_ENABLED="never", QC_PROXY_STATIC=STATIC,
                  QC_ENV_FILE="/nonexistent"):
            self.assertFalse(proxy.enabled())
            self.assertEqual(proxy.attempts(), [("direct", None)])

    def test_bare_host_port_gets_a_scheme(self):
        with _Env(QC_PROXY_STATIC="1.2.3.4:8080", QC_ENV_FILE="/nonexistent"):
            self.assertEqual(proxy.chain()[0][1], "http://1.2.3.4:8080")


class Redaction(unittest.TestCase):
    def test_password_is_masked(self):
        got = proxy.redact("http://user:sup3rsecret@1.2.3.4:5")
        self.assertNotIn("sup3rsecret", got)
        self.assertIn("user", got)

    def test_labels_never_carry_the_password(self):
        with _Env(QC_PROXY_STATIC="user:sup3rsecret@1.2.3.4:5",
                  QC_ENV_FILE="/nonexistent"):
            for label, _ in proxy.attempts():
                self.assertNotIn("sup3rsecret", label)

    def test_failure_message_never_carries_the_password(self):
        with _Env(QC_PROXY_STATIC="user:sup3rsecret@127.0.0.1:1",
                  QC_ENV_FILE="/nonexistent"):
            # `false` always fails, so the whole chain is exhausted.
            with self.assertRaises(SystemExit) as cm:
                proxy.run_with_proxy(lambda u: ["false"], why="test")
            self.assertNotIn("sup3rsecret", str(cm.exception))

    def test_url_without_credentials_survives(self):
        self.assertEqual(proxy.redact("http://1.2.3.4:5"), "http://1.2.3.4:5")


class Escalation(unittest.TestCase):
    def test_it_falls_through_to_the_next_endpoint(self):
        """First endpoint fails, second succeeds -> the second is returned."""
        with _Env(QC_PROXY_STATIC=STATIC, QC_ENV_FILE="/nonexistent"):
            first = proxy.chain()[0][1]

            def build(url):
                return ["false"] if url == first else ["true"]

            p, used = proxy.run_with_proxy(build, why="test")
            self.assertEqual(p.returncode, 0)
            self.assertNotEqual(used, first)

    def test_success_pins_the_exit_for_later_calls(self):
        with _Env(QC_PROXY_STATIC=STATIC, QC_ENV_FILE="/nonexistent"):
            first = proxy.chain()[0][1]
            proxy.run_with_proxy(lambda u: ["true"], key="VID", why="test")
            self.assertEqual(proxy.sticky_for("VID"), first)
            # A pinned exit is attempted before the rest of the plan.
            seen = []

            def build(url):
                seen.append(url)
                return ["true"]

            proxy.run_with_proxy(build, key="VID", why="test")
            self.assertEqual(seen[0], first)

    def test_exhausting_every_tier_raises_and_lists_them(self):
        with _Env(QC_PROXY_STATIC=STATIC, QC_PROXY_DATACENTER=DC,
                  QC_ENV_FILE="/nonexistent"):
            with self.assertRaises(SystemExit) as cm:
                proxy.run_with_proxy(lambda u: ["false"], why="test")
            msg = str(cm.exception)
            self.assertIn("3 tried", msg)
            self.assertIn("static -> datacenter", msg)

    def test_custom_check_can_reject_a_zero_exit(self):
        """A yt-dlp bot check exits 1, but a truncated stub can exit 0."""
        with _Env(QC_PROXY_STATIC=STATIC, QC_ENV_FILE="/nonexistent"):
            with self.assertRaises(SystemExit):
                proxy.run_with_proxy(lambda u: ["true"],
                                     check=lambda p: False, why="test")


class Relay(unittest.TestCase):
    def test_no_upstream_yields_none(self):
        with relay.serve(None) as local:
            self.assertIsNone(local)

    def test_connect_handshake_reaches_the_upstream(self):
        """Drive a real CONNECT through the relay to a local fake proxy.

        This is the behaviour ffmpeg cannot do for itself, which is the entire
        reason the relay exists, so it is worth exercising for real.
        """
        seen = {}
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]

        def upstream():
            conn, _ = srv.accept()
            req = b""
            while b"\r\n\r\n" not in req:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                req += chunk
            seen["req"] = req.decode("latin-1")
            conn.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            conn.sendall(b"PAYLOAD")
            conn.close()

        t = threading.Thread(target=upstream, daemon=True)
        t.start()
        try:
            with relay.serve("http://bob:hunter2@127.0.0.1:%d" % port) as local:
                self.assertTrue(local.startswith("http://127.0.0.1:"))
                lp = int(local.rsplit(":", 1)[1])
                c = socket.create_connection(("127.0.0.1", lp), timeout=5)
                c.sendall(b"CONNECT example.com:443 HTTP/1.1\r\n"
                          b"Host: example.com:443\r\n\r\n")
                resp = c.recv(4096)
                self.assertIn(b"200", resp)
                self.assertEqual(c.recv(4096), b"PAYLOAD")
                c.close()
        finally:
            t.join(timeout=5)
            srv.close()

        # The relay must have added the upstream credentials itself -- that is
        # what lets ffmpeg talk to an unauthenticated local port instead.
        self.assertIn("CONNECT example.com:443", seen.get("req", ""))
        self.assertIn("Proxy-Authorization: Basic", seen.get("req", ""))

    def test_relay_listener_is_closed_on_exit(self):
        """The listener must be closed on exit so the thread cannot outlive the
        download it was started for.

        Two tempting assertions are both wrong here. Re-binding the port fails on
        a correctly closed listener (TIME_WAIT), and connecting to it may still
        succeed from the kernel's backlog, so neither is deterministic. Assert the
        socket object itself is closed, which is the actual contract.
        """
        with relay.serve("http://127.0.0.1:1") as local:
            self.assertTrue(local.startswith("http://127.0.0.1:"))
            r = [t for t in threading.enumerate()
                 if isinstance(t, relay._Relay)][-1]
            self.assertEqual(r.sock.fileno() == -1, False)   # open inside
        self.assertEqual(r.sock.fileno(), -1)                # closed after
        self.assertTrue(r._stop)


if __name__ == "__main__":
    unittest.main()
