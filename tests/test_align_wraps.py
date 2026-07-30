"""
Adversarial corpus for the repeat-detection DP in qc/author/align.py.

WHAT A "WRAP" IS. `align()` returns `(events, jumps)`. A jump is the DP moving
the reference pointer somewhere other than the next word; the ones that survive
the filter at the end of `align()` are the BACKWARD ones -- an ibtida' restart,
where the reciter pauses and picks the ayah up again from an earlier word. That
report is what the author step uses to caption the repeat twice. Two failure
modes matter and they pull in opposite directions:

  * MISSING a real restart. The repeated pass is then smeared over the wrong
    reference words and the caption runs ahead of the voice for the rest of the
    window.
  * INVENTING one. The Quran is full of refrains -- ar-Rahman's
    "fa-bi-ayyi ala'i rabbikuma tukadhdhiban" lands 31 times, al-Mursalat's
    "waylun yawma'idhin li-l-mukadhdhibin" 10 times -- and every one of them is
    a place where a DP that is even slightly too eager can rewind into an
    identical earlier instance and caption a straight-through recitation as a
    repeat.

WHY THE TESTS ARE BUILT THIS WAY. There is no audio here and no Whisper. The
ASR stream is SYNTHESISED from `qc.quran` (see `recite()`): every "ASR" word is
the mushaf word it stands for, normalised exactly the way `align.asr_forms`
normalises a real Whisper token, so `_sim` scores it 1.0 -- the same number a
perfect transcript would produce. That isolates the DP. The only ASR defects
modelled are the two that actually occur: a word DROPPED (a melisma the decoder
swallows -- `drop=`, which still advances the clock, because the reciter did say
it) and a token HALLUCINATED (a word from another surah spliced in). No Arabic
is typed in this file; every string is copied out of `qc.quran` programmatically.
The refrain surahs are LOCATED by `refrain_ayat()`, which finds the most
frequently recurring word-trigram in a surah -- the ayah numbers are discovered,
not asserted from memory.

WHY THE ASSERTIONS ARE SHAPED THIS WAY. A previous attempt at this corpus
shipped a "refrain confusion" test that was byte-identical in input to its
clean-recitation test and strictly weaker in what it checked, so it could not
fail. Two rules keep that from happening again:

  1. Every false-positive test asserts on the EVENTS as well as on `jumps`:
     the reference indices must never descend. An implementation that rewinds
     but forgets to report it fails just as hard as one that reports it.
  2. The suite is jointly non-trivial. Returning `jumps == []` unconditionally
     passes every test in `FalsePositiveRefrains` and fails every test in
     `TruePositiveRestart` and `Boundaries`; reporting a jump wherever a form
     recurs does the reverse. Nothing satisfies both except a DP that actually
     tells a restart from a refrain.

EXPECTED TO FAIL AT HEAD. Two tests specified behaviour the DP at HEAD did not
have; they are named in `EXPECTED_TO_FAIL_AT_HEAD` below, and they carried
`@unittest.expectedFailure` until the bounded-wraparound DP landed and made
them pass, at which point the decorators came off as the docstring below says
they should. Everything else PASSED at HEAD too and is a regression guard --
if a change turns any of those red, the change is wrong.

  * `FalsePositiveRefrains.test_dropped_ayah_bodies_do_not_rewind_into_a_refrain`
    HEAD reports a restart 18 -> 0 on a straight-through reading of 55:13-25 in
    which the ASR dropped the bodies of 55:17 and 55:19-20. Once two refrain
    instances become adjacent in the transcript, paying one 1.80 jump to reuse
    refs 0-3 is cheaper than paying 11 x 0.60 for the words the decoder missed,
    so the DP captions ayah 21's refrain as ayah 13's. A dropout must never be
    able to manufacture a restart.
  * `Boundaries.test_an_aborted_first_word_is_kept_twice`
    The reciter says the first word, stops, says it again, then carries on --
    the "aborts a word and begins again" case the module docstring names. HEAD
    absorbs the second utterance as a 0.60 gap_asr, because one jump costs 1.80,
    and the word appears once in `events`. It was recited twice.

When the DP was fixed these two turned into unittest "unexpected successes",
which `wasSuccessful()` counts as a failure -- deleting the decorator at that
point was the handoff, and it has been taken. Nothing else in this file has
been touched since it was written.

    tools/render-venv/bin/python -m unittest discover -s tests
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from qc import quran as Q                               # noqa: E402
from qc.author import align as A                        # noqa: E402

EXPECTED_TO_FAIL_AT_HEAD = (
    "FalsePositiveRefrains.test_dropped_ayah_bodies_do_not_rewind_into_a_refrain",
    "Boundaries.test_an_aborted_first_word_is_kept_twice",
)

# A murattal-ish clock. The numbers are not measurements of anything -- the DP
# never reads a time, it only copies them into the events -- but keeping words
# non-overlapping and strictly increasing is what makes `_fill_gaps` behave the
# way it does on real input, so the gap has to be real.
WORD_SEC = 0.42
GAP_SEC = 0.08
PAUSE_SEC = 0.90     # the breath a reciter takes before an ibtida'


def recite(ref, order, drop=(), pause_before=(), t=0.0):
    """Synthesise the ASR stream for a recitation. -> [{'w','form','t0','t1'}].

    `order` is the sequence of indices into `ref` the reciter actually uttered,
    so a restart is written as a sequence that goes backwards. `drop` is the
    set of POSITIONS IN `order` the decoder failed to emit: the clock still
    advances across them, because the word was recited -- that hole in time is
    exactly what a melisma dropout looks like downstream, and what
    `align._fill_gaps` interpolates across.

    The dicts are shaped like `align.asr_forms` output and the form is derived
    the same way, so `_sim` against the right reference word is 1.0 -- what a
    perfect transcript would score. Nothing here invents Arabic.
    """
    drop = set(drop)
    pause_before = set(pause_before)
    out = []
    for k, j in enumerate(order):
        if k in pause_before:
            t += PAUSE_SEC
        w = ref[j]["ar"]
        if k not in drop:
            out.append({"w": w, "form": Q.normalize(w).replace(" ", ""),
                        "t0": round(t, 3), "t1": round(t + WORD_SEC, 3)})
        t += WORD_SEC + GAP_SEC
    return out


def refs_of(events):
    return [e["ref"] for e in events]


def descents(seq):
    """The indices where a sequence goes backwards -> [(pos, prev, cur)]."""
    return [(i, a, b) for i, (a, b) in enumerate(zip(seq, seq[1:]), start=1)
            if b < a]


def ayat_in(events, ref):
    return {ref[e["ref"]]["ayah"] for e in events}


def refrain_ayat(surah, k=3):
    """The surah's refrain and the ayat carrying it, DISCOVERED not assumed.

    -> (trigram of normalised words, [ayah numbers]). The refrain is taken to
    be the k-word sequence appearing in the most ayat (ties broken by length).
    Asserting ayah numbers from memory is precisely how a refrain test ends up
    pointed at the wrong window and passing for the wrong reason, so the window
    is derived from the mushaf every run and the count is then pinned.
    """
    df = {}
    for n in range(1, Q.ayah_count(surah) + 1):
        forms = [w["forms"][0] for w in A.ref_words(surah, n, n)]
        for i in range(len(forms) - k + 1):
            df.setdefault(tuple(forms[i:i + k]), set()).add(n)
    best = max(df, key=lambda g: (len(df[g]), sum(len(x) for x in g)))
    return best, sorted(df[best])


def interior(ref, ayah):
    """An ayah's word indices minus its first and last.

    Dropping these models a dropout INSIDE an ayah while leaving the ayah
    evidenced at both ends -- so "is every ayah still represented?" stays a
    meaningful question. Dropping a whole ayah instead would make the answer
    no for reasons that have nothing to do with wrap detection.
    """
    idx = [i for i, r in enumerate(ref) if r["ayah"] == ayah]
    return tuple(idx[1:-1])


# ---------------------------------------------------------------------------
# 1. the restart that must be found
# ---------------------------------------------------------------------------

class TruePositiveRestart(unittest.TestCase):
    """at-tawbah 9:128 -- the clip this module was written for.

    The reciter runs to "'anittum", breathes, and takes the ayah up again from
    "'azizun 'alayhi ma 'anittum", then finishes. That is one ibtida', and the
    four repeated words have to be captioned twice.
    """

    SURAH, AYAH = 9, 128
    FIRST_PASS_END = 8      # 'anittum, the last word before the breath
    RESTART_AT = 5          # 'azizun, where he picks it back up

    def setUp(self):
        self.ref = A.ref_words(self.SURAH, self.AYAH, self.AYAH)
        # Pin the shape the indices above are indices INTO. If the mushaf or
        # the display-word split ever changes underneath, this fails loudly
        # rather than silently testing a restart between different words.
        self.assertEqual(len(self.ref), 14)
        self.assertEqual({r["ayah"] for r in self.ref}, {self.AYAH})
        order = (list(range(0, self.FIRST_PASS_END + 1))
                 + list(range(self.RESTART_AT, len(self.ref))))
        self.order = order
        self.asr = recite(self.ref, order,
                          pause_before=(self.FIRST_PASS_END + 1,))
        self.events, self.jumps = A.align(self.asr, self.ref)

    def jump(self):
        """The one wrap. Asserted here so a wrong COUNT fails as a failure
        rather than as an IndexError three lines later."""
        self.assertEqual(len(self.jumps), 1, self.jumps)
        return self.jumps[0]

    def test_exactly_one_wrap_is_reported(self):
        self.assertEqual(self.jump()["kind"], "restart")

    def test_the_wrap_names_the_right_reference_words(self):
        j = self.jump()
        self.assertEqual(j["from_ref"], self.FIRST_PASS_END)
        self.assertEqual(j["to_ref"], self.RESTART_AT)
        # And the words it names are the ones the reciter actually moved
        # between -- compared against the mushaf strings, never against typed
        # Arabic.
        self.assertEqual(self.ref[j["from_ref"]]["ar"],
                         self.ref[self.FIRST_PASS_END]["ar"])
        self.assertEqual(self.ref[j["to_ref"]]["ar"],
                         self.ref[self.RESTART_AT]["ar"])

    def test_the_wrap_is_timed_at_the_second_pass(self):
        # It must be reported at the ibtida', not at the start of the window:
        # the caption split is taken from this time.
        j = self.jump()
        first_pass_len = self.FIRST_PASS_END + 1
        self.assertAlmostEqual(j["t"], self.asr[first_pass_len]["t0"], places=3)

    def test_events_replay_the_recitation_in_order(self):
        # The whole contract of `events`: recitation order, repeats included.
        self.assertEqual(refs_of(self.events), self.order)

    def test_the_repeated_words_appear_twice(self):
        got = refs_of(self.events)
        for j in range(self.RESTART_AT, self.FIRST_PASS_END + 1):
            self.assertEqual(got.count(j), 2,
                             "ref %d (%s) recited twice" % (j, self.ref[j]["ar"]))
        # ...and every other word exactly once. A DP that "solves" the repeat
        # by duplicating the tail would pass the check above.
        for j in list(range(0, self.RESTART_AT)) + \
                list(range(self.FIRST_PASS_END + 1, len(self.ref))):
            self.assertEqual(got.count(j), 1, j)

    def test_the_only_descent_in_events_is_the_reported_wrap(self):
        j = self.jump()
        d = descents(refs_of(self.events))
        self.assertEqual(len(d), 1, d)
        _, prev, cur = d[0]
        self.assertEqual((prev, cur), (j["from_ref"], j["to_ref"]))

    def test_every_event_is_measured_not_interpolated(self):
        # Nothing was dropped, so `_fill_gaps` has no work to do; a synthetic
        # event here would mean the DP lost a word it was handed.
        self.assertEqual([e for e in self.events if e["sim"] is None], [])
        self.assertEqual(len(self.events), len(self.asr))


# ---------------------------------------------------------------------------
# 2 + 3. the refrains that must NOT be read as restarts
# ---------------------------------------------------------------------------

class FalsePositiveRefrains(unittest.TestCase):
    """Straight-through recitations of refrain-heavy windows, with dropouts.

    Every case here is a reciter who never went back. The ASR is missing words
    -- which is the pressure, because the cheapest way to explain a hole in a
    surah whose lines rhyme is to claim the reciter rewound to the previous
    identical line. He did not. Zero wraps, and the events must not descend.
    """

    def assertNoWrap(self, asr, ref, msg=""):
        events, jumps = A.align(asr, ref)
        self.assertEqual(jumps, [], "spurious wrap %s: %s" % (msg, jumps))
        # Stronger than `jumps == []`: an implementation that rewinds the
        # pointer and simply does not report it is just as broken.
        self.assertEqual(descents(refs_of(events)), [],
                         "events rewound %s" % msg)
        return events

    def assertEveryAyahRepresented(self, events, ref, msg=""):
        want = {r["ayah"] for r in ref}
        self.assertEqual(ayat_in(events, ref), want,
                         "ayat lost %s: %s" % (msg, sorted(want - ayat_in(events, ref))))

    # -- ar-Rahman, the hardest case -----------------------------------------

    def test_ar_rahman_refrain_is_located_where_we_think_it_is(self):
        # If this fails, every ar-Rahman test below is aimed at the wrong words.
        _, ayat = refrain_ayat(55)
        self.assertEqual(len(ayat), 31)
        self.assertEqual(ayat[:6], [13, 16, 18, 21, 23, 25])

    def test_ar_rahman_clean_recitation_has_no_wrap(self):
        ref = A.ref_words(55, 13, 25)
        events = self.assertNoWrap(recite(ref, list(range(len(ref)))), ref)
        self.assertEqual(refs_of(events), list(range(len(ref))))

    def test_ar_rahman_dropouts_do_not_manufacture_a_wrap(self):
        """55:13-25 with the melisma dropout at five different places.

        The window carries the refrain six times. Each sub-case deletes three
        consecutive words from the transcript -- once in an ayah body near the
        start, once mid-window, once near the end, and twice INSIDE a refrain
        instance, which is the worst of them: three quarters of one refrain is
        missing and the surviving word is identical to a word in five other
        refrains. Varying the position is the point; one hard-coded fixup
        cannot satisfy all five.
        """
        ref = A.ref_words(55, 13, 25)
        self.assertEqual(len(ref), 56)
        order = list(range(len(ref)))
        for drop in ((5, 6, 7),          # body of 55:14, early
                     (30, 31, 32),       # body of 55:20, mid-window
                     (47, 48, 49),       # body of 55:24, late
                     (15, 16, 17),       # the refrain of 55:16, gutted
                     (42, 43, 44)):      # the refrain of 55:23, gutted
            with self.subTest(drop=drop):
                asr = recite(ref, order, drop=drop)
                self.assertEqual(len(asr), len(ref) - 3)
                events = self.assertNoWrap(asr, ref, "drop=%s" % (drop,))
                self.assertEveryAyahRepresented(events, ref, "drop=%s" % (drop,))
                # The dropped words come back as interpolated events, not as
                # matches stolen from another refrain.
                synthetic = {e["ref"] for e in events if e["sim"] is None}
                self.assertTrue(set(drop) <= synthetic,
                                "%s not interpolated (got %s)"
                                % (drop, sorted(synthetic)))

    def test_dropped_ayah_bodies_do_not_rewind_into_a_refrain(self):
        """The one that killed the last attempt. See the module docstring.

        Straight through 55:13-25; the decoder loses the whole body of 55:17
        and the whole bodies of 55:19 and 55:20, so three refrain instances end
        up adjacent in the transcript with nothing between them. HEAD pays one
        jump and re-uses refs 0-3 for 55:21's refrain -- it captions ayah 21 as
        ayah 13 and reports a restart that never happened. There is no restart
        in this audio and there must be no wrap in the answer.
        """
        ref = A.ref_words(55, 13, 25)
        drop = tuple(range(19, 23)) + tuple(range(27, 34))
        self.assertNoWrap(recite(ref, list(range(len(ref))), drop=drop), ref,
                          "dropped ayah bodies")

    # -- the other refrains ---------------------------------------------------

    def _refrain_window(self, surah, n_instances, expect_count):
        """The window spanning the first `n_instances` refrain ayat."""
        _, ayat = refrain_ayat(surah)
        self.assertEqual(len(ayat), expect_count, ayat)
        return A.ref_words(surah, ayat[0], ayat[n_instances - 1])

    def _sweep(self, surah, ref):
        """Drop each ayah's interior in turn, then all of them at once."""
        order = list(range(len(ref)))
        ayat = sorted({r["ayah"] for r in ref})
        cases = [("ayah %d" % a, interior(ref, a)) for a in ayat]
        cases.append(("every ayah at once",
                      tuple(i for a in ayat for i in interior(ref, a))))
        clean = self.assertNoWrap(recite(ref, order), ref, "clean")
        self.assertEqual(refs_of(clean), order)
        for name, drop in cases:
            if not drop:
                continue
            with self.subTest(surah=surah, drop=name):
                tag = "%d %s" % (surah, name)
                events = self.assertNoWrap(recite(ref, order, drop=drop), ref, tag)
                self.assertEveryAyahRepresented(events, ref, tag)

    def test_al_qamar_fahal_min_muddakir_refrain(self):
        # 54's refrain closes six ayat, and 54:17 and 54:22 are the SAME ayah
        # word for word -- the window below contains both.
        ref = self._refrain_window(54, 3, expect_count=6)
        self.assertEqual(sorted({r["ayah"] for r in ref}), list(range(15, 23)))
        self._sweep(54, ref)

    def test_al_mursalat_waylun_yawmaidhin_refrain(self):
        ref = self._refrain_window(77, 3, expect_count=10)
        self.assertEqual(sorted({r["ayah"] for r in ref}), list(range(15, 25)))
        self._sweep(77, ref)

    def test_ash_shuara_fattaqullaha_wa_atiuni_refrain(self):
        # 26:108 and 26:110 are identical and only one ayah apart -- the
        # tightest recurrence of the three.
        ref = self._refrain_window(26, 2, expect_count=8)
        self.assertEqual(sorted({r["ayah"] for r in ref}), [108, 109, 110])
        self._sweep(26, ref)


