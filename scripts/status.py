#!/usr/bin/env python3
"""Clip + reel post-tracker and tagger.

Two kinds of artifacts:
  clips/<name>/   WIP authoring folders. Source of truth while they exist =
                  their `tags.yaml` (`tags` list + `posted` bool). Mirrored to
                  the folder's macOS Finder tags.
  reels/<NAME>.mp4  Permanent exported reels (see scripts/export_reel.py).
                  A reel carries exactly ONE Finder tag and nothing else:
                  plain Green (posted) or plain Red (not posted). Keyword tags
                  live in the clip's tags.yaml, never on the reel.
                  While the owning clip folder exists its tags.yaml is truth and
                  the reel's marker is mirrored from it; once the clip folder is
                  deleted, the reel's own green/red marker IS the source of
                  truth. `sync` never touches markers on ownerless reels.

Usage (run with Homebrew python so it matches the rest of the pipeline):
    /opt/homebrew/bin/python3 scripts/status.py list
    /opt/homebrew/bin/python3 scripts/status.py post   <clip-or-reel> [...]
    /opt/homebrew/bin/python3 scripts/status.py unpost <clip-or-reel> [...]
    /opt/homebrew/bin/python3 scripts/status.py sync            # re-apply Finder tags

`post`/`unpost` also accept `all` (all clips + any ownerless reels). Editing
`posted:` in a tags.yaml by hand and then running `sync` (or `list`) achieves
the same thing for clips that still exist.

This module doubles as the shared library for reel naming + xattr helpers
(imported by export_reel.py).
"""
import os
import sys
import re
import json
import time
import plistlib
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLIPS = os.path.join(ROOT, "clips")
REELS = os.path.join(ROOT, "reels")
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import render_text  # reuse the repo's own indentation YAML reader  # noqa: E402

FFPROBE = "/opt/homebrew/bin/ffprobe"
TAG_ATTR = "com.apple.metadata:_kMDItemUserTags"
# Finder tag color indices: 0 none,1 grey,2 green,3 purple,4 blue,5 yellow,6 red,7 orange
# Reels carry exactly ONE tag: plain green or plain red. No keyword tags, no
# custom labels — the colour alone means posted / not posted.
POSTED_TAG = "Green\n2"
NOT_POSTED_TAG = "Red\n6"
# Legacy marker labels still found on older files; treated as equivalent.
LEGACY_POSTED = ("Posted\n2", "Posted")
LEGACY_NOT_POSTED = ("Not Posted\n6", "Not Posted")
MARKERS = (POSTED_TAG, NOT_POSTED_TAG, "Green", "Red") + LEGACY_POSTED + LEGACY_NOT_POSTED

