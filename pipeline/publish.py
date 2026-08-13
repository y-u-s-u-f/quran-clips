"""pipeline/publish.py -- post a finished reel to Instagram and Facebook.

    python3 pipeline/publish.py reels/BANDAR-BALEELA-ANAM-102-103.mp4

One command, both platforms, one caption. The caption is BUILT, never typed:
the reel's mp4 tags say which ayat it is and who recited them, and the text
comes out of `quran.py` -- al-Tafsir al-Muyassar for the meaning, the Uthmani
mushaf for the ayat themselves, the surah name for the hashtag. Nothing here
paraphrases scripture, and the same reel always yields the same caption.

The mp4 is the record. `generate.py` writes `artist` (the reciter, in Arabic,
as the hashtag spells him) and `comment` ("Quran 56:83-87 ...") into every
render, so a reel is publishable from the file alone -- no config lookup, no
naming convention to honour, and reels made years apart post identically.
`--surah/--ayat/--reciter` override the tags for a file that has none.

Both uploads are RESUMABLE-protocol uploads of local bytes: no public URL is
needed anywhere, which is the whole reason this is a script and not a manual
upload. Instagram: create a REELS container (upload_type=resumable), POST the
file to rupload.facebook.com, poll the container until FINISHED, publish it.
Facebook: /video_reels start -> the same rupload POST -> finish. The two APIs
are the same shape with different nouns.

The two posts are independent and cannot be otherwise. The Instagram app's
"also share to Facebook" tick -- the one that links them so a single insights
page covers both -- has no Graph API equivalent: `share_to_feed` only chooses
between Instagram's own Feed and Reels tabs, and `/video_reels` crossposts
between Facebook Pages only. Uploading twice is also what keeps the Facebook
copy a real Reel rather than the still image the native cross-share sometimes
produces.

Credentials live in `.env` (FB_PAGE_ID, FB_PAGE_TOKEN, IG_BUSINESS_ACCOUNT_ID)
and never in the repo. Publishing is the one stage that both leaves the
machine AND changes something irreversible, so it prints the caption and asks
before it posts unless `--yes` is given. A reel that posted is tagged green in
Finder, so `reels/` shows what has already gone out; `--draft` leaves it
untagged.
"""
import argparse
import json
import os
import plistlib
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import quran  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

GRAPH = "https://graph.facebook.com/v21.0"
RUPLOAD = "https://rupload.facebook.com"

# Instagram truncates a caption past this; Facebook's limit is far higher.
# Only the ayat + tafsir can push past it (a 6-ayah span does), so the
# overflow rule drops the recited text and keeps the explanation.
IG_CAPTION_MAX = 2200

# The Finder tag a published reel carries, so `reels/` shows at a glance what
# has already gone out -- the same green label applied by hand until now.
# A tag is stored in the user-tags xattr as "<name>\n<colour index>", and the
# swatch Finder actually draws comes from the separate FinderInfo label, so
# both are written (measured: the xattr alone leaves the file colourless and
# unlisted by `mdls`). Finder's AppleScript `label index` runs the colours in
# the opposite order to the stored index -- green is 2 on disk, 6 to Finder.
PUBLISHED_TAG = "Green"
TAG_COLOR_INDEX = 2
FINDER_LABEL_INDEX = 6

# Where the cover frame is cut from, in milliseconds. Both platforms default
# to frame 0 on an API upload -- neither picks a frame the way the app's
# editor offers one -- and frame 0 of these reels is the fade-in, before the
# first caption has arrived. 1.55s is past every style's fade and lands on a
# lit frame with the first card up.
COVER_MS = 1550


# --- .env ------------------------------------------------------------------
# Each script in this pipeline carries its own reader on purpose: every one of
# them must run standalone, so none of them may import a shared util module.
def load_env():
    env = dict(os.environ)
    path = os.path.join(ROOT, ".env")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().strip('"').strip("'")
                env.setdefault(k, v)   # shell environment wins
    return env


def need(env, *keys):
    missing = [k for k in keys if not env.get(k)]
    if missing:
        raise SystemExit("missing in .env: %s" % ", ".join(missing))
    return [env[k] for k in keys]