# ---------------------------------------------------------------------------
# 4. a word that repeats inside a single ayah
# ---------------------------------------------------------------------------

class RepeatWithinOneAyah(unittest.TestCase):
    """Repetition that is IN THE TEXT, not in the performance.

    Nothing was recited twice here, so nothing may be reported. This is the
    case that catches an implementation which decides "this form has been seen
    before, therefore he restarted" without asking where the reference pointer
    is.
    """

    def _check(self, surah, ayah, dup_at, drop=()):
        i, j = dup_at
        ref = A.ref_words(surah, ayah, ayah)
        # The premise, read out of the mushaf rather than asserted from memory:
        # the two words are INDISTINGUISHABLE to the matcher. Note the premise
        # is about the skeleton, not the spelling -- 55:60 writes the second
        # occurrence with a different final vowel, and a transcript cannot see
        # that, which is exactly why the DP has to resolve it by position.
        self.assertEqual(ref[i]["forms"], ref[j]["forms"])
        self.assertEqual(Q.normalize(ref[i]["ar"]), Q.normalize(ref[j]["ar"]))
        events, jumps = A.align(recite(ref, list(range(len(ref))), drop=drop),
                                ref)
        self.assertEqual(jumps, [], "%d:%d drop=%s" % (surah, ayah, drop))
        self.assertEqual(refs_of(events), list(range(len(ref))))
        return events

    def test_al_asr_watawasaw_twice_in_one_ayah(self):
        # 103:3 says "watawasaw bi-l-haqqi watawasaw bi-s-sabr".
        self._check(103, 3, (5, 7))

    def test_al_asr_survives_the_word_between_the_two_being_dropped(self):
        # With "bi-l-haqqi" missing, the transcript reads the duplicated word
        # back to back -- as loud an invitation to rewind as the text offers.
        events = self._check(103, 3, (5, 7), drop=(6,))
        self.assertEqual([e["ref"] for e in events if e["sim"] is None], [6])

    def test_ar_rahman_60_al_ihsan_twice_in_one_ayah(self):
        # "hal jaza'u l-ihsani illa l-ihsan" -- one word between the two.
        self._check(55, 60, (2, 4))

    def test_ar_rahman_60_survives_the_word_between_the_two_being_dropped(self):
        self._check(55, 60, (2, 4), drop=(3,))


