"""
qc/ffgraph.py -- a very small chain/label builder for ffmpeg -filter_complex.

This is deliberately NOT a DAG library: there is no topological sort, no dead
branch elimination, no optimisation. It records chains in the order they are
declared and joins them with ';' -- exactly what hand-written f-string
concatenation did -- so a graph ported onto it emits a byte-identical string.

What it does buy:

  * input() owns the ffmpeg -i argv, so input indices can never drift out of
    sync with the [N:v] labels used in the graph.
  * tap() defers a split's degree to the number of consumers that actually
    asked for a branch. Disabling an effect used to mean editing `split=3`
    and both surviving consumers by hand; now the consumer's tap() call is
    the only edit. A tap with a single consumer emits no split at all -- the
    consumer is rewired straight onto the source label.

Usage:

    g = ffgraph.Graph()
    v = g.input("in.mp4", ss="1.000", t="4.000")   # -> "0:v"; v.a is "0:a"
    g.chain(v, ["scale=100:100", "setsar=1"], "sc") # [0:v]scale...,setsar=1[sc]
    a = g.tap("sc")                                 # deferred split branch
    fc, argv = g.render()
"""


class Ref(str):
    """An input pad reference such as "0:v"."""

    @property
    def v(self):
        return Ref(str(self).split(":")[0] + ":v")

    @property
    def a(self):
        return Ref(str(self).split(":")[0] + ":a")


class _Chain(object):
    __slots__ = ("ins", "filters", "outs")

    def __init__(self, ins, filters, outs):
        self.ins, self.filters, self.outs = ins, filters, outs


class _Tap(object):
    """A split whose degree is not known until every consumer has asked."""
    __slots__ = ("src", "outs")

    def __init__(self, src):
        self.src, self.outs = src, []


def _labels(x):
    if x is None:
        return []
    if isinstance(x, str):
        return [x]
    return [str(i) for i in x]


def _joined(f):
    return f if isinstance(f, str) else ",".join(f)


class Graph(object):
    def __init__(self):
        self._nodes = []        # ordered _Chain / _Tap
        self._argv = []         # ffmpeg input argv, in -i order
        self._taps = {}         # source label -> _Tap
        self._ninputs = 0

    # -- inputs -----------------------------------------------------------
    def input(self, path, **kw):
        """Record an ffmpeg input. Keyword args become the pre -i options, in
        the order given (ss=, t=, framerate=, loop=, stream_loop=...).
        Returns the video pad ref, e.g. "0:v"; use .a for the audio pad."""
        idx = self._ninputs
        self._ninputs += 1
        for k, val in kw.items():
            self._argv += ["-" + k, str(val)]
        self._argv += ["-i", path]
        return Ref("%d:v" % idx)

    # -- chains -----------------------------------------------------------
    def chain(self, ins, filters, outs):
        """Append "[in]...filters...[out]...". `ins`/`outs` may be a single
        label or a sequence; `filters` a string or a sequence joined by ','.
        Returns the out label, or a tuple of them when there is more than one.
        `ins=None` declares a source chain (color=, perlin=, ...)."""
        outs = _labels(outs)
        self._nodes.append(_Chain(_labels(ins), _joined(filters), outs))
        return outs[0] if len(outs) == 1 else tuple(outs)

    def tap(self, label, name=None):
        """Ask for a branch off `label`. The split node is created (in graph
        order) at the first call and grows by one output per further call.
        `name` pins the emitted label -- required where the label itself is
        part of a frozen contract."""
        t = self._taps.get(label)
        if t is None:
            t = _Tap(label)
            self._taps[label] = t
            self._nodes.append(t)
        out = name or "%s_t%d" % (label, len(t.outs) + 1)
        t.outs.append(out)
        return out

    # -- render -----------------------------------------------------------
    def render(self):
        """Return (filter_complex, input argv)."""
        # a tap with a single consumer is not a split at all: alias its one
        # output straight onto the source label and emit nothing.
        alias = {}
        for n in self._nodes:
            if isinstance(n, _Tap) and len(n.outs) == 1:
                alias[n.outs[0]] = n.src

        def res(lbl):
            for _ in range(len(alias) + 1):
                if lbl not in alias:
                    break
                lbl = alias[lbl]
            return lbl

        parts = []
        for n in self._nodes:
            if isinstance(n, _Tap):
                if len(n.outs) < 2:
                    continue                  # elided (or unused entirely)
                parts.append("[%s]split=%d%s"
                             % (res(n.src), len(n.outs),
                                "".join("[%s]" % o for o in n.outs)))
            else:
                parts.append("%s%s%s"
                             % ("".join("[%s]" % res(i) for i in n.ins),
                                n.filters,
                                "".join("[%s]" % o for o in n.outs)))
        return ";".join(parts), list(self._argv)