# --- the reel --------------------------------------------------------------
def probe(path):
    """-> (tags, width, height, duration_ms). One ffprobe: the duration is
    asked for here rather than by clamp_cover, which needs it on the same
    file a moment later."""
    out = subprocess.run(
        [os.environ.get("QC_FFPROBE", "ffprobe"), "-v", "error",
         "-show_entries", "format=duration:format_tags:stream=width,height",
         "-select_streams", "v:0", "-of", "json", path],
        capture_output=True, text=True, check=True).stdout
    d = json.loads(out)
    fmt = d.get("format") or {}
    tags = {k.lower(): v for k, v in (fmt.get("tags") or {}).items()}
    stream = (d.get("streams") or [{}])[0]
    return (tags, stream.get("width"), stream.get("height"),
            float(fmt.get("duration") or 0.0) * 1000)


def reel_facts(path, surah=None, ayat=None, reciter=None):
    """-> (surah, ayah_start, ayah_end, reciter, duration_ms). Tags unless
    overridden."""
    tags, w, h, dur_ms = probe(path)
    if w and h and int(w) > int(h):
        print("  ! %sx%s is landscape. Reels are 9:16 on both platforms; "
              "this will be padded or cropped by them." % (w, h))
    if surah is None:
        m = re.search(r"Quran\s+(\d+):(\d+)-(\d+)", tags.get("comment", ""))
        if not m:
            raise SystemExit(
                "%s carries no verse span. Re-render it (generate.py tags "
                "every reel now) or pass --surah/--ayat."
                % os.path.basename(path))
        surah, a0, a1 = (int(g) for g in m.groups())
    else:
        a0, a1 = (int(x) for x in str(ayat).replace(":", "-").split("-")[-2:])
    if reciter is None:
        reciter = tags.get("artist", "").strip()
        if not reciter:
            raise SystemExit(
                "%s carries no reciter. Set `reciter:` in its config and "
                "re-render, or pass --reciter with his Arabic name."
                % os.path.basename(path))
    return int(surah), a0, a1, reciter, dur_ms


# --- the caption -----------------------------------------------------------
def caption(surah, a0, a1, reciter, with_ayat=True):
    """The post description: al-Muyassar over the span, then two hashtags.

    Consecutive ayat SHARE a tafsir paragraph in al-Muyassar (56:83-85 are
    explained by one), so they are grouped: every ayah of a group is quoted,
    then their single explanation, once. Printing per ayah instead repeats
    the same paragraph three times.
    """
    groups = []
    for v in quran.range(surah, a0, a1):
        t = quran.tafsir(surah, v["ayah"])
        if groups and groups[-1][0] == t:
            groups[-1][1].append(v)
        else:
            groups.append((t, [v]))

    # "al-Tafsir al-Muyassar: ", escaped rather than pasted -- no Arabic is
    # typed into a source file here (see CLAUDE.md invariant 1). Letter by
    # letter: alef lam teh feh seen yeh reh / alef lam meem yeh seen reh.
    lines = ["\u0627\u0644\u062a\u0641\u0633\u064a\u0631"      # al-Tafsir
             " \u0627\u0644\u0645\u064a\u0633\u0631: "]  # al-Muyassar
    for t, verses in groups:
        if with_ayat:
            # U+FD3E/U+FD3F ORNATE PARENTHESIS, the pair a mushaf sets an
            # ayah in. FD3F (the "right" one) is written FIRST on purpose:
            # FD3E is drawn "(" and neither is Bidi_Mirrored, so the glyph
            # is fixed while the RTL run puts the first character on the
            # right -- FD3E-first bows both ornaments away from the ayah.
            lines += ["\ufd3f %s \ufd3e (%d)" % (v["ar"], v["ayah"])
                      for v in verses]
        lines.append(t)
    tags = "#%s | #%s" % (reciter.strip().replace(" ", "_"),
                          quran.surah_name_ar(surah, plain=True).replace(" ", "_"))
    return "\n".join(lines) + "\n\n" + tags