# ---------------------------------------------------------------------------
# 5. boundaries
# ---------------------------------------------------------------------------

class Boundaries(unittest.TestCase):

    def setUp(self):
        self.ref = A.ref_words(9, 128, 128)

    def test_empty_asr(self):
        self.assertEqual(A.align([], self.ref), ([], []))

    def test_empty_reference(self):
        self.assertEqual(A.align(recite(self.ref, [0]), []), ([], []))

    def test_a_single_asr_word_at_the_start(self):
        events, jumps = A.align(recite(self.ref, [0]), self.ref)
        self.assertEqual(jumps, [])
        self.assertEqual(refs_of(events), [0])

    def test_a_single_asr_word_from_the_middle(self):
        # The window may open mid-ayah, so every start state is free: one word
        # must land on ITS reference word, not on ref 0.
        events, jumps = A.align(recite(self.ref, [7]), self.ref)
        self.assertEqual(jumps, [])
        self.assertEqual(refs_of(events), [7])

    def test_asr_longer_than_the_reference(self):
        # A short ayah recited three times over: the reference runs out long
        # before the audio does, and every pass has to be kept.
        ref = A.ref_words(112, 1, 1)
        n = len(ref)
        order = list(range(n)) * 3
        events, jumps = A.align(recite(ref, order, pause_before=(n, 2 * n)), ref)
        self.assertEqual(refs_of(events), order)
        self.assertEqual(len(jumps), 2, jumps)
        for j in jumps:
            self.assertEqual((j["from_ref"], j["to_ref"]), (n - 1, 0))

    def test_a_restart_at_the_very_first_word(self):
        # He opens, gets two words in, stops, and opens again. The wrap is at
        # the top of the window, where there is no earlier context to lean on.
        n = len(self.ref)
        order = [0, 1] + list(range(0, n))
        events, jumps = A.align(recite(self.ref, order, pause_before=(2,)),
                                self.ref)
        self.assertEqual(refs_of(events), order)
        self.assertEqual(len(jumps), 1, jumps)
        self.assertEqual((jumps[0]["from_ref"], jumps[0]["to_ref"]), (1, 0))

    @unittest.expectedFailure
    def test_an_aborted_first_word_is_kept_twice(self):
        """One word, aborted and begun again. See the module docstring.

        `events` is the record of what was RECITED. He said the word, stopped,
        and said it again; HEAD keeps it once because absorbing the second
        utterance as a gap is cheaper than a jump. Whether that shows up in
        `jumps` is a judgement call -- a one-word abort is arguably not an
        ibtida' -- so this only pins the events.

        EXPECTED TO FAIL, AND NOT A BUG TO BE FIXED BY REPRICING. Detecting a
        one-word rewind needs W(1) < REWARD + COST_GAP_ASR = 1.60. Staying safe
        against a spurious one-word rewind needs W(1) > COST_SKIP = 1.80, since
        that is the most a detour into an identical earlier copy can ever save.
        The band is empty by 0.20: no pair of wrap constants satisfies both.

        The tie is broken toward safety, and the asymmetry is the reason. A
        missed one-word abort costs this clip a hand-fix, and a one-word repeat
        is far more often an accidental overlap than an ibtida' anyway. A
        spurious one, priced cheaply enough to catch this, duplicates Qur'anic
        words on screen and drops real ayat from the timeline -- which is what
        the first port of this DP actually did.

        This is left as a live expected-failure rather than deleted so the
        limitation stays visible. If someone ever separates the two cases
        STRUCTURALLY -- e.g. by requiring that the ASR actually re-emitted a
        word matching the rewind target, which is true of a genuine abort and
        false of a dropout-driven detour -- this becomes reachable, and
        `WrapPricingInvariant.test_the_one_word_miss_is_deliberate` records the
        trade that would then be revisited.
        """
        n = len(self.ref)
        order = [0] + list(range(0, n))
        events, _ = A.align(recite(self.ref, order, pause_before=(1,)), self.ref)
        self.assertEqual(refs_of(events).count(0), 2)
        self.assertEqual(refs_of(events), order)

    def test_two_restarts_in_one_window(self):
        # Two ibtida's in a single ayah, the second starting later than the
        # first. Both must be reported, in the order they happened, and the
        # events must replay all three passes.
        order = (list(range(0, 5)) + list(range(2, 9))
                 + list(range(5, len(self.ref))))
        events, jumps = A.align(
            recite(self.ref, order, pause_before=(5, 12)), self.ref)
        self.assertEqual(refs_of(events), order)
        self.assertEqual([(j["from_ref"], j["to_ref"]) for j in jumps],
                         [(4, 2), (8, 5)])
        self.assertEqual([j["kind"] for j in jumps], ["restart", "restart"])
        self.assertEqual(len(descents(refs_of(events))), 2)

    def test_a_restart_spanning_an_ayah_boundary(self):
        # He crosses into the next ayah, stops, and picks up from the middle of
        # the previous one -- the wrap has to be allowed to cross the seam.
        ref = A.ref_words(55, 13, 16)
        restart_at, turn = 2, 6
        order = list(range(0, turn)) + list(range(restart_at, len(ref)))
        events, jumps = A.align(recite(ref, order, pause_before=(turn,)), ref)
        self.assertEqual(refs_of(events), order)
        self.assertEqual(len(jumps), 1, jumps)
        j = jumps[0]
        self.assertEqual((j["from_ref"], j["to_ref"]), (turn - 1, restart_at))
        self.assertNotEqual(ref[j["from_ref"]]["ayah"], ref[j["to_ref"]]["ayah"])


