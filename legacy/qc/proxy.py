"""
qc.proxy -- egress routing for the one stage that touches the network.

`qc source add` is the only command that leaves the machine, and on a cloud host
YouTube blocks the datacentre IP outright ("Sign in to confirm you're not a
bot"). So downloads route through a proxy pool, with a fixed escalation:

    1. static residential   -- the default tier. Sticky exit IPs.
    2. datacentre           -- fallback when every residential exit fails.
    3. fail                 -- no direct-connection attempt is made when a pool
                               is configured, because a bare cloud IP will be
                               bot-checked anyway and burning it teaches nothing.

Why the ORDER is not negotiable, measured rather than assumed:

  * yt-dlp resolves a signed googlevideo URL that embeds the resolving exit IP.
    A ROTATING proxy therefore 403s the media fetch: the URL was signed for a
    different exit than the one fetching it. Only sticky/static exits work for a
    download. (A rotating pool is still right for small metadata calls, which is
    why it is not in this chain at all.)
  * The bot check fires at the PLAYER-API stage, before any media transfer, and
    it burns per exit IP. Retrying one endpoint harder makes it worse; moving to
    the next endpoint is what recovers. Hence round-robin over endpoints rather
    than N attempts against the first.

Configuration lives in `.env` (proxy credentials must never be committed):

    QC_PROXY_STATIC=user:pass@host:port,user:pass@host2:port2,...
    QC_PROXY_DATACENTER=user:pass@host:port,...
    QC_PROXY_ENABLED=auto | always | never

`auto` (the default) uses the pool when one is configured and skips it entirely
when none is -- so a laptop on a home connection needs no configuration and
behaves exactly as before this module existed.
"""
import os
import subprocess
import sys

from . import config

TIERS = ("static", "datacenter")

# A tier's endpoints are tried in order and the working one is remembered for the
# rest of the process: within a single `source add` the metadata call and the
# media fetch MUST use the same exit, or the signed URL is invalid.
_sticky = {}


def _entries(tier):
    """Parsed endpoints for a tier, in order. Each is a proxy URL string."""
    raw = config.var("QC_PROXY_%s" % tier.upper()) or ""
    if not raw:
        # `[proxy] static = [...]` in qc.toml is accepted as a list or a string.
        val = config.get("proxy", tier)
        if isinstance(val, (list, tuple)):
            raw = ",".join(str(v) for v in val)
        elif val:
            raw = str(val)
    out = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        out.append(chunk if "://" in chunk else "http://" + chunk)
    return out


def mode():
    """`auto` | `always` | `never`."""
    return str(config.var("QC_PROXY_ENABLED")
               or config.get("proxy", "enabled") or "auto").lower()


def configured():
    """True when any tier has at least one endpoint."""
    return any(_entries(t) for t in TIERS)


def enabled():
    """Should this run use the pool at all?"""
    m = mode()
    if m == "never":
        return False
    if m == "always":
        return True
    return configured()


def chain():
    """[(tier, proxy_url), ...] in the order they should be attempted."""
    out = []
    for tier in TIERS:
        for url in _entries(tier):
            out.append((tier, url))
    return out


def redact(url):
    """A proxy URL safe to print: the password becomes `***`.

    Every log line and error message goes through this. A traceback that leaks a
    residential proxy password into a terminal transcript is a credential leak,
    and these URLs are pasted into issues.
    """
    if not url or "@" not in url:
        return url or ""
    scheme, _, rest = url.partition("://")
    creds, _, host = rest.rpartition("@")
    if ":" in creds:
        user, _, _pw = creds.partition(":")
        creds = "%s:***" % user
    return "%s://%s@%s" % (scheme, creds, host) if scheme else "%s@%s" % (creds, host)


def describe():
    """[(tier, count, source)] for `qc doctor`."""
    rows = []
    for tier in TIERS:
        var = "QC_PROXY_%s" % tier.upper()
        src = config.var_source(var) or ("qc.toml" if config.get("proxy", tier) else None)
        rows.append((tier, len(_entries(tier)), src))
    return rows


def attempts():
    """The full attempt plan: [(label, proxy_url_or_None), ...].

    When no pool is configured (or it is disabled) this is a single direct
    attempt, which keeps the no-config path identical to plain yt-dlp.
    """
    if not enabled():
        return [("direct", None)]
    plan = [("%s %s" % (t, redact(u)), u) for t, u in chain()]
    if not plan:
        # mode=always with nothing configured is a misconfiguration worth naming
        # rather than silently going direct.
        raise SystemExit(
            "QC_PROXY_ENABLED=always but no endpoints are configured.\n"
            "  Set QC_PROXY_STATIC (and optionally QC_PROXY_DATACENTER) in .env\n"
            "  as user:pass@host:port[,user:pass@host:port...]")
    return plan


def sticky_for(key):
    """The endpoint already proven to work for `key` in this process, if any."""
    return _sticky.get(key)


def remember(key, url):
    """Pin `key` to `url` so later calls in the same run reuse that exit."""
    if url:
        _sticky[key] = url


def run_with_proxy(build_cmd, key=None, check=None, why="fetch"):
    """Run `build_cmd(proxy_url)` over the attempt plan until one succeeds.

    `build_cmd` takes a proxy URL (or None for a direct attempt) and returns an
    argv list. `check(completed_process) -> bool` decides success; the default is
    a zero exit status. Returns (CompletedProcess, proxy_url_used).

    A previously successful endpoint for `key` is tried FIRST, which is what
    keeps a signed URL and its media fetch on one exit.
    """
    plan = attempts()
    pinned = sticky_for(key) if key else None
    if pinned:
        plan = ([(("pinned %s" % redact(pinned)), pinned)]
                + [p for p in plan if p[1] != pinned])

    failures = []
    for label, url in plan:
        if len(plan) > 1:
            print("  -> %s via %s" % (why, label), file=sys.stderr)
        env = dict(os.environ)
        if url:
            # Exported for any CHILD process that honours the standard vars.
            # yt-dlp is passed --proxy explicitly by its caller; ffmpeg ignores
            # these for https and needs -http_proxy, which is why the media
            # fetch does not rely on them.
            env["http_proxy"] = env["https_proxy"] = url
            env["HTTP_PROXY"] = env["HTTPS_PROXY"] = url
        p = subprocess.run(build_cmd(url), env=env,
                           capture_output=True, text=True)
        ok = (p.returncode == 0) if check is None else check(p)
        if ok:
            if key:
                remember(key, url)
            return p, url
        tail = (p.stderr or p.stdout or "").strip().splitlines()
        failures.append("%s: %s" % (label, tail[-1][:150] if tail else
                                    "exit %d" % p.returncode))

    raise SystemExit(
        "every egress attempt failed (%d tried):\n%s\n"
        "  Tiers are attempted static -> datacenter, then this error; no direct\n"
        "  connection is tried when a pool is configured. Check the pools in\n"
        "  .env, or set QC_PROXY_ENABLED=never to go direct."
        % (len(failures), "\n".join("    " + f for f in failures)))
