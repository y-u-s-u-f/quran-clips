"""pipeline/fetch.py -- source intake. One folder per source under sources/.

    python3 pipeline/fetch.py <youtube-url-or-id>          download
    python3 pipeline/fetch.py /path/to/video.mp4           take in a local file
    python3 pipeline/fetch.py <src> --name my-slug         pick the folder name
    python3 pipeline/fetch.py <url> --proxy                route via .env pool
    python3 pipeline/fetch.py <url> --proxy http://u:p@h:p explicit proxy
    python3 pipeline/fetch.py <url> --timestamps 12:30-15:00
                                                           download a section

Produces sources/<id>/:
    source.mp4      the video (downloaded, or a symlink to the local file --
                    re-encoded instead when the local file is above 30fps)
    captions.srt    YouTube's Arabic auto-captions, only when YouTube has them

Everything is held to 30fps at intake: the reels render at 30, so surplus
frames only cost decode and filter time in every stage downstream.

A hand-made source needs no fetch at all: create sources/<name>/ and put a
source.mp4 in it.

YouTube <id> is the 11-char video id; a local file's folder is its filename
slug unless --name says otherwise. Existing files are never overwritten --
re-downloading can silently hand back a different re-encode than the one a
shipped reel's numbers were derived against.

Proxy: `--proxy` with no value enables the pool configured in .env
(QC_PROXY_STATIC then QC_PROXY_DATACENTER, comma-separated user:pass@host:port
-- static residential first, datacentre fallback). One exit is used for the
whole fetch: a signed googlevideo URL embeds the exit IP that resolved it, so
the metadata call and the media fetch must leave from the same exit, and a
rotating proxy 403s the download outright. Credentials are redacted in output.

--timestamps limits the download to a section via yt-dlp --download-sections.
Caveat: combined with an authenticated proxy the range fetch runs through a
child ffmpeg that cannot CONNECT-tunnel https, so for proxied hosts download
the full video instead and use the reel config's `trim`.
"""
import argparse
import json
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCES = os.path.join(ROOT, "sources")

# Reels render at 30fps, so nothing above it survives to the screen. Held down
# at intake -- once here -- rather than in every downstream decode.
MAX_FPS = 30


# --- machine config (.env) -------------------------------------------------

def _dotenv():
    """KEY=value pairs from ROOT/.env; process environment wins on conflict."""
    path = os.path.join(ROOT, ".env")
    out = {}
    if os.path.exists(path):
        for raw in open(path, encoding="utf-8"):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].lstrip()
            k, sep, v = line.partition("=")
            if sep:
                v = v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                    v = v[1:-1]
                elif " #" in v:
                    v = v.split(" #", 1)[0].rstrip()
                out[k.strip()] = v
    return out


_ENV = None


def envvar(name, default=None):
    global _ENV
    if os.environ.get(name):
        return os.environ[name]
    if _ENV is None:
        _ENV = _dotenv()
    return _ENV.get(name) or default


def yt_dlp():
    return envvar("QC_YT_DLP", "yt-dlp")


def ffprobe():
    return envvar("QC_FFPROBE", "ffprobe")


def ffmpeg():
    return envvar("QC_FFMPEG", "ffmpeg")


# --- proxy pool ------------------------------------------------------------

def proxy_pool():
    """[(url, redacted_label), ...] -- static residential tier first."""
    pool = []
    for tier, key in (("static", "QC_PROXY_STATIC"),
                      ("datacenter", "QC_PROXY_DATACENTER")):
        for ep in (envvar(key) or "").split(","):
            ep = ep.strip()
            if not ep:
                continue
            url = ep if "://" in ep else "http://" + ep
            host = url.rsplit("@", 1)[-1]
            pool.append((url, "%s %s" % (tier, host)))
    return pool


def redact(text):
    return re.sub(r"(https?://)[^@/\s]+@", r"\1***@", text or "")


# --- helpers ---------------------------------------------------------------

def video_id(src):
    """A full YouTube URL, a youtu.be link, or a bare 11-char id -> id or None."""
    src = (src or "").strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", src):
        return src
    m = re.search(r"(?:v=|/shorts/|youtu\.be/|/embed/|/live/)([A-Za-z0-9_-]{11})",
                  src)
    return m.group(1) if m else None


def slugify(name):
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-.")
    return s or "source"