# ---------------------------------------------------------------------------
# 6. the no-repeat path, frozen
# ---------------------------------------------------------------------------

class ContractPreservation(unittest.TestCase):
    """What HEAD emits on a clean recitation, captured verbatim.

    Everything downstream -- energy snapping, card splitting, the English cut
    -- reads `events`, and the overwhelming majority of clips contain no repeat
    at all. Whatever the repeat detection becomes, it must not perturb this.
    The literal below is HEAD's own output, transcribed from a run, not a
    prediction; it covers the four paths a clean window exercises: match,
    gap_ref, gap_asr and the `_fill_gaps` interpolation.
    """

    SURAH, A_, B_ = 82, 6, 9
    DROPPED_REF = 12          # a word the decoder swallowed
    HALLUCINATED_BEFORE = 10  # a token from another surah, spliced in here

    # (ref, i, t0, t1, sim), times and sims rounded to 6 dp.
    HEAD_EVENTS = (
        (0, 0, 0.0, 0.42, 1.0),
        (1, 1, 0.5, 0.92, 1.0),
        (2, 2, 1.0, 1.42, 1.0),
        (3, 3, 1.5, 1.92, 1.0),
        (4, 4, 2.0, 2.42, 1.0),
        (5, 5, 2.5, 2.92, 1.0),
        (6, 6, 3.0, 3.42, 1.0),
        (7, 7, 3.5, 3.92, 1.0),
        (8, 8, 4.0, 4.42, 1.0),
        (9, 9, 4.5, 4.92, 1.0),
        (10, 11, 5.0, 5.42, 1.0),
        (11, 12, 5.5, 5.92, 1.0),
        (12, None, 5.92, 6.21, None),
        (13, 13, 6.5, 6.92, 1.0),
        (14, 14, 7.0, 7.42, 1.0),
        (15, 15, 7.5, 7.92, 1.0),
        (16, 16, 8.0, 8.42, 1.0),
        (17, 17, 8.5, 8.92, 1.0),
        (18, 18, 9.0, 9.42, 1.0),
        (19, 19, 9.5, 9.92, 1.0),
    )

    def _stream(self):
        ref = A.ref_words(self.SURAH, self.A_, self.B_)
        asr = recite(ref, list(range(len(ref))), drop=(self.DROPPED_REF,))
        # The hallucination is a real mushaf word from a DIFFERENT surah,
        # copied out of qc.quran -- Whisper's inventions are real Arabic words,
        # and a made-up string would be a softer test.
        alien = A.ref_words(112, 1, 1)[0]["ar"]
        k = [n for n, w in enumerate(asr)
             if w["w"] == ref[self.HALLUCINATED_BEFORE]["ar"]][0]
        t0 = round((asr[k - 1]["t1"] + asr[k]["t0"]) / 2.0, 3)
        asr.insert(k, {"w": alien, "form": Q.normalize(alien).replace(" ", ""),
                       "t0": t0, "t1": round(t0 + 0.2, 3)})
        return ref, asr

    def test_clean_window_events_are_unchanged(self):
        ref, asr = self._stream()
        events, jumps = A.align(asr, ref)
        self.assertEqual(jumps, [])
        got = tuple((e["ref"], e["i"], round(e["t0"], 6), round(e["t1"], 6),
                     None if e["sim"] is None else round(e["sim"], 6))
                    for e in events)
        self.assertEqual(got, self.HEAD_EVENTS)

    def test_clean_window_events_carry_the_mushaf_spelling(self):
        # The event's `asr` field is Whisper's spelling and is throwaway; what
        # the caption uses is ref[e['ref']]. Pin that the indices still line up
        # with the reference list, which is the whole safety property.
        ref, asr = self._stream()
        events, _ = A.align(asr, ref)
        self.assertEqual([e["ref"] for e in events], list(range(len(ref))))
        self.assertEqual([ref[e["ref"]]["ayah"] for e in events],
                         [r["ayah"] for r in ref])

    def test_a_perfect_transcript_is_the_identity_alignment(self):
        # No dropout, no hallucination: one event per ASR word, in order, every
        # time copied through untouched and every similarity exact.
        for surah, a, b in ((9, 128, 128), (82, 6, 9), (103, 1, 3)):
            with self.subTest(surah=surah):
                ref = A.ref_words(surah, a, b)
                asr = recite(ref, list(range(len(ref))))
                events, jumps = A.align(asr, ref)
                self.assertEqual(jumps, [])
                self.assertEqual(len(events), len(asr))
                for k, e in enumerate(events):
                    self.assertEqual((e["ref"], e["i"]), (k, k))
                    self.assertEqual(e["t0"], asr[k]["t0"])
                    self.assertEqual(e["t1"], asr[k]["t1"])
                    self.assertEqual(e["sim"], 1.0)
                    self.assertEqual(e["asr"], ref[k]["ar"])


