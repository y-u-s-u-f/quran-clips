"""
qc.scale -- render options, and the one place a template's pixel values are
resized for a canvas other than the one they were authored on.

templates/bars.yaml is written for a 1080x1920 canvas: every figure in it is
either a FRACTION of the canvas (which needs no help) or an ABSOLUTE PIXEL
COUNT measured on the reference reels and multiplied by 1.5. A half-size
preview has to move all of the latter and none of the former, and getting that
list wrong does not crash -- it silently renders a preview that does not look
like the final, which is worse than no preview at all.

So the pixel keys are enumerated ONCE, here, and `scale_style()` is the only
thing allowed to touch them. Two properties make it safe:

  IDENTITY AT 1.0. px(v, 1.0) returns the value object it was given -- same
  type, same float, not `v * 1.0`. `scale_style(style, 1.0)` returns the style
  dict unchanged, by reference. A final render therefore cannot be perturbed by
  the existence of the preview path, which the goldens prove.

  NO SILENT OMISSIONS. Every key in the template whose name ends in `_px` or
  `_pt` must appear in SCALED; a new one that does not is a hard error at
  scale time rather than a preview that quietly diverges.

What is deliberately NOT scaled: `fx.glow.scatter`, `fx.scan.scatter` and
`fx.snow.texel_scale` are already multiplied by `band_h / 405` where they are
used, `fx.heat.rms_frac_w` is a fraction of the width, and `perlin_centre` /
`perlin_sd` are 8-bit sample statistics, not lengths.
"""

# (path..., key) of every absolute-pixel value in templates/bars.yaml.
SCALED = (
    ("canvas", "width"), ("canvas", "height"),
    ("band", "width"), ("band", "height"), ("band", "y"),
    ("grade", "scrim", "left_to_px"), ("grade", "scrim", "edge_to_px"),
    ("text", "nominal_pt"), ("text", "min_pt"),
    ("text", "shadow", "blur_px"),
    ("bar", "height_px"), ("bar", "pad_x_px"),
    ("bar", "baseline_below_center_px"),
    ("transitions", "wipe_feather_px"),
    ("fx", "barglow", "sigma_near_px"), ("fx", "barglow", "sigma_far_px"),
    ("fx", "textglow", "sigma_px"),
)

# these must come out even: they end up as the encoder's frame size and as the
# crop/pad geometry of the band inside it.
EVEN = (("canvas", "width"), ("canvas", "height"),
        ("band", "width"), ("band", "height"), ("band", "y"))


def px(v, scale):
    """One absolute pixel value at `scale`. Identity at 1.0, by construction."""
    if scale == 1.0:
        return v
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return v
    if isinstance(v, int):
        return int(round(v * scale))
    return round(v * scale, 4)


def _audit(node, path, seen):
    for k, v in (node or {}).items():
        p = path + (k,)
        if isinstance(v, dict):
            _audit(v, p, seen)
        elif str(k).endswith(("_px", "_pt")) and p not in seen:
            raise SystemExit(
                "qc.scale: templates/bars.yaml has an absolute-pixel key "
                "`%s` that qc.scale.SCALED does not list -- a scaled preview "
                "would leave it at its 1080x1920 value. Add it (or rename it "
                "if it is not a pixel count)." % ".".join(str(x) for x in p))


def _copy(node):
    return {k: (_copy(v) if isinstance(v, dict) else v)
            for k, v in node.items()}


def scale_style(style, scale):
    """A copy of the style template resized to `scale`. At 1.0, the original
    object, untouched."""
    _audit(style, (), set(SCALED))
    if scale == 1.0:
        return style
    out = _copy(style)
    for path in SCALED:
        node = out
        for k in path[:-1]:
            node = node.get(k) if isinstance(node, dict) else None
            if node is None:
                break
        if not isinstance(node, dict) or path[-1] not in node:
            continue
        node[path[-1]] = px(node[path[-1]], scale)
    for path in EVEN:
        node = out
        for k in path[:-1]:
            node = node[k]
        v = node[path[-1]]
        if isinstance(v, int) and v % 2:
            node[path[-1]] = v - 1
    return out


# ---------------------------------------------------------------------------
# render options
#
# The default instance is exactly the historical behaviour: full canvas, every
# effect the template asks for, two-pass loudnorm, output/final.mp4. Nothing in
# the renderer branches on anything else unless a caller passes a different
# Opts, so the shipped render path is untouched.
# ---------------------------------------------------------------------------
class Opts(object):
    __slots__ = ("scale", "out_name", "disable", "crf", "preset",
                 "skip_loudnorm", "window", "overlay_sub", "still", "label",
                 "reuse")

    def __init__(self, scale=1.0, out_name="final.mp4", disable=(), crf=None,
                 preset=None, skip_loudnorm=False, window=None,
                 overlay_sub="overlays", still=False, label=None, reuse=None):
        self.scale = float(scale)
        self.out_name = out_name
        self.disable = tuple(disable)
        self.crf = crf
        self.preset = preset
        self.skip_loudnorm = bool(skip_loudnorm)
        # (t0, dur) in clip-relative seconds: render only this window, with
        # every caption time shifted so the schedule still lines up.
        self.window = window
        self.overlay_sub = overlay_sub
        self.still = bool(still)
        self.label = label
        # an already-built render_bars report, so a series of stills off one
        # clip does not redraw the captions (or re-sample the bar colour, which
        # costs an ffmpeg pass over the whole segment) once per frame
        self.reuse = reuse

    @property
    def is_default(self):
        return (self.scale == 1.0 and not self.disable and self.window is None
                and not self.still and self.crf is None and self.preset is None
                and not self.skip_loudnorm and self.out_name == "final.mp4")


DEFAULT = Opts()

# What a preview turns off. `grade` and `scrim` STAY: they are cheap, and the
# pill colour is derived from the graded band, so a preview without them would
# not even have the right hue. `glow` stays too -- it is one blur off the band
# and it carries the scene bloom the composition is judged on.
PREVIEW_OFF = ("heat", "snow", "barglow", "textglow", "scan")


def preview_opts(scale=0.5):
    return Opts(scale=scale, out_name="preview.mp4", disable=PREVIEW_OFF,
                crf=28, preset="ultrafast", skip_loudnorm=True,
                overlay_sub="overlays-preview", label="preview")


def with_disabled(clip, names):
    """A copy of the clip whose `fx:` map switches `names` off. Used instead of
    editing the template so that the effect registry still validates the name
    and still reports the switch in the render log."""
    if not names:
        return clip
    out = dict(clip)
    fx = dict(out.get("fx") or {})
    for n in names:
        fx[n] = False
    out["fx"] = fx
    return out