def probe_duration(path):
    """Seconds, or 0.0 when the file cannot be probed at all."""
    p = subprocess.run([ffprobe(), "-v", "error", "-show_entries",
                        "format=duration", "-of", "json", path],
                       capture_output=True, text=True)
    try:
        return float(json.loads(p.stdout)["format"]["duration"])
    except Exception:
        return 0.0


def video_fps(path):
    """Frames per second of the first video stream; 0.0 when there is none."""
    p = subprocess.run([ffprobe(), "-v", "error", "-select_streams", "v:0",
                        "-show_entries", "stream=avg_frame_rate",
                        "-of", "default=nw=1:nk=1", path],
                       capture_output=True, text=True)
    num, _, den = (p.stdout or "").strip().partition("/")
    try:
        return float(num) / (float(den) or 1.0)
    except ValueError:
        return 0.0


def audio_codec(path):
    p = subprocess.run([ffprobe(), "-v", "error", "-select_streams", "a:0",
                        "-show_entries", "stream=codec_name",
                        "-of", "default=nw=1:nk=1", path],
                       capture_output=True, text=True)
    out = (p.stdout or "").strip()
    return out.splitlines()[0].strip() if out else ""


def ensure_aac(path):
    """Guarantee the source's audio is AAC. -> True if it had to transcode.

    OPUS-IN-MP4 DEADLOCKS FFMPEG at some seek points -- the demuxer blocks in
    tq_send while the filter and encoder threads block in tq_receive, 0% CPU,
    forever, and the output is left with no moov atom. yt-dlp happily muxes
    YouTube's Opus audio into an .mp4, so the guarantee is enforced on the
    file rather than merely requested in the format string. The VIDEO IS
    COPIED, never re-encoded.
    """
    codec = audio_codec(path)
    if codec in ("aac", ""):
        return False
    tmp = path + ".aac.mp4"
    print("  audio   : %s -> aac (opus in mp4 deadlocks ffmpeg; video copied)"
          % codec)
    rc = subprocess.run([ffmpeg(), "-hide_banner", "-nostats", "-loglevel",
                         "error", "-y", "-i", path, "-map", "0:v:0",
                         "-map", "0:a:0", "-c:v", "copy", "-c:a", "aac",
                         "-b:a", "192k", "-movflags", "+faststart",
                         tmp]).returncode
    if rc != 0 or not os.path.exists(tmp):
        if os.path.exists(tmp):
            os.remove(tmp)
        raise SystemExit("could not transcode %s audio to AAC" % path)
    os.replace(tmp, path)
    return True


def usable(path, min_bytes=100_000):
    """A downloaded file is usable only when it has real size AND a probeable
    duration -- a stalled or stub fetch leaves a file that is present and
    worthless, so mere existence is never accepted."""
    return (os.path.exists(path) and os.path.getsize(path) >= min_bytes
            and probe_duration(path) > 0)


# --- YouTube ---------------------------------------------------------------

# Prefer a real <=1080p video+audio pair, and prefer M4A (AAC) audio -- see
# ensure_aac for why an Opus track in an MP4 is not acceptable here.
# <=30fps first: reels are rendered at 30, so a 60fps rendition is a bigger
# download whose every other frame is decoded and filtered only to be dropped
# again. The unconstrained selectors stay as fallbacks -- a video published
# only at 60fps must still fetch.
FORMAT = ("bv*[height<=1080][fps<=%(fps)d]+ba[ext=m4a]/"
          "bv*[height<=1080][fps<=%(fps)d]+ba/"
          "bv*[height<=1080]+ba[ext=m4a]/bv*[height<=1080]+ba/"
          "b[height<=1080]/bv*+ba/b") % {"fps": MAX_FPS}

# Player-client ladder. The bot check ("Sign in to confirm you're not a bot")
# fires per exit IP AND per player client at the player API, before any bytes
# move, so recovery is the next client, never retrying one harder. Measured
# 2026-08 over 105 endpoints x 5 clients: `tv_simply` resolved on 31 exits
# where the yt-dlp default and `web_embedded` failed on all 105, so it leads.
# `web_embedded` stays last: it is the one that still exposes 1080p when the
# others answer DRM-only.
#
# `tv_simply` costs 1080p unless a GVS PO token is available: without one it
# skips its https formats and the selector above silently settles for 640x360,
# exiting 0 with a real (small) file that the stub gate accepts. A local bgutil
# provider on 127.0.0.1:4416 supplies the token; check it is up before a fetch
# and read the printed WxH afterwards, since only that reveals the downgrade.
# With no provider running, `--client android_vr` pins a client whose formats
# need no token and reaches 1080p.
PLAYER_CLIENTS = ["tv_simply", "android_vr", "ios", "tv", "web_safari",
                  "web_embedded"]