# Surah number -> transliterated slug (mushaf order 1..114).
SURAH = {
    1: "Al-Fatihah", 2: "Al-Baqarah", 3: "Aal-Imran", 4: "An-Nisa",
    5: "Al-Maidah", 6: "Al-Anam", 7: "Al-Araf", 8: "Al-Anfal", 9: "At-Tawbah",
    10: "Yunus", 11: "Hud", 12: "Yusuf", 13: "Ar-Rad", 14: "Ibrahim",
    15: "Al-Hijr", 16: "An-Nahl", 17: "Al-Isra", 18: "Al-Kahf", 19: "Maryam",
    20: "Ta-Ha", 21: "Al-Anbiya", 22: "Al-Hajj", 23: "Al-Muminun", 24: "An-Nur",
    25: "Al-Furqan", 26: "Ash-Shuara", 27: "An-Naml", 28: "Al-Qasas",
    29: "Al-Ankabut", 30: "Ar-Rum", 31: "Luqman", 32: "As-Sajdah", 33: "Al-Ahzab",
    34: "Saba", 35: "Fatir", 36: "Ya-Sin", 37: "As-Saffat", 38: "Sad",
    39: "Az-Zumar", 40: "Ghafir", 41: "Fussilat", 42: "Ash-Shura", 43: "Az-Zukhruf",
    44: "Ad-Dukhan", 45: "Al-Jathiyah", 46: "Al-Ahqaf", 47: "Muhammad",
    48: "Al-Fath", 49: "Al-Hujurat", 50: "Qaf", 51: "Adh-Dhariyat", 52: "At-Tur",
    53: "An-Najm", 54: "Al-Qamar", 55: "Ar-Rahman", 56: "Al-Waqiah", 57: "Al-Hadid",
    58: "Al-Mujadila", 59: "Al-Hashr", 60: "Al-Mumtahanah", 61: "As-Saff",
    62: "Al-Jumuah", 63: "Al-Munafiqun", 64: "At-Taghabun", 65: "At-Talaq",
    66: "At-Tahrim", 67: "Al-Mulk", 68: "Al-Qalam", 69: "Al-Haqqah", 70: "Al-Maarij",
    71: "Nuh", 72: "Al-Jinn", 73: "Al-Muzzammil", 74: "Al-Muddaththir",
    75: "Al-Qiyamah", 76: "Al-Insan", 77: "Al-Mursalat", 78: "An-Naba",
    79: "An-Naziat", 80: "Abasa", 81: "At-Takwir", 82: "Al-Infitar",
    83: "Al-Mutaffifin", 84: "Al-Inshiqaq", 85: "Al-Buruj", 86: "At-Tariq",
    87: "Al-Ala", 88: "Al-Ghashiyah", 89: "Al-Fajr", 90: "Al-Balad", 91: "Ash-Shams",
    92: "Al-Layl", 93: "Ad-Duha", 94: "Ash-Sharh", 95: "At-Tin", 96: "Al-Alaq",
    97: "Al-Qadr", 98: "Al-Bayyinah", 99: "Az-Zalzalah", 100: "Al-Adiyat",
    101: "Al-Qariah", 102: "At-Takathur", 103: "Al-Asr", 104: "Al-Humazah",
    105: "Al-Fil", 106: "Quraysh", 107: "Al-Maun", 108: "Al-Kawthar",
    109: "Al-Kafirun", 110: "An-Nasr", 111: "Al-Masad", 112: "Al-Ikhlas",
    113: "Al-Falaq", 114: "An-Nas",
}

# Definite-article prefixes, longest first so Adh-/Ash- win over Ad-/As-.
# Aal-Imran ("family of Imran") deliberately does NOT match any of these.
ARTICLES = ("Adh-", "Ash-", "Al-", "An-", "Ar-", "As-", "At-", "Az-", "Ad-")


def slug(s):
    s = re.sub(r"[^\w\s-]", "", str(s)).strip()
    return re.sub(r"[\s_]+", "-", s)


def strip_article(name):
    for a in ARTICLES:
        if name.startswith(a):
            return name[len(a):]
    return name


SHORT = {n: strip_article(name).upper() for n, name in SURAH.items()}
NUM_BY_SHORT = {v: k for k, v in SHORT.items()}


def reel_name(meta):
    """Reel filename from tags.yaml meta: RECITER-SURAH-a0-a1.mp4."""
    a0, a1 = [int(x) for x in meta["ayah_range"]]
    rec = meta.get("reciter_short") or meta.get("reciter") or "unknown"
    return f"{slug(rec).upper()}-{SHORT[int(meta['surah_number'])]}-{a0}-{a1}.mp4"


def parse_reel(fname):
    """RECITER-SURAH-a0-a1[.mp4] -> dict. The surah is always the tail of the
    hyphenated body (longest suffix match), which disambiguates hyphenated
    reciters AND hyphenated surahs (TA-HA, AAL-IMRAN)."""
    stem = fname[:-4] if fname.lower().endswith(".mp4") else fname
    m = re.match(r"^(.+)-(\d+)-(\d+)$", stem)
    if not m:
        return None
    body, a0, a1 = m.group(1), int(m.group(2)), int(m.group(3))
    best = None
    for short, num in NUM_BY_SHORT.items():
        if (body == short or body.endswith("-" + short)) and (
            best is None or len(short) > len(best[0])
        ):
            best = (short, num)
    if not best:
        return None
    short, num = best
    return {
        "reciter": body[: len(body) - len(short)].rstrip("-"),
        "surah_number": num,
        "a0": a0,
        "a1": a1,
    }