class WrapPricingInvariant(unittest.TestCase):
    """The safety argument in align.py, asserted directly on the constants.

    Every other false-positive test here is BEHAVIOURAL: it builds a window,
    aligns it, and checks no wrap came back. That is necessary and it is not
    sufficient -- a behavioural test only probes the distances its own window
    happens to present, and the first port of this DP shipped a green suite
    while priced with a hole at d = 1 and d = 2, because no test in the suite
    ever put a one-word rewind in front of it.

    So this asserts the inequality itself, over every distance rather than
    over the distances a test author thought to write down. It is the cheapest
    test in the file and it is the one that would have caught the regression.
    """

    def test_safety_holds_at_every_distance(self):
        # W(d) > COST_SKIP for EVERY d >= 1. COST_SKIP is the most a detour
        # into an identical earlier copy can ever save, so once the wrap costs
        # more than the cap, no detour pays for itself at any distance. d is
        # bounded above only by the window, so check well past any real one.
        for d in range(1, 200):
            w = A.COST_JUMP + A.COST_JUMP_SPAN * d
            self.assertGreater(
                w, A.COST_SKIP,
                "wrap at distance %d costs %.2f, at or under the %.2f a "
                "forward skip is capped at: a straight-through recitation "
                "with a dropout can be bought as an ibtida'" % (d, w, A.COST_SKIP))

    def test_detection_holds_from_two_words(self):
        # A contiguous rewind of L words is worth (REWARD + COST_GAP_ASR) * L
        # = 1.60 * L, and sits at d == L. The gate must stay under that from
        # L = 2 up, or a real ibtida' is never bought.
        for L in range(2, 200):
            w = A.COST_JUMP + A.COST_JUMP_SPAN * L
            self.assertLess(
                w, (1.0 + A.COST_GAP_ASR) * L,
                "a %d-word restart costs %.2f but is only worth %.2f: it "
                "would never be detected" % (L, w, (1.0 + A.COST_GAP_ASR) * L))

    def test_the_one_word_miss_is_deliberate(self):
        # Documented in the cost block: a single repeated word is far more
        # often an accidental overlap than an ibtida', so L = 1 is priced OUT
        # on purpose. If someone ever makes it detectable they have reopened
        # the d = 1 hole, and this fails to tell them which trade they made.
        self.assertGreater(A.COST_JUMP + A.COST_JUMP_SPAN,
                           (1.0 + A.COST_GAP_ASR),
                           "a one-word rewind became detectable; that is the "
                           "same edge the d = 1 safety bound needs to be "
                           "unaffordable")