def _run_ytdlp(args, proxy=None, capture=False):
    cmd = [yt_dlp()] + args + (["--proxy", proxy] if proxy else [])
    if capture:
        p = subprocess.run(cmd, capture_output=True, text=True)
        return p.returncode, p.stdout, redact(p.stderr)
    p = subprocess.run(cmd)
    return p.returncode, "", ""


def _with_fallback(args, proxy=None, capture=False, client=None):
    """Walk PLAYER_CLIENTS until one resolves. `client` pins a known-good one
    (the caller passes back what metadata succeeded with, so the media fetch
    does not re-pay the ladder)."""
    order = [client] if client else PLAYER_CLIENTS
    rc = out = err = None
    for cl in order:
        cl_args = ["--extractor-args", "youtube:player_client=%s" % cl]
        rc, out, err = _run_ytdlp(cl_args + args, proxy, capture)
        if rc == 0:
            return rc, out, err, cl
        print("  player_client=%s failed (rc=%d)" % (cl, rc), file=sys.stderr)
    return rc, out, err, None


def parse_ts(spec):
    """'MM:SS' / 'HH:MM:SS' / bare seconds -> a string yt-dlp understands."""
    spec = spec.strip()
    if not re.fullmatch(r"[\d:.]+", spec):
        raise SystemExit("bad timestamp %r" % spec)
    return spec


def fetch_youtube(vid, out_dir, proxy=None, timestamps=None, client=None):
    url = "https://www.youtube.com/watch?v=%s" % vid
    dst = os.path.join(out_dir, "source.mp4")

    # metadata first: title/duration are worth having on screen before minutes
    # of download, and a bot check fires here, before any bytes move.
    rc, out, err, client = _with_fallback(["-J", "--no-playlist", url], proxy,
                                          capture=True, client=client)
    if rc != 0:
        raise RuntimeError("yt-dlp metadata failed:\n%s"
                           % ((err or out) or "")[-2000:])
    print("  client  : %s" % client)
    meta = json.loads(out)
    print("  %s | %ss | %sx%s | %s" % (
        meta.get("title"), meta.get("duration"), meta.get("width"),
        meta.get("height"), meta.get("uploader") or meta.get("channel")))

    if usable(dst):
        print("  video   : %s  (reused)" % os.path.relpath(dst, ROOT))
    else:
        args = ["--no-playlist", "-f", FORMAT, "--merge-output-format", "mp4",
                "-o", os.path.join(out_dir, "source.%(ext)s"), url]
        if timestamps:
            a, _, b = timestamps.partition("-")
            args = ["--download-sections",
                    "*%s-%s" % (parse_ts(a), parse_ts(b))] + args
        rc, _, _, _ = _with_fallback(args, proxy, client=client)
        if rc != 0 or not usable(dst):
            raise RuntimeError(
                "download failed for %s (file %s)" % (
                    vid, "missing" if not os.path.exists(dst)
                    else "present but stub/unprobeable -- not accepted"))
        ensure_aac(dst)
        print("  video   : %s  [audio %s]"
              % (os.path.relpath(dst, ROOT), audio_codec(dst) or "none"))

    srt = os.path.join(out_dir, "captions.srt")
    if os.path.exists(srt):
        print("  captions: %s  (reused)" % os.path.relpath(srt, ROOT))
        return
    _with_fallback(["--no-playlist", "--skip-download", "--write-auto-subs",
                    "--sub-lang", "ar-orig", "--convert-subs", "srt",
                    "-o", os.path.join(out_dir, "source.%(ext)s"), url],
                   proxy, client=client)
    got = os.path.join(out_dir, "source.ar-orig.srt")
    if os.path.exists(got):
        os.replace(got, srt)
        print("  captions: %s" % os.path.relpath(srt, ROOT))
    else:
        print("  captions: none (no Arabic auto-captions on this video; "
              "transcribe.py covers it)")


# --- local files -----------------------------------------------------------

