"""
qc/fx -- the registry of individually switchable "bars" effects.

Every stage of the bars look is an Effect object with a name, an `enable`
resolution and (for the band-chain ones) an `apply()`. Two things follow:

  * ORDER IS DATA. `fx.order` in templates/bars.yaml lists the band chain in
    the order it runs; build_bars just walks that list. Reordering the look no
    longer means reordering statements.

  * SWITCHING IS ONE FLAG. Each effect resolves its own `enable` out of its own
    parameter block, and a clip overrides it with `fx: {name: false}` in
    clip.yaml. Because the effect owns all three of its hooks, disabling it
    removes its accumulator plate and its per-phrase branches too -- and
    because the band taps go through ffgraph's tap(), the `split=N` degree
    recomputes itself. Turning `scan` off drops the band split from 3 to 2 with
    no other edit.

An unknown effect name -- in `fx.order`, in the template's `fx:` map, or in a
clip's `fx:` override -- is a hard error naming the offender, never a silent
no-op.

    chain = qc.fx.fx_chain(style, clip)     # enabled band effects, in order
    for eff in qc.fx.plates(chain):         # accumulator plates
        eff.plate(g, ctx)
    ...
    band = qc.fx.enabled(style, clip, "scrim")
"""
from .base import Ctx, Effect, LUMA, truth          # noqa: F401
from . import effects

REGISTRY = dict((c.name, c) for c in effects.ALL)
BAND_FX = tuple(c.name for c in effects.ALL if c.band)


def _die(msg):
    raise SystemExit("bars fx: " + msg)


def _known(where):
    return "%s (known: %s)" % (where, ", ".join(sorted(REGISTRY)))


def switches(style, clip):
    """Resolve every effect's enable flag: the template's own block, then the
    clip's `fx:` overrides on top. Returns {name: bool}."""
    tpl = style.get("fx") or {}
    for k in tpl:
        if k != "order" and k not in REGISTRY:
            _die(_known("templates/bars.yaml has unknown effect 'fx.%s'" % k))
    out = {}
    for name, cls in REGISTRY.items():
        out[name] = truth((cls.config(style) or {}).get("enable"), True)

    ov = clip.get("fx") if isinstance(clip, dict) else None
    if ov is None:
        return out
    if not isinstance(ov, dict):
        _die("clip.yaml `fx:` must be a map of effect name -> true/false")
    for k, v in ov.items():
        if k not in REGISTRY:
            _die(_known("clip.yaml has unknown effect 'fx.%s'" % k))
        out[k] = truth(v, True)
    return out


def enabled(style, clip, name):
    """Is a single named effect on? (Used by the pre-composite stages, which
    are wired at their own sites rather than in the band chain.)"""
    if name not in REGISTRY:
        _die(_known("no such effect '%s'" % name))
    return switches(style, clip)[name]


def fx_chain(style, clip):
    """The ENABLED band effects, instantiated, in `fx.order` order."""
    order = (style.get("fx") or {}).get("order")
    if not order:
        _die("templates/bars.yaml is missing `fx.order` "
             "(the band chain order is data, not code order)")
    if not isinstance(order, (list, tuple)):
        _die("templates/bars.yaml `fx.order` must be a list")
    sw = switches(style, clip)
    seen, chain = [], []
    for name in order:
        cls = REGISTRY.get(name)
        if cls is None:
            _die(_known("fx.order names unknown effect '%s'" % name))
        if not cls.band:
            _die("fx.order names '%s', which is not a band-chain effect "
                 "(band effects: %s)" % (name, ", ".join(BAND_FX)))
        if name in seen:
            _die("fx.order lists '%s' twice" % name)
        seen.append(name)
        if sw[name]:
            chain.append(cls(style, clip))
    missing = [n for n in BAND_FX if n not in seen]
    if missing:
        _die("fx.order omits %s -- every band effect must be listed, even a "
             "disabled one" % ", ".join(missing))
    return chain


def plates(chain):
    """The enabled effects that declare an accumulator plate, in the FROZEN
    plate-emission order (`plate_rank`). Deliberately independent of
    `fx.order`: the plates are declared before the phrase loop, long before
    the band chain runs, and their relative order is part of the
    byte-for-byte filtergraph contract."""
    return sorted((e for e in chain if e.plate_rank is not None),
                  key=lambda e: e.plate_rank)


def by_layer(chain):
    """{caption layer name -> the enabled effect that branches off it}."""
    return dict((e.layer, e) for e in chain if e.layer)