def captions_for(surah, a0, a1, reciter, for_ig=True):
    """-> (facebook, instagram). Facebook always gets the full text; only
    Instagram's 2200-character cap can force the ayat out of it, and only
    when Instagram is actually a target."""
    full = caption(surah, a0, a1, reciter)
    ig = full
    if not for_ig:
        return full, full
    if len(ig) > IG_CAPTION_MAX:
        ig = caption(surah, a0, a1, reciter, with_ayat=False)
        print("  ! full caption is %d chars, over Instagram's %d: its copy "
              "drops the quoted ayat and keeps the tafsir."
              % (len(full), IG_CAPTION_MAX))
    if len(ig) > IG_CAPTION_MAX:
        raise SystemExit(
            "even the tafsir alone is %d chars, over Instagram's %d. Post "
            "this span by hand, or split the reel." % (len(ig), IG_CAPTION_MAX))
    return full, ig


# --- Graph API -------------------------------------------------------------
def api(path, token, params=None, method="POST"):
    params = dict(params or {})
    params["access_token"] = token
    data = urllib.parse.urlencode(params).encode()
    url = "%s/%s" % (GRAPH, path)
    if method == "GET":
        url, data = "%s?%s" % (url, data.decode()), None
    req = urllib.request.Request(url, data=data, method=method)
    try:
        return json.load(urllib.request.urlopen(req, timeout=120))
    except urllib.error.HTTPError as e:
        raise SystemExit("Graph %s %s failed:\n%s"
                         % (method, path, e.read().decode()[:1500]))


def rupload(url, token, path):
    """The one call that moves bytes. Offset 0 and one shot: these files are
    tens of megabytes, and a resume protocol worth writing needs a failure
    mode worth handling -- a failed upload here is just re-run."""
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        body = f.read()
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Authorization": "OAuth %s" % token,
        "offset": "0",
        "file_size": str(size),
        "Content-Type": "application/octet-stream",
    })
    try:
        return json.load(urllib.request.urlopen(req, timeout=900))
    except urllib.error.HTTPError as e:
        raise SystemExit("upload failed:\n%s" % e.read().decode()[:1500])


def clamp_cover(dur, ms):
    """A cover offset past the end of the reel is not an error on either
    platform -- Instagram just falls back to frame 0 and says nothing -- so
    it is caught here, where it can still be reported. `dur` is the
    milliseconds reel_facts already probed; a file that reported none is left
    alone rather than clamped against a zero."""
    if dur and ms >= dur:
        ms = int(dur / 2)
        print("  ! cover offset is past the end of a %.1fs reel; using "
              "%.2fs instead" % (dur / 1000, ms / 1000))
    return ms


def cover_frame(path, ms, out):
    """One JPEG at `ms` into the reel. Facebook has no thumb_offset -- its
    cover is uploaded as an image -- so the frame is cut here and both
    platforms end up on the SAME one."""
    subprocess.run(
        [os.environ.get("QC_FFMPEG", "ffmpeg"), "-y", "-v", "error",
         "-ss", "%.3f" % (ms / 1000.0), "-i", path, "-frames:v", "1",
         "-q:v", "2", out], check=True)
    return out


def multipart(fields, filename, blob, field="source"):
    """-> (content_type, body). The thumbnail endpoint takes a file, not a
    URL, and this is the only call in the pipeline that needs multipart --
    12 lines against a dependency."""
    b = b"----qcpublish%d" % os.getpid()
    out = []
    for k, v in fields.items():
        out += [b"--" + b,
                b'Content-Disposition: form-data; name="%s"' % k.encode(),
                b"", str(v).encode()]
    out += [b"--" + b,
            b'Content-Disposition: form-data; name="%s"; filename="%s"'
            % (field.encode(), filename.encode()),
            b"Content-Type: image/jpeg", b"", blob, b"--" + b + b"--", b""]
    return "multipart/form-data; boundary=%s" % b.decode(), b"\r\n".join(out)