def fetch_local(path, out_dir):
    """Symlink the file in place. A source ABOVE MAX_FPS is re-encoded down to
    it instead: the renderers drop the surplus frames anyway, so carrying them
    only buys every later pass (bar-colour sample, loudnorm, the render's own
    decode + grade) twice the work. The user's own file is never touched."""
    src = os.path.abspath(path)
    if not os.path.isfile(src):
        raise SystemExit("not a file: %s" % src)
    fps = video_fps(src)
    ext = ".mp4" if fps > MAX_FPS else os.path.splitext(src)[1].lower()
    dst = os.path.join(out_dir, "source" + ext)
    if os.path.islink(dst) or os.path.exists(dst):
        print("  video   : %s  (already present, untouched)"
              % os.path.relpath(dst, ROOT))
        return
    if fps <= MAX_FPS:
        os.symlink(src, dst)
        print("  video   : %s -> %s" % (os.path.relpath(dst, ROOT), src))
        return
    print("  fps     : %.3f -> %d (re-encoded once, at intake)" % (fps, MAX_FPS))
    # crf 16: this copy becomes the master every reel is cut from, so it is
    # encoded well above the crf 18 the reels themselves are delivered at.
    acodec = ["-c:a", "copy"] if audio_codec(src) == "aac" else \
        ["-c:a", "aac", "-b:a", "192k"]
    tmp = dst + ".part.mp4"
    rc = subprocess.run([ffmpeg(), "-hide_banner", "-nostats", "-loglevel",
                         "error", "-y", "-i", src, "-r", str(MAX_FPS),
                         "-c:v", "libx264", "-preset", "medium", "-crf", "16",
                         "-pix_fmt", "yuv420p"] + acodec
                        + ["-movflags", "+faststart", tmp]).returncode
    if rc != 0 or not usable(tmp):
        if os.path.exists(tmp):
            os.remove(tmp)
        raise SystemExit("could not re-encode %s to %dfps" % (src, MAX_FPS))
    os.replace(tmp, dst)
    print("  video   : %s  [from %s]" % (os.path.relpath(dst, ROOT), src))


# --- main ------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(
        description="fetch a source video into sources/<id>/")
    ap.add_argument("source", help="YouTube URL / 11-char id / local file path")
    ap.add_argument("--name", help="folder name (default: video id / file slug)")
    ap.add_argument("--proxy", nargs="?", const=True, default=None,
                    help="route through the .env proxy pool, or a given URL")
    ap.add_argument("--timestamps", metavar="A-B",
                    help="download only this section (MM:SS-MM:SS)")
    ap.add_argument("--client", choices=PLAYER_CLIENTS,
                    help="pin one player client instead of walking the ladder "
                         "(android_vr when tv_simply lands at 640x360)")
    a = ap.parse_args(argv)

    vid = video_id(a.source)
    is_local = vid is None and (os.path.exists(a.source) or os.sep in a.source)
    if vid is None and not is_local:
        raise SystemExit("cannot parse %r as a YouTube URL/id, and it is not "
                         "a local file" % a.source)

    name = slugify(a.name) if a.name else (vid or slugify(
        os.path.splitext(os.path.basename(a.source))[0]))
    out_dir = os.path.join(SOURCES, name)
    os.makedirs(out_dir, exist_ok=True)
    print("source %s/" % os.path.relpath(out_dir, ROOT))

    if is_local:
        if a.proxy or a.timestamps:
            print("  (--proxy/--timestamps ignored for a local file)")
        fetch_local(a.source, out_dir)
        return 0

    # Proxy plan: an explicit URL is one attempt; `--proxy` alone walks the
    # .env pool in its fixed order. One exit per whole fetch (sticky).
    if a.proxy is True:
        pool = proxy_pool()
        if not pool:
            raise SystemExit("--proxy given but no QC_PROXY_STATIC/"
                             "QC_PROXY_DATACENTER configured in .env")
    elif a.proxy:
        pool = [(a.proxy if "://" in a.proxy else "http://" + a.proxy,
                 "explicit")]
    else:
        pool = [(None, "direct")]

    last = None
    for url, label in pool:
        if label != "direct":
            print("  egress  : %s" % label)
        try:
            fetch_youtube(vid, out_dir, proxy=url, timestamps=a.timestamps,
                          client=a.client)
            return 0
        except RuntimeError as e:
            last = e
            print("  attempt via %s failed: %s" % (label, redact(str(e))),
                  file=sys.stderr)
    raise SystemExit(redact(str(last)) if last else "fetch failed")


if __name__ == "__main__":
    sys.exit(main())
