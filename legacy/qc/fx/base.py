"""
qc/fx/base.py -- the Effect protocol shared by every switchable bars stage.

An Effect owns THREE things and nothing else:

  * its own parameters (read out of the style template under its own key),
  * the filter strings and derived scalars it needs -- moved here VERBATIM
    from build_bars.py; the sigma/knee/tint algebra is not re-derived,
  * where it hooks into the graph.

There are three hooks, because the bars graph touches an effect in up to three
places:

  plate(g, ctx)              a black accumulator plate declared once, before
                             the phrase loop (barglow's white pill silhouette
                             plate, textglow's glyph plate).
  per_phrase(g, ctx, i, lbl) a per-phrase branch off one caption layer, taken
                             inside the phrase loop; returns the label the
                             compositor should overlay instead of `lbl`.
  apply(g, band, ctx)        the band-chain stage itself: consumes the running
                             band label, returns the new one.

Only `apply` is ordered by `fx.order`. `plate` emission order is FROZEN
separately (see qc.fx.plates) because it predates the ordering and is part of
the byte-for-byte filtergraph contract.
"""

# Rec.601 luma, written as a 3x3 channel mixer so it can sit inside an RGB
# chain without a format change. Shared by glow, textglow and scan.
LUMA = ("colorchannelmixer=rr=0.299:rg=0.587:rb=0.114:"
        "gr=0.299:gg=0.587:gb=0.114:br=0.299:bg=0.587:bb=0.114")


def truth(v, default=True):
    """Accept the several spellings of `enable` this repo's YAML has used:
    a bool, the legacy 1/0 int, or a string."""
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    return str(v).strip().lower() in ("1", "true", "yes", "on")


class Ctx(object):
    """Plain attribute bag: the canvas/band geometry and the handful of
    clip-derived values every effect draws on (see build_bars.build)."""

    def __init__(self, **kw):
        self.__dict__.update(kw)


class Effect(object):
    name = None
    band = True          # participates in the fx.order band chain
    plate_rank = None    # not None => emits an accumulator plate, at this rank
    layer = None         # caption layer it branches off ("bar" / "text")

    def __init__(self, style, clip):
        self.style, self.clip = style, clip
        self.cfg = self.config(style)

    @classmethod
    def config(cls, style):
        """The effect's parameter block, i.e. where its `enable` lives."""
        return (style.get("fx") or {}).get(cls.name) or {}

    # -- hooks (all optional) --------------------------------------------
    def plate(self, g, ctx):
        pass

    def per_phrase(self, g, ctx, i, lbl):
        return lbl

    def apply(self, g, band, ctx):
        return band