def reel_meta(path):
    """Embedded MP4 container tags (title/artist/comment/date) via ffprobe."""
    r = subprocess.run(
        [FFPROBE, "-v", "quiet", "-print_format", "json", "-show_format", path],
        capture_output=True, text=True,
    )
    try:
        return (json.loads(r.stdout).get("format") or {}).get("tags") or {}
    except Exception:
        return {}


def clip_dirs():
    if not os.path.isdir(CLIPS):
        return []
    out = []
    for name in sorted(os.listdir(CLIPS)):
        d = os.path.join(CLIPS, name)
        if os.path.isdir(d) and os.path.isfile(os.path.join(d, "tags.yaml")):
            out.append(name)
    return out


def reel_files():
    if not os.path.isdir(REELS):
        return []
    return sorted(
        f for f in os.listdir(REELS)
        if f.lower().endswith(".mp4") and not f.startswith(".")
    )


def load(name):
    return render_text.load_yaml(os.path.join(CLIPS, name, "tags.yaml"))


def set_finder_tags(path, names):
    """Overwrite the file/folder's Finder user-tags with `names`.

    macOS Python lacks os.setxattr, so shell out to the `xattr` CLI with a
    hex-encoded binary plist (the format Finder stores tags in)."""
    data = plistlib.dumps(names, fmt=plistlib.FMT_BINARY)
    subprocess.run(["xattr", "-wx", TAG_ATTR, data.hex(), path], check=True)


def get_finder_tags(path):
    """Read Finder user-tags back; [] if the xattr is missing/unreadable."""
    r = subprocess.run(
        ["xattr", "-px", TAG_ATTR, path], capture_output=True, text=True
    )
    if r.returncode != 0:
        return []
    try:
        return list(plistlib.loads(bytes.fromhex("".join(r.stdout.split()))))
    except Exception:
        return []


def set_marker(path, posted):
    """Set the file's Finder tags to just the green/red marker."""
    set_finder_tags(path, [POSTED_TAG if posted else NOT_POSTED_TAG])


def is_posted(path):
    """True if the file carries a green marker (incl. legacy 'Posted' label)."""
    tags = get_finder_tags(path)
    return any(t in (POSTED_TAG, "Green") or t in LEGACY_POSTED for t in tags)


def apply_finder(name, meta):
    path = os.path.join(CLIPS, name)
    keywords = list(meta.get("tags") or [])
    marker = POSTED_TAG if meta.get("posted") else NOT_POSTED_TAG
    set_finder_tags(path, keywords + [marker])


def clip_reel_path(name, meta=None):
    """Path of the clip's exported reel, or None if not exported yet."""
    try:
        p = os.path.join(REELS, reel_name(meta or load(name)))
    except Exception:
        return None
    return p if os.path.isfile(p) else None


def resolve(arg):
    """Accept a clip name or a reel filename (±.mp4, case-insensitive).
    Return (clip_name_or_None, reel_path_or_None)."""
    if os.path.isfile(os.path.join(CLIPS, arg, "tags.yaml")):
        return arg, clip_reel_path(arg)
    want = arg.lower()
    if not want.endswith(".mp4"):
        want += ".mp4"
    for f in reel_files():
        if f.lower() == want:
            rp = os.path.join(REELS, f)
            for c in clip_dirs():
                try:
                    if reel_name(load(c)) == f:
                        return c, rp
                except Exception:
                    pass
            return None, rp
    return None, None


