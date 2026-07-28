"""qc.author.fetch -- source acquisition. `qc source add <url>`.

`sources/` is the durable, flat, shared download cache: one `<video_id>.mp4`
per YouTube video, reused by every clip cut from it (SKILL.md:83-91). This
module is the only thing that writes into it, and it never overwrites: an
existing .mp4 is kept as-is, because re-downloading can silently hand you a
different re-encode than the one a shipped clip's crop numbers were solved
against.

Three artifacts per source:
    sources/<id>.mp4            video, <=1080p
    sources/<id>.ar-orig.vtt    YouTube's Arabic auto-captions, if any
    sources/meta/<id>.yaml      title/duration/fps/dimensions/uploader

The meta yaml is the one that is COMMITTED (see .gitignore) -- it is cheap to
store, expensive to re-fetch, and `qc locate` needs the duration and title.
"""
import json
import os
import re
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SOURCES = os.path.join(ROOT, "sources")
META = os.path.join(SOURCES, "meta")

# The 403 / DRM-only fallback learned on al-infitar-6-9 (SKILL.md:78): when the
# default player clients refuse, `web_embedded` was the only one still exposing
# format 137 (1080p). Cheap to retry with, so it is a plain second attempt
# rather than something the caller has to know about.
EMBED_ARGS = ["--extractor-args", "youtube:player_client=web_embedded"]

# Prefer a real <=1080p video+audio pair; fall back to any progressive <=1080p.
FORMAT = "bv*[height<=1080]+ba/b[height<=1080]/bv*+ba/b"


def yt_dlp():
    exe = shutil.which("yt-dlp")
    if not exe:
        raise SystemExit(
            "yt-dlp not found on PATH. `brew install yt-dlp` (do NOT pip-install "
            "it into /opt/homebrew/bin/python3)."
        )
    return exe


def video_id(url):
    """Accept a full YouTube URL, a youtu.be link, or a bare 11-char id."""
    url = (url or "").strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", url):
        return url
    m = re.search(r"(?:v=|/shorts/|youtu\.be/|/embed/|/live/)([A-Za-z0-9_-]{11})", url)
    if m:
        return m.group(1)
    raise SystemExit("cannot find a YouTube video id in %r" % url)


def _run(args, capture=False):
    if capture:
        p = subprocess.run(args, capture_output=True, text=True)
        return p.returncode, p.stdout, p.stderr
    p = subprocess.run(args)
    return p.returncode, "", ""


def _with_fallback(base_args, capture=False):
    """Run yt-dlp; on failure retry once with the web_embedded client."""
    rc, out, err = _run(base_args, capture)
    if rc == 0:
        return rc, out, err
    sys.stderr.write("  yt-dlp failed (rc=%d); retrying with player_client=web_embedded\n" % rc)
    return _run(base_args[:1] + EMBED_ARGS + base_args[1:], capture)


# --- yaml out --------------------------------------------------------------
# Written by hand rather than via PyYAML so the file stays diff-friendly and
# key-ordered, and so this module has no import-time dependency on the venv.