class ShortDistanceRewinds(FalsePositiveRefrains):
    """The span-1 and span-2 pressure the behavioural suite was blind to.

    Inherits the assertions from FalsePositiveRefrains deliberately -- the
    claim under test is the same one ("he never went back"), only the distance
    is short enough that the previous pricing could afford it.
    """

    def test_dropout_next_to_a_repeated_word_is_not_a_rewind(self):
        # A window whose text repeats a single word at short range, recited
        # straight through, with the decoder losing words right beside the
        # repeat -- the exact shape that made a d = 1 wrap look cheap.
        ref = A.ref_words(55, 13, 25)
        n = len(ref)
        for start in (3, 7, 11):
            for width in (1, 2, 3):
                if start + width >= n:
                    continue
                order = list(range(n))
                drop = range(start, start + width)
                with self.subTest(start=start, width=width):
                    self.assertNoWrap(recite(ref, order, drop=drop), ref,
                                      "dropout %d..%d" % (start, start + width))

    def test_short_dropouts_across_a_refrain_surah(self):
        for surah in (54, 77):
            _, ayat = refrain_ayat(surah)
            if len(ayat) < 2:
                continue
            ref = A.ref_words(surah, ayat[0], ayat[min(3, len(ayat) - 1)])
            n = len(ref)
            for start in range(1, min(n - 2, 14), 4):
                with self.subTest(surah=surah, start=start):
                    events = self.assertNoWrap(
                        recite(ref, list(range(n)), drop=(start, start + 1)),
                        ref, "surah %d drop at %d" % (surah, start))
                    self.assertEveryAyahRepresented(events, ref,
                                                    "surah %d" % surah)


if __name__ == "__main__":
    unittest.main()
