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
                    re-encoded instead when it is above 30fps)
    captions.srt    YouTube's Arabic auto-captions, only when YouTube has them
    captions.none   sentinel: captions resolved and YouTube has none (or the
                    fetch was a --timestamps section), so a re-fetch of a
                    complete source never touches the network

Everything is held to 30fps at intake: the reels render at 30, so surplus
frames only cost decode and filter time in every stage downstream. A source
above 30fps is re-encoded down once, here.

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
rotating proxy 403s the download outright. Credentials are redacted in output
and reach yt-dlp through its environment, never its argv.

One player-API request per fresh fetch: the metadata call's JSON is handed
back to yt-dlp (--load-info-json) for the media and captions, so those two
fetch only googlevideo/timedtext URLs and never meet the bot check.

--timestamps limits the download to a section via yt-dlp --download-sections.
YouTube's captions are timed against the full video, so a section fetch
records captions.none instead of a mistimed captions.srt. Caveat: combined
with an authenticated proxy the range fetch runs through a child ffmpeg that
cannot CONNECT-tunnel https, so for proxied hosts download the full video
instead and use the reel config's `trim`.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile

import generate  # stdlib + quran.py only at import
from generate import envvar  # the one .env reader

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCES = os.path.join(ROOT, "sources")

# Reels render at 30fps, so nothing above it survives to the screen. Held down
# at intake -- once here -- rather than in every downstream decode.
MAX_FPS = 30


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


def video_info(path):
    """(width, height, fps) of the first video stream; zeros when there is
    none."""
    p = subprocess.run([ffprobe(), "-v", "error", "-select_streams", "v:0",
                        "-show_entries", "stream=width,height,avg_frame_rate",
                        "-of", "json", path],
                       capture_output=True, text=True)
    try:
        s = json.loads(p.stdout)["streams"][0]
        num, _, den = s["avg_frame_rate"].partition("/")
        return int(s["width"]), int(s["height"]), \
            float(num) / (float(den or 1) or 1.0)
    except (ValueError, KeyError, IndexError):
        return 0, 0, 0.0


def audio_codec(path):
    p = subprocess.run([ffprobe(), "-v", "error", "-select_streams", "a:0",
                        "-show_entries", "stream=codec_name",
                        "-of", "default=nw=1:nk=1", path],
                       capture_output=True, text=True)
    out = (p.stdout or "").strip()
    return out.splitlines()[0].strip() if out else ""