def set_posted(name, value):
    """Flip `posted:` (and stamp `posted_at`) in tags.yaml, preserving comments."""
    p = os.path.join(CLIPS, name, "tags.yaml")
    lines = open(p, encoding="utf-8").read().splitlines()
    stamp = time.strftime("%Y-%m-%d") if value else ""
    saw_posted = saw_at = False
    for i, ln in enumerate(lines):
        if re.match(r"^\s*posted\s*:", ln):
            lines[i] = f"posted: {'true' if value else 'false'}"
            saw_posted = True
        elif re.match(r"^\s*posted_at\s*:", ln):
            lines[i] = f'posted_at: "{stamp}"'
            saw_at = True
    if not saw_posted:
        lines.append(f"posted: {'true' if value else 'false'}")
    if not saw_at:
        lines.append(f'posted_at: "{stamp}"')
    open(p, "w", encoding="utf-8").write("\n".join(lines) + "\n")


def cmd_list():
    names = clip_dirs()
    if names:
        w = max(len(n) for n in names)
        print(f"{'CLIP':<{w}}  POSTED  {'VERSE':<10}  TAGS")
        print("-" * (w + 40))
        n_posted = 0
        for name in names:
            m = load(name)
            rng = m.get("ayah_range") or []
            verse = f"{m.get('surah_number','?')}:{rng[0] if rng else '?'}"
            mark = "✅ yes" if m.get("posted") else "⬜ no "
            if m.get("posted"):
                n_posted += 1
            tags = " ".join("#" + t for t in (m.get("tags") or [])[:4])
            print(f"{name:<{w}}  {mark}   {verse:<10}  {tags}")
        print("-" * (w + 40))
        print(f"{n_posted}/{len(names)} clips posted")
    else:
        print("no clips with tags.yaml found")

    reels = reel_files()
    if not reels:
        return
    owner = {}
    for c in names:
        try:
            owner[reel_name(load(c))] = c
        except Exception:
            pass
    w = max(len(f) for f in reels)
    print()
    print(f"{'REEL':<{w}}  POSTED  {'VERSE':<10}  CLIP")
    print("-" * (w + 40))
    n_posted = 0
    for f in reels:
        posted = is_posted(os.path.join(REELS, f))
        if posted:
            n_posted += 1
        info = parse_reel(f)
        verse = f"{info['surah_number']}:{info['a0']}" if info else "?"
        mark = "✅ yes" if posted else "⬜ no "
        print(f"{f:<{w}}  {mark}   {verse:<10}  {owner.get(f, '(no clip)')}")
    print("-" * (w + 40))
    print(f"{n_posted}/{len(reels)} reels posted")


def cmd_toggle(value, args):
    if not args or args == ["all"]:
        targets = clip_dirs()
        owned = set()
        for c in targets:
            try:
                owned.add(reel_name(load(c)))
            except Exception:
                pass
        targets = targets + [f for f in reel_files() if f not in owned]
    else:
        targets = args
    for arg in targets:
        clip, reel = resolve(arg)
        if clip:
            set_posted(clip, value)
            apply_finder(clip, load(clip))
            if reel:
                set_marker(reel, value)
            extra = " (+reel)" if reel else ""
            print(f"{'POSTED ✅' if value else 'unposted ⬜'}  {clip}{extra}")
        elif reel:
            set_marker(reel, value)
            print(f"{'POSTED ✅' if value else 'unposted ⬜'}  {os.path.basename(reel)} (reel only)")
        else:
            print(f"skip {arg}: no clip tags.yaml or reel found")


def cmd_sync():
    """Re-apply Finder tags from each tags.yaml onto the clip folder AND its
    exported reel (if any). Ownerless reels are the truth themselves — never
    touched here."""
    for name in clip_dirs():
        meta = load(name)
        apply_finder(name, meta)
        rp = clip_reel_path(name, meta)
        if rp:
            set_marker(rp, meta.get("posted"))  # reels: colour marker only
            print(f"synced Finder tags: {name}  (+ reel {os.path.basename(rp)})")
        else:
            print(f"synced Finder tags: {name}")


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"
    rest = sys.argv[2:]
    if cmd == "list":
        cmd_list()
    elif cmd == "post":
        cmd_toggle(True, rest)
    elif cmd == "unpost":
        cmd_toggle(False, rest)
    elif cmd == "sync":
        cmd_sync()
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
