#!/usr/bin/env python3
"""Export a finished clip to the permanent reels/ library.

Run:  tools/render-venv/bin/python scripts/export_reel.py <clip-name> [...] | all

Per clip:
  - stream-copy remux clips/<name>/output/final.mp4 -> reels/RECITER-SURAH-a0-a1.mp4
    (e.g. BADR-AL-TURKI-AHZAB-70-71.mp4; reciter part from tags.yaml
    `reciter_short` if set, else `reciter`)
  - embeds MP4 container metadata: title, artist (reciter), date, and a comment
    with the verse ref, source YouTube URL and source segment timestamps —
    so the reel stays self-describing after the clip folder is deleted
  - applies the tags.yaml keyword tags + a green Posted / red "Not Posted"
    Finder marker onto the reel file itself
  - drops a Finder alias to the reel inside the clip folder (symlink fallback)

Idempotent: re-run any time; overwrites the reel atomically and re-asserts
tags/alias. After posting (scripts/status.py post <name>) the clip folder can
be deleted by hand — the reel and its xattr marker are the permanent record.
"""
import os
import sys
import subprocess
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import render_text  # noqa: E402
import status  # noqa: E402  (shared SURAH/naming/xattr helpers)

sys.path.insert(0, ROOT)
from qc import env as _env  # noqa: E402

FFMPEG = _env.require("ffmpeg", "remux the exported reel")

ALIAS_SCRIPT = """
on run argv
  set srcFile to POSIX file (item 1 of argv) as alias
  set destFolder to POSIX file (item 2 of argv) as alias
  tell application "Finder"
    set name of (make alias file to srcFile at destFolder) to (item 3 of argv)
  end tell
end run
"""


def make_alias(clip_dir, reel_path, stem):
    """Finder alias named <stem> in the clip folder; never fails the export."""
    alias_path = os.path.join(clip_dir, stem)
    if os.path.lexists(alias_path):
        return
    try:
        subprocess.run(
            ["osascript", "-e", ALIAS_SCRIPT, reel_path, clip_dir, stem],
            check=True, capture_output=True, timeout=15,
        )
    except Exception as e:
        try:
            os.symlink(os.path.relpath(reel_path, clip_dir), alias_path)
            print(f"  warn: Finder alias failed ({e}); made symlink instead")
        except Exception as e2:
            print(f"  warn: no alias created ({e2})")


def is_preview(path):
    """`qc render --preview` writes output/preview.mp4 and tags the container
    `comment=qc-preview`. The tag is what makes the refusal below survive
    someone renaming the file, which is the only way a preview can reach here
    at all."""
    p = subprocess.run(
        [_env.require("ffprobe"), "-v", "error", "-show_entries",
         "format_tags=comment", "-of", "default=nw=1:nk=1", path],
        capture_output=True, text=True)
    return p.stdout.strip().startswith("qc-preview")


def export(name):
    clip_dir = os.path.join(status.CLIPS, name)
    tags_p = os.path.join(clip_dir, "tags.yaml")
    final = os.path.join(clip_dir, "output", "final.mp4")
    if not os.path.isfile(tags_p):
        print(f"skip {name}: no tags.yaml")
        return False
    if not os.path.isfile(final):
        print(f"skip {name}: no output/final.mp4")
        return False
    if is_preview(final):
        print(f"REFUSED {name}: output/final.mp4 is a PREVIEW render (half "
              f"canvas, no heat/snow/glow passes, crf 28). Re-render it "
              f"properly with `qc render {name}` before exporting.")
        return False
    meta = render_text.load_yaml(tags_p)
    clip_p = os.path.join(clip_dir, "clip.yaml")
    clip = render_text.load_yaml(clip_p) if os.path.isfile(clip_p) else {}

    n = int(meta["surah_number"])
    a0, a1 = [int(x) for x in meta["ayah_range"]]
    reciter = str(meta.get("reciter") or "").strip()
    url = meta.get("source_url") or clip.get("source_url") or ""
    seg = clip.get("segment") or {}
    title = meta.get("title") or f"{status.SURAH[n]} {a0}-{a1} — {reciter}"
    comment = (
        f"Quran {n}:{a0}-{a1} | {reciter} | source: {url}"
        f" | segment {seg.get('start', '?')}-{seg.get('end', '?')}"
    )

    rname = status.reel_name(meta)
    os.makedirs(status.REELS, exist_ok=True)
    dst = os.path.join(status.REELS, rname)
    tmp = os.path.join(status.REELS, ".tmp-" + rname)
    subprocess.run(
        [
            FFMPEG, "-y", "-v", "error", "-i", final,
            "-map_metadata", "-1", "-c", "copy", "-movflags", "+faststart",
            "-metadata", f"title={title}",
            "-metadata", f"artist={reciter}",
            "-metadata", f"date={time.strftime('%Y-%m-%d')}",
            "-metadata", f"comment={comment}",
            tmp,
        ],
        check=True,
    )
    os.replace(tmp, dst)

    # Reels get one plain colour tag only — green = posted, red = not posted.
    status.set_marker(dst, meta.get("posted"))
    make_alias(clip_dir, dst, rname[:-4])
    print(f"  reel  {rname}{'  ✅ posted' if meta.get('posted') else ''}")
    return True


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(1)
    names = status.clip_dirs() if args == ["all"] else args
    done = sum(1 for name in names if export(name))
    print(f"\nDone: {done}/{len(names)} exported -> {status.REELS}")


if __name__ == "__main__":
    main()