def publish_instagram(env, path, text, publish=True, cover_ms=0):
    ig, token = need(env, "IG_BUSINESS_ACCOUNT_ID", "FB_PAGE_TOKEN")
    print("  instagram: creating container...")
    c = api("%s/media" % ig, token, {
        "media_type": "REELS", "upload_type": "resumable",
        "caption": text, "share_to_feed": "true",
        # Milliseconds into the reel for the cover frame. The API default is
        # 0 -- the very first frame, which on these reels is the fade-in
        # before the first caption. Instagram does not pick a smart frame for
        # an API upload the way the app's editor suggests one.
        "thumb_offset": int(cover_ms)})
    cid = c["id"]
    print("  instagram: uploading %.1f MB..." % (os.path.getsize(path) / 1e6))
    rupload(c.get("uri") or "%s/ig-api-upload/v21.0/%s" % (RUPLOAD, cid),
            token, path)
    # The container transcodes after the bytes land; media_publish on an
    # IN_PROGRESS container is an error, not a wait.
    for _ in range(60):
        st = api(cid, token, {"fields": "status_code,status"}, method="GET")
        if st.get("status_code") == "FINISHED":
            break
        if st.get("status_code") == "ERROR":
            raise SystemExit("instagram container failed: %s" % st.get("status"))
        time.sleep(5)
    else:
        raise SystemExit("instagram container still processing after 5 min "
                         "(container %s -- publish it by hand)" % cid)
    if not publish:
        print("  instagram: container %s ready, NOT published (--draft)" % cid)
        return None
    media = api("%s/media_publish" % ig, token, {"creation_id": cid})["id"]
    link = api(media, token, {"fields": "permalink"}, method="GET")
    print("  instagram: %s" % link.get("permalink", media))
    return link.get("permalink")


def set_facebook_cover(token, video_id, jpeg):
    """Facebook's reel cover is an uploaded image, not an offset. Not fatal:
    a reel whose cover stayed on frame 0 is still a published reel, so this
    reports and moves on rather than taking the post down with it."""
    with open(jpeg, "rb") as f:
        blob = f.read()
    ctype, body = multipart({"is_preferred": "true", "access_token": token},
                            os.path.basename(jpeg), blob)
    req = urllib.request.Request(
        "%s/%s/thumbnails" % (GRAPH, video_id), data=body, method="POST",
        headers={"Content-Type": ctype})
    try:
        urllib.request.urlopen(req, timeout=120)
        return True
    except urllib.error.HTTPError as e:
        print("  ! facebook cover not set: %s" % e.read().decode()[:300])
        return False


def publish_facebook(env, path, text, publish=True, cover=None):
    page, token = need(env, "FB_PAGE_ID", "FB_PAGE_TOKEN")
    print("  facebook: opening reel session...")
    s = api("%s/video_reels" % page, token, {"upload_phase": "start"})
    vid = s["video_id"]
    print("  facebook: uploading %.1f MB..." % (os.path.getsize(path) / 1e6))
    rupload(s.get("upload_url") or "%s/video-upload/v21.0/%s" % (RUPLOAD, vid),
            token, path)
    if cover:
        set_facebook_cover(token, vid, cover)
    api("%s/video_reels" % page, token, {
        "upload_phase": "finish", "video_id": vid,
        "video_state": "PUBLISHED" if publish else "DRAFT",
        "description": text})
    if not publish:
        print("  facebook: video %s saved as a DRAFT" % vid)
        return None
    link = api(vid, token, {"fields": "permalink_url"}, method="GET")
    url = link.get("permalink_url", "")
    print("  facebook: %s" % (("https://facebook.com" + url) if url else vid))
    return url


def find_reel(arg):
    """Accept whatever names the reel: a path, a bare filename, or the reel
    name alone. `publish` is typed from any directory and the file is usually
    dragged in or half-remembered, so the argument is resolved against
    reels/ rather than the shell's cwd. Ambiguity is never guessed at."""
    if os.path.exists(arg) and not os.path.isdir(arg):
        return arg
    name = os.path.basename(arg)
    reels = os.path.join(ROOT, "reels")
    for cand in (name, name + ".mp4"):
        if os.path.exists(os.path.join(reels, cand)):
            return os.path.join(reels, cand)
    stem = name.lower().removesuffix(".mp4")
    hits = [f for f in sorted(os.listdir(reels))
            if f.endswith(".mp4") and stem in f.lower()]
    if len(hits) == 1:
        return os.path.join(reels, hits[0])
    if hits:
        raise SystemExit("%r matches %d reels:\n  %s"
                         % (arg, len(hits), "\n  ".join(hits)))
    raise SystemExit("no reel matches %r (looked in %s)" % (arg, reels))