def ensure_aac(path, dst=None):
    """Guarantee the source's audio is AAC, writing to `dst` (default: in
    place). -> True if it had to transcode.

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
    dst = dst or path
    tmp = dst + ".aac.mp4"
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
    os.replace(tmp, dst)
    return True


def usable(path):
    """A downloaded file is usable only when it has real size AND a probeable
    duration -- a stalled or stub fetch leaves a file that is present and
    worthless, so mere existence is never accepted."""
    return (os.path.exists(path) and os.path.getsize(path) >= 100_000
            and probe_duration(path) > 0)


def cap_fps(src, dst):
    """Re-encode `src` to MAX_FPS at `dst` (they may be the same path). The
    renderers drop the surplus frames anyway, so carrying them only buys
    every later pass (bar-colour sample, loudnorm, the render's own decode +
    grade) twice the work. Audio leaves as AAC -- see ensure_aac."""
    print("  fps     : %.3f -> %d (re-encoded once, at intake)"
          % (video_info(src)[2], MAX_FPS))
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


def describe(dst):
    """The one line that reveals a quality downgrade: what actually landed."""
    w, h, fps = video_info(dst)
    return "%dx%d %gfps, audio %s" % (w, h, round(fps, 3),
                                      audio_codec(dst) or "none")


# --- YouTube ---------------------------------------------------------------

# Prefer a real <=1080p video+audio pair, and prefer M4A (AAC) audio -- see
# ensure_aac for why an Opus track in an MP4 is not acceptable here.
FORMAT = ("bv*[height<=1080]+ba[ext=m4a]/bv*[height<=1080]+ba/"
          "b[height<=1080]/bv*+ba/b")
# Resolution outranks frame rate, then the smallest-surplus rate wins. A hard
# [fps<=30] filter instead settled for 720p30 on aqz-KE-bpKQ, whose 1080p
# exists only at 60 (measured 2026-09); a 60fps download is capped by cap_fps.
SORT = "res:1080,fps:%d" % MAX_FPS

# Player-client ladder. The bot check ("Sign in to confirm you're not a bot")
# fires per exit IP AND per player client at the player API, before any bytes
# move, so recovery is the next client, never retrying one harder.
#
# Measured 2026-09, yt-dlp 2026.08.19, direct, no PO-token provider, on
# YkXjYyKwHJ4 and 94bFKq5PUXI: `web_embedded` offered 1080p / 2160p;
# `tv_simply` and `android_vr` offered only 360p (they skip their https formats
# without a GVS PO token); `ios`, `tv` and `web_safari` offered no video at all,
# so they are gone. `web_embedded` leads because it is the only one that
# reaches 1080p unaided. The other two stay because they resolved past the bot
# check on exits where `web_embedded` did not (2026-08: `tv_simply` on 31 of
# 105), and a local bgutil provider on 127.0.0.1:4416 restores their 1080p.
PLAYER_CLIENTS = ["web_embedded", "tv_simply", "android_vr"]
# A client whose formats top out below this is passed over for the next one:
# the PO-token cap lands at 640x360 with rc 0 and a real file, which nothing
# else would catch. A pinned `--client` accepts whatever it offers -- that is
# how a genuinely low-resolution source gets in.
MIN_HEIGHT = 720


def best_height(meta):
    """Tallest downloadable video format the metadata call offered."""
    return max([f.get("height") or 0 for f in meta.get("formats") or []
                if f.get("vcodec") not in (None, "none") and f.get("url")]
               or [0])


def _run_ytdlp(args, proxy=None, capture=False):
    env = None
    if proxy:
        env = dict(os.environ, HTTP_PROXY=proxy, HTTPS_PROXY=proxy,
                   http_proxy=proxy, https_proxy=proxy)
    cmd = [yt_dlp()] + args
    if capture:
        p = subprocess.run(cmd, capture_output=True, text=True, env=env)
        return p.returncode, p.stdout, redact(p.stderr)
    p = subprocess.run(cmd, env=env)
    return p.returncode, "", ""


def _resolve(url, proxy=None, client=None):
    """Metadata call. Walk PLAYER_CLIENTS until one resolves with formats of
    at least MIN_HEIGHT; `client` pins one and accepts what it offers.
    -> (json_text, meta, client)."""
    order = [client] if client else PLAYER_CLIENTS
    err = ""
    low = []
    for cl in order:
        rc, out, err = _run_ytdlp(
            ["--extractor-args", "youtube:player_client=%s" % cl,
             "-J", "--no-playlist", url], proxy, capture=True)
        if rc != 0:
            print("  player_client=%s failed (rc=%d)" % (cl, rc),
                  file=sys.stderr)
            continue
        meta = json.loads(out)
        h = best_height(meta)
        if client or h >= MIN_HEIGHT:
            return out, meta, cl
        print("  player_client=%s offers only %dp -- next client"
              % (cl, h), file=sys.stderr)
        low.append("%s %dp" % (cl, h))
    if low:
        raise RuntimeError(
            "no player client offered >= %dp (%s). tv_simply/android_vr need "
            "a PO token (bgutil on 127.0.0.1:4416) for more; for a genuinely "
            "low-res source, pin one with --client {%s}"
            % (MIN_HEIGHT, ", ".join(low), ",".join(PLAYER_CLIENTS)))
    raise RuntimeError("yt-dlp metadata failed:\n%s" % (err or "")[-2000:])


def parse_ts(spec):
    """'MM:SS' / 'HH:MM:SS' / bare seconds -> a string yt-dlp understands."""
    spec = spec.strip()
    if not re.fullmatch(r"[\d:.]+", spec):
        raise SystemExit("bad timestamp %r" % spec)
    return spec


def fetch_youtube(vid, out_dir, proxy=None, timestamps=None, client=None):
    url = "https://www.youtube.com/watch?v=%s" % vid
    dst = os.path.join(out_dir, "source.mp4")
    srt = os.path.join(out_dir, "captions.srt")
    # "Captions resolved, none exist" is also complete: most recitations have
    # no Arabic auto-captions, and without the sentinel every re-fetch of such
    # a source re-walks the player-client ladder for captions that are not
    # there -- the exact request the bot check kills.
    none = os.path.join(out_dir, "captions.none")

    # Nothing left to fetch: this source is already complete, so it never
    # reaches the network at all. The metadata call is a player-API request
    # like any other and can fail the bot check -- failing it for a fetch
    # that would have downloaded nothing is a re-fetch that fails for free.
    if usable(dst) and (os.path.exists(srt) or os.path.exists(none)):
        print("  video   : %s  %s  (reused)"
              % (os.path.relpath(dst, ROOT), describe(dst)))
        print("  captions: %s  (reused)"
              % os.path.relpath(srt if os.path.exists(srt) else none, ROOT))
        return

    # metadata first: title/duration are worth having on screen before minutes
    # of download, and a bot check fires here, before any bytes move.
    out, meta, client = _resolve(url, proxy, client)
    print("  client  : %s (offers %dp)" % (client, best_height(meta)))
    print("  %s | %ss | %s" % (meta.get("title"), meta.get("duration"),
                              meta.get("uploader") or meta.get("channel")))
    fd, info = tempfile.mkstemp(suffix=".info.json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(out)
        _fetch_media(info, meta, dst, srt, none, proxy, timestamps, vid)
    finally:
        os.remove(info)


def _fetch_media(info, meta, dst, srt, none, proxy, timestamps, vid):
    """Media + captions from the metadata call's own JSON: same formats, same
    exit, no second player-API request."""
    out_dir = os.path.dirname(dst)
    if usable(dst):
        print("  video   : %s  %s  (reused)"
              % (os.path.relpath(dst, ROOT), describe(dst)))
    else:
        args = ["--load-info-json", info, "-f", FORMAT, "-S", SORT,
                "--merge-output-format", "mp4",
                "-o", os.path.join(out_dir, "source.%(ext)s")]
        if timestamps:
            a, _, b = timestamps.partition("-")
            args = ["--download-sections",
                    "*%s-%s" % (parse_ts(a), parse_ts(b))] + args
        rc, _, _ = _run_ytdlp(args, proxy)
        if rc != 0 or not usable(dst):
            raise RuntimeError(
                "download failed for %s (file %s)" % (
                    vid, "missing" if not os.path.exists(dst)
                    else "present but stub/unprobeable -- not accepted"))
        want = min(best_height(meta), 1080)
        if video_info(dst)[1] < want:
            got = describe(dst)
            os.remove(dst)
            raise RuntimeError("download landed at %s but %dp was offered "
                               "-- not accepted" % (got, want))
        if video_info(dst)[2] > MAX_FPS:
            cap_fps(dst, dst)
        else:
            ensure_aac(dst)
        print("  video   : %s  %s" % (os.path.relpath(dst, ROOT), describe(dst)))

    if os.path.exists(srt):
        print("  captions: %s  (reused)" % os.path.relpath(srt, ROOT))
        return
    if timestamps:
        _no_captions(none, "--timestamps section: YouTube's captions are "
                           "timed against the full video")
        return
    if "ar-orig" not in (meta.get("automatic_captions") or {}):
        _no_captions(none, "no ar-orig auto-captions on YouTube")
        return
    rc, _, _ = _run_ytdlp(
        ["--load-info-json", info, "--skip-download", "--write-auto-subs",
         "--sub-lang", "ar-orig", "--convert-subs", "srt",
         "-o", os.path.join(out_dir, "source.%(ext)s")], proxy)
    got = os.path.join(out_dir, "source.ar-orig.srt")
    if rc == 0 and os.path.exists(got):
        os.replace(got, srt)
        print("  captions: %s" % os.path.relpath(srt, ROOT))
    else:
        # A FAILED caption fetch records nothing -- the next run retries.
        print("  captions: fetch failed; the next run retries")


def _no_captions(none, why):
    """Record that there is no captions.srt to have, so the completeness gate
    fires and a re-fetch never touches the network."""
    with open(none, "w", encoding="utf-8") as fh:
        fh.write("%s (recorded by fetch.py; delete to re-check)\n" % why)
    print("  captions: none -- %s (recorded; transcribe.py covers it)" % why)


# --- local files -----------------------------------------------------------

def fetch_local(path, out_dir):
    """Symlink the file in place. A source ABOVE MAX_FPS is re-encoded down to
    it instead (cap_fps), and a non-AAC .mp4 gets an AAC copy (ensure_aac --
    the deadlock is an MP4-container one, so other containers are linked
    as-is). The user's own file is never touched."""
    src = os.path.abspath(path)
    if not os.path.isfile(src):
        raise SystemExit("not a file: %s" % src)
    fps = video_info(src)[2]
    ext = os.path.splitext(src)[1].lower()
    fix_audio = ext in (".mp4", ".m4v") and audio_codec(src) not in ("aac", "")
    if fps > MAX_FPS or fix_audio or ext == ".m4v":
        ext = ".mp4"
    dst = os.path.join(out_dir, "source" + ext)
    if os.path.basename(dst) not in generate.SOURCE_NAMES:
        raise SystemExit("%s: unsupported extension; use one of %s"
                         % (src, " ".join(n[6:] for n in generate.SOURCE_NAMES)))
    if os.path.islink(dst) or os.path.exists(dst):
        print("  video   : %s  %s  (already present, untouched)"
              % (os.path.relpath(dst, ROOT), describe(dst)))
        return
    if fps > MAX_FPS:
        cap_fps(src, dst)
    elif fix_audio:
        ensure_aac(src, dst)
    else:
        os.symlink(src, dst)
        print("  video   : %s -> %s  %s"
              % (os.path.relpath(dst, ROOT), src, describe(dst)))
        return
    print("  video   : %s  %s  [from %s]"
          % (os.path.relpath(dst, ROOT), describe(dst), src))


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
                    help="pin one player client instead of walking the ladder; "
                         "accepts whatever resolution it offers")
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
