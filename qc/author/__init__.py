"""qc.author -- the front end of the pipeline: everything BEFORE rendering.

`qc.*` proper is the renderer's shared internals, frozen against the goldens in
tests/golden/. This subpackage is the other half: deciding what to clip in the
first place. It is the code replacing the prose procedure in
`.claude/skills/make-post/SKILL.md`, one stage at a time.

Present:
    fetch    `qc source add <url>` -- yt-dlp download + metadata + auto-captions
    locate   `qc locate <id>`      -- which surah/ayat does this video recite?

Nothing here imports from qc.fx / qc.ffgraph, and nothing here may change
render output.
"""