def mark_published(path):
    """Tag the reel green in Finder, the way a posted reel has always been
    marked by hand.

    Existing tags are preserved and the tag is not duplicated on a re-post.
    Cosmetic and macOS-only: a failure here never fails a publish that already
    succeeded, so it is reported rather than raised."""
    if sys.platform != "darwin":
        return
    key = "com.apple.metadata:_kMDItemUserTags"
    try:
        # -x: hex out. The payload is a binary plist and is not text.
        cur = subprocess.run(["xattr", "-p", "-x", key, path],
                             capture_output=True, text=True)
        tags = []
        if cur.returncode == 0:
            blob = bytes.fromhex("".join(cur.stdout.split()))
            tags = list(plistlib.loads(blob))
        if any(t.split("\n")[0] == PUBLISHED_TAG for t in tags):
            return
        tags.append("%s\n%d" % (PUBLISHED_TAG, TAG_COLOR_INDEX))
        subprocess.run(
            ["xattr", "-w", "-x", key,
             plistlib.dumps(tags, fmt=plistlib.FMT_BINARY).hex(), path],
            check=True, capture_output=True)
        subprocess.run(
            ["osascript", "-e",
             'tell application "Finder" to set label index of '
             '(POSIX file "%s" as alias) to %d' % (path, FINDER_LABEL_INDEX)],
            capture_output=True, timeout=15)
        print("  tagged %s in Finder" % PUBLISHED_TAG.lower())
    except Exception as e:                                  # noqa: BLE001
        print("  ! could not tag the file %s: %s" % (PUBLISHED_TAG.lower(), e))


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="publish a rendered reel to Instagram + Facebook")
    ap.add_argument("reel", help="reels/<name>.mp4, or just the reel's name")
    ap.add_argument("--ig-only", action="store_true")
    ap.add_argument("--fb-only", action="store_true")
    ap.add_argument("--draft", action="store_true",
                    help="upload but do not publish: an Instagram container "
                         "and a Facebook draft, both publishable by hand")
    ap.add_argument("--caption-only", action="store_true",
                    help="print the caption and stop -- no network")
    ap.add_argument("--yes", "-y", action="store_true",
                    help="skip the confirmation prompt")
    ap.add_argument("--cover-ms", type=int, default=COVER_MS,
                    help="milliseconds into the reel for the cover frame "
                         "(default %d; 0 = the first frame)" % COVER_MS)
    ap.add_argument("--surah", type=int, help="override the mp4's tags")
    ap.add_argument("--ayat", help="e.g. 83-87 (with --surah)")
    ap.add_argument("--reciter", help="Arabic name, as the hashtag spells him")
    a = ap.parse_args(argv)

    a.reel = find_reel(a.reel)
    surah, a0, a1, reciter, dur_ms = reel_facts(a.reel, a.surah, a.ayat,
                                                a.reciter)
    print("%s -- %s %d:%d-%d, %s" % (os.path.basename(a.reel),
                                     quran.surah_name(surah), surah, a0, a1,
                                     reciter))
    fb_text, ig_text = captions_for(surah, a0, a1, reciter,
                                    for_ig=not a.fb_only)
    print("-" * 60)
    print(ig_text)
    print("-" * 60)
    if a.caption_only:
        return 0

    targets = []
    if not a.fb_only:
        targets.append("Instagram")
    if not a.ig_only:
        targets.append("Facebook")
    what = "upload as DRAFT" if a.draft else "PUBLISH"
    if not a.yes:
        if input("%s to %s? [y/N] " % (what, " + ".join(targets))).strip().lower() \
                not in ("y", "yes"):
            return 1

    env = load_env()
    if a.cover_ms:
        a.cover_ms = clamp_cover(dur_ms, a.cover_ms)
    if not a.fb_only:
        publish_instagram(env, a.reel, ig_text, publish=not a.draft,
                          cover_ms=a.cover_ms)
    if not a.ig_only:
        cover = None
        if a.cover_ms:
            cover = cover_frame(a.reel, a.cover_ms,
                                os.path.join(tempfile.gettempdir(),
                                             "qc-cover-%d.jpg" % os.getpid()))
        publish_facebook(env, a.reel, fb_text, publish=not a.draft,
                         cover=cover)
        if cover:
            os.remove(cover)
    if not a.draft:
        mark_published(a.reel)
    return 0


if __name__ == "__main__":
    sys.exit(main())