def _yaml_scalar(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if v is None:
        return "null"
    if isinstance(v, (int, float)):
        return repr(v)
    s = str(v)
    return '"%s"' % s.replace("\\", "\\\\").replace('"', '\\"')


def write_meta(vid, meta):
    os.makedirs(META, exist_ok=True)
    path = os.path.join(META, "%s.yaml" % vid)
    order = ["id", "url", "title", "uploader", "upload_date", "duration",
             "width", "height", "fps", "has_ar_captions", "caption_langs"]
    with open(path, "w", encoding="utf-8") as f:
        f.write("# written by `qc source add` -- do not hand-edit the fetched\n"
                "# fields; anything below is what yt-dlp reported for this video.\n")
        for k in order:
            if k in meta:
                v = meta[k]
                if isinstance(v, list):
                    f.write("%s: [%s]\n" % (k, ", ".join(_yaml_scalar(x) for x in v)))
                else:
                    f.write("%s: %s\n" % (k, _yaml_scalar(v)))
    return path


def read_meta(vid):
    """Minimal reader for what write_meta emits (flat scalars + flow lists)."""
    path = os.path.join(META, "%s.yaml" % vid)
    if not os.path.exists(path):
        return {}
    out = {}
    for line in open(path, encoding="utf-8"):
        line = line.rstrip("\n")
        if not line or line.lstrip().startswith("#") or ":" not in line:
            continue
        k, _, v = line.partition(":")
        v = v.strip()
        if v.startswith("[") and v.endswith("]"):
            v = [x.strip().strip('"') for x in v[1:-1].split(",") if x.strip()]
        elif v.startswith('"') and v.endswith('"'):
            v = v[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        elif v in ("true", "false"):
            v = v == "true"
        elif v == "null":
            v = None
        else:
            try:
                v = int(v)
            except ValueError:
                try:
                    v = float(v)
                except ValueError:
                    pass
        out[k.strip()] = v
    return out


# --- the stages ------------------------------------------------------------

def probe(url):
    """yt-dlp -J -> the metadata dict we keep."""
    rc, out, err = _with_fallback(
        [yt_dlp(), "-J", "--no-playlist", url], capture=True)
    if rc != 0:
        raise SystemExit("yt-dlp metadata failed:\n%s" % (err or out)[-2000:])
    j = json.loads(out)
    subs = list((j.get("automatic_captions") or {}).keys())
    ar = [s for s in subs if s.startswith("ar")]
    return {
        "id": j.get("id"),
        "url": j.get("webpage_url") or url,
        "title": j.get("title"),
        "uploader": j.get("uploader") or j.get("channel"),
        "upload_date": j.get("upload_date"),
        "duration": j.get("duration"),
        "width": j.get("width"),
        "height": j.get("height"),
        "fps": j.get("fps"),
        "has_ar_captions": bool(ar),
        "caption_langs": sorted(ar)[:8],
    }


def download(vid, url):
    """Fetch <=1080p to sources/<id>.mp4. Reuses an existing file untouched."""
    dst = os.path.join(SOURCES, "%s.mp4" % vid)
    if os.path.exists(dst):
        return dst, False
    os.makedirs(SOURCES, exist_ok=True)
    rc, _, _ = _with_fallback([
        yt_dlp(), "--no-playlist", "-f", FORMAT,
        "--merge-output-format", "mp4",
        "-o", os.path.join(SOURCES, "%(id)s.%(ext)s"), url])
    if rc != 0 or not os.path.exists(dst):
        raise SystemExit("download failed for %s" % vid)
    return dst, True


def captions(vid, url, lang="ar-orig"):
    """Fetch YouTube's Arabic auto-captions. Returns the path, or None.

    Absence is NORMAL and not an error -- plenty of recitation uploads have no
    auto-captions at all (and `ar-orig` in particular only exists when YouTube
    detected the audio as Arabic). The caller reports it; a later phase falls
    back to local Whisper (SKILL.md:65-75).
    """
    dst = os.path.join(SOURCES, "%s.%s.vtt" % (vid, lang))
    if os.path.exists(dst):
        return dst, False
    _with_fallback([
        yt_dlp(), "--no-playlist", "--skip-download",
        "--write-auto-subs", "--sub-lang", lang, "--sub-format", "vtt",
        "-o", os.path.join(SOURCES, "%(id)s.%(ext)s"), url])
    return (dst, True) if os.path.exists(dst) else (None, False)


def add(url, skip_video=False):
    vid = video_id(url)
    canonical = "https://www.youtube.com/watch?v=%s" % vid
    print("source %s" % vid)

    meta = probe(canonical)
    meta["id"] = vid

    if skip_video:
        mp4, fresh = os.path.join(SOURCES, "%s.mp4" % vid), False
        print("  video   : skipped (--no-video)")
    else:
        mp4, fresh = download(vid, canonical)
        print("  video   : %s%s" % (os.path.relpath(mp4, ROOT),
                                    "" if fresh else "  (reused)"))

    vtt, cfresh = captions(vid, canonical)
    if vtt:
        print("  captions: %s%s" % (os.path.relpath(vtt, ROOT),
                                    "" if cfresh else "  (reused)"))
    else:
        langs = meta.get("caption_langs") or []
        print("  captions: NONE for ar-orig%s" % (
            " (available Arabic: %s)" % ", ".join(langs) if langs else
            " -- this video has no Arabic auto-captions; `qc locate` will not "
            "work on it until a later phase adds Whisper fallback"))
    meta["has_ar_captions"] = bool(vtt)

    path = write_meta(vid, meta)
    print("  meta    : %s" % os.path.relpath(path, ROOT))
    print("  %s | %ss | %sx%s @ %sfps | %s" % (
        meta.get("title"), meta.get("duration"), meta.get("width"),
        meta.get("height"), meta.get("fps"), meta.get("uploader")))
    return vid
