import { CopyBar, FeedbackList, FeedbackProvider, IdeasBox, type Item, type Option } from '@/components/feedback'

type F = {
  id: string
  group: string
  title: string
  what: string
  fix: string
  gain: string
  measured?: boolean
  risk: 'low' | 'medium'
  effort: 'S' | 'M'
  where: string
  rec?: string
  why?: string
  options?: Option[]
}

const DO: Option[] = [
  { id: 'a', label: 'Do it' },
  { id: 'b', label: 'Skip' },
]

const findings: F[] = [
  // ---------------------------------------------------------------- speed
  {
    id: 'F1', group: 'Speed',
    title: 'Split checks reuse their preview cut instead of re-cutting the video every time',
    what: 'Every `generate.py --verify-only` re-encodes the same window into a 32px preview, even when only groups or English changed. That cut is 85% of the run.',
    fix: 'Keep the preview in the reel\'s tmp folder with a small note of what it was cut from (source file, size, modified time, trim). Reuse it when they match.',
    gain: 'hujurat 1.64s → 0.29s, ahzab 2.01s → 0.28s per check (CPU 6–10s → 0.3s). Paid on every split check.',
    measured: true, risk: 'medium', effort: 'S', where: 'generate.py:1088-1098',
    rec: 'a', why: 'The step you repeat most while authoring. A stale preview is the risk, so the key covers source and trim.',
  },
  {
    id: 'F2', group: 'Speed',
    title: 'Wrong group word totals fail when the config loads, not after the video is cut',
    what: 'Group counts are summed only in apply_groups, after probing and trimming, and after align and crop have already run. AGENTS.md says this check belongs at config time.',
    fix: 'For a named span, check the total in check_shapes (it already counts the span\'s words for ayah_end_words). Keep the later check for auto-detected spans.',
    gain: 'Bad counts fail in 0.08s instead of 1.47s, and before align or a paid crop call.',
    measured: true, risk: 'low', effort: 'S', where: 'generate.py:628-638, 945-1009',
    rec: 'a', why: 'Faster and matches the "loud validation at config time" rule.',
  },
  {
    id: 'F3', group: 'Speed',
    title: 'Align runs the speech model once per audio window, not once per reel',
    what: 'The three hujurat styles share one source and trim, yet align runs MMS on the same audio three times in one batch.',
    fix: 'Keep the last window\'s model output in memory, keyed by source and trim. Text alignment, repeat repair and output stay per config.',
    gain: '~2.7s saved for each extra style from the same window (5.4s on the three-style hujurat batch). Output files byte-identical.',
    measured: true, risk: 'low', effort: 'S', where: 'align.py:403-407',
    rec: 'a', why: 'About 6 lines. Pays every time you cut several styles from one window.',
  },
  {
    id: 'F4', group: 'Speed',
    title: 'fetch probes a cached source once instead of three times',
    what: 'The cached path runs ffprobe separately for duration, video info and audio codec on the same file.',
    fix: 'One ffprobe JSON call, shared by the usability check and the description. Probe again after any re-encode.',
    gain: '85–140ms per cached fetch. Mostly a tidier fetch.py.',
    measured: true, risk: 'low', effort: 'S', where: 'fetch.py:119-152, 186-222',
    rec: 'a', why: 'Small time saving, but it removes duplicated probe code too.',
  },
  {
    id: 'F5', group: 'Speed',
    title: 'install.sh imports each heavy package once, not twice',
    what: 'A healthy rerun imports mlx_whisper and ctc_forced_aligner to decide whether to install, then again for the summary.',
    fix: 'Remember the first result; only re-import after an install attempt.',
    gain: '~5.8s per install.sh rerun.',
    measured: true, risk: 'low', effort: 'S', where: 'install.sh:128-170',
    rec: 'a', why: 'Rarely run, but the fix is two variables.',
  },
  {
    id: 'F6', group: 'Speed',
    title: 'Publish: post to Facebook while Instagram is still processing',
    what: 'Facebook waits until Instagram has uploaded, processed (polled up to 300s) and published.',
    fix: 'Option a keeps it sequential with no threads: upload to Instagram, do all of Facebook, then poll Instagram and publish. Option b runs both at once in threads.',
    gain: 'Estimate: saves whichever is shorter, Instagram\'s processing wait or the Facebook post. Not measured (no live posting in the audit).',
    measured: false, risk: 'medium', effort: 'M', where: 'publish.py:295-306, 479-497',
    options: [
      { id: 'a', label: 'Reorder steps, no threads', detail: 'Same waiting saved, nothing runs at the same time' },
      { id: 'b', label: 'Run both at once (threads)', detail: 'More code, harder error handling on posts you cannot undo' },
      { id: 'c', label: 'Skip' },
    ],
    rec: 'a', why: 'Gets the overlap with no concurrency. Tested with mocked API calls only.',
  },
  // ------------------------------------------------------------ correctness
  {
    id: 'F7', group: 'Correctness & safety',
    title: 'A reused alignment must have the same words, not just the same count',
    what: 'Change the span to another verse with the same word count and the old timings are accepted. Tested: 49:4-5 timings were accepted for 2:142.',
    fix: '.align.json already stores each word. Compare them with the mushaf words in generate.py\'s reader and in align.py\'s reuse check, and re-align or refuse when they differ.',
    gain: 'Stops captions silently landing on the wrong timings. ~4 lines.',
    measured: true, risk: 'low', effort: 'S', where: 'generate.py:551-570, align.py:314-367',
    rec: 'a', why: 'A real silent-wrong-output bug, and the fix is a list comparison.',
  },
  {
    id: 'F8', group: 'Correctness & safety',
    title: 'graph_parity fails when a golden file is missing instead of writing a new one',
    what: 'If a golden file is missing, the check writes one from the current code and passes.',
    fix: 'Write goldens only with --bless; otherwise report the missing file and exit non-zero. Clearing __pycache__ stays as it is (that is deliberate).',
    gain: 'The byte-identity gate can no longer pass by accident.',
    risk: 'low', effort: 'S', where: 'tests/graph_parity.py:127-132',
    rec: 'a',
  },
  {
    id: 'F9', group: 'Correctness & safety',
    title: 'graph_parity also checks the caption images\' pixels',
    what: 'The test compares the filtergraph text, which only includes image paths and boxes. A colour change inside the caption PNGs passes.',
    fix: 'Add a golden hash of each generated bar/text image\'s decoded pixels, recorded with --bless.',
    gain: 'Makes renderer cleanups (F12–F14) provably pixel-identical.',
    risk: 'low', effort: 'S', where: 'tests/graph_parity.py:98-110',
    rec: 'a', why: 'Do this before touching drawing code.',
  },
  {
    id: 'F10', group: 'Correctness & safety',
    title: 'crop --annotate always writes a fresh image, including for empty shots',
    what: 'With no reciter in frame, crop succeeds but writes no annotation, so the skill\'s check of /tmp/c.png can show the previous reel\'s image.',
    fix: 'Annotate empty shots too (centred window, "no reciter" label), and use a per-reel annotation path in the skill.',
    gain: 'You can\'t approve the wrong reel\'s framing by mistake.',
    risk: 'low', effort: 'S', where: 'crop.py:445-465, SKILL.md',
    rec: 'a',
  },
  {
    id: 'F11', group: 'Correctness & safety',
    title: 'install.sh checks the same ffmpeg/ffprobe/yt-dlp the pipeline uses',
    what: 'The pipeline uses QC_FFMPEG / QC_FFPROBE / QC_YT_DLP from .env when they are set; install.sh only checks the copies on PATH.',
    fix: 'Look these up through generate.envvar (install.sh already does this for other settings) and run the perlin check on that ffmpeg.',
    gain: 'Install summary says OK only when the pipeline will actually run.',
    risk: 'low', effort: 'S', where: 'install.sh:50-68',
    rec: 'a', why: 'Only matters if you ever set these overrides. Skip if you never do.',
  },
  // ---------------------------------------------------------------- cleanup
  {
    id: 'F12', group: 'Code cleanup (same output)',
    title: 'One drop-shadow helper for both renderers',
    what: 'render_text and render_bars each build a shadow the same way; bars also runs an affine transform where a pasted offset does the same thing.',
    fix: 'One with_shadow(ink, color, alpha, offset) in render_common. Each renderer keeps its own constants.',
    gain: '~15 lines, one owner. Pixel-identical (needs F9).',
    risk: 'low', effort: 'S', where: 'render_text.py:451-459, render_bars.py:376-387',
    rec: 'a',
  },
  {
    id: 'F13', group: 'Code cleanup (same output)',
    title: 'render_bars: drop strip_tashkeel, use norm_ar',
    what: 'strip_tashkeel(s) is norm_ar(s, TASHKEEL) character for character, used once.',
    fix: 'Call norm_ar(ln, TASHKEEL) and delete the function.',
    gain: '~4 lines.',
    risk: 'low', effort: 'S', where: 'render_bars.py:149-150, 349',
    rec: 'a',
  },
  {
    id: 'F14', group: 'Code cleanup (same output)',
    title: 'render_text.en_tokens: remove a redundant whitespace branch',
    what: 'The early branch for empty/whitespace chunks does exactly what the loop below already does.',
    fix: 'Delete the three-line branch.',
    gain: '3 lines.',
    risk: 'low', effort: 'S', where: 'render_text.py:179-198',
    rec: 'a',
  },
  {
    id: 'F15', group: 'Code cleanup (same output)',
    title: 'wrap_parity: one first-fit helper instead of two copies',
    what: 'enumerated() and line_count() each carry the same first-fit wrapping.',
    fix: 'One test-local first_fit(); line_count becomes len(first_fit(...)).',
    gain: '~10–13 lines.',
    risk: 'low', effort: 'S', where: 'tests/wrap_parity.py:46-121',
    rec: 'a',
  },
  {
    id: 'F16', group: 'Code cleanup (same output)',
    title: 'quran._index: drop an unused list and a redundant dedupe set',
    what: 'The index builds a "norm" list nobody reads (5.3 MB of the 21.5 MB index), and a "seen" set that can never catch anything.',
    fix: 'Remove both. Tables, tokens() and normalize() untouched.',
    gain: '6 lines, 5 MB. Postings identical over all 6236 ayat.',
    measured: true, risk: 'low', effort: 'S', where: 'quran.py:309-327',
    rec: 'a',
  },
  {
    id: 'F17', group: 'Code cleanup (same output)',
    title: 'Two append-loops become list comprehensions',
    what: 'quran.display_words and generate.identify_verse_span build lists with append/extend loops.',
    fix: 'Write each as a comprehension.',
    gain: '~5 lines.',
    risk: 'low', effort: 'S', where: 'quran.py:176-181, generate.py:576-578',
    rec: 'a',
  },
  {
    id: 'F18', group: 'Code cleanup (same output)',
    title: 'Silence clipping: one condition, no pointless max()',
    what: 'clip_to_speech skips open-ended silences in one branch, then clamps with a max() that can never change the value.',
    fix: 'if s_end is not None and start < s_start < end: end = s_start',
    gain: '~2 lines.',
    risk: 'low', effort: 'S', where: 'generate.py:776-781',
    rec: 'a',
  },
  {
    id: 'F19', group: 'Code cleanup (same output)',
    title: 'align_words: remove fallbacks for a case that already raises',
    what: 'Interpolation handles "no matched word on either side", which the earlier no-matches check already makes impossible.',
    fix: 'Drop those branches from the two endpoint expressions.',
    gain: 'Two nested conditionals gone from the hardest expressions in the file.',
    risk: 'low', effort: 'S', where: 'generate.py:422-437',
    rec: 'a',
  },
  {
    id: 'F20', group: 'Code cleanup (same output)',
    title: 'run_config builds its result once for both exits',
    what: 'The verify-only and render exits repeat the same result dict; only "output" differs.',
    fix: 'Build it once before the verify branch; set output after the render.',
    gain: 'One place defines the CLI\'s JSON result.',
    risk: 'low', effort: 'S', where: 'generate.py:1166-1188',
    rec: 'a',
  },
  {
    id: 'F21', group: 'Code cleanup (same output)',
    title: 'align: functools.cache instead of a hand-written model singleton',
    what: 'A global, a None check and an assignment re-implement @cache.',
    fix: '@functools.cache on aligner(), imports stay lazy.',
    gain: '~5 lines.',
    risk: 'low', effort: 'S', where: 'align.py:338-353',
    rec: 'a',
  },
  {
    id: 'F22', group: 'Code cleanup (same output)',
    title: 'publish: itertools.groupby for shared tafsir paragraphs',
    what: 'A manual loop groups consecutive ayat that share the same tafsir text.',
    fix: 'groupby(quran.range(...), key=tafsir).',
    gain: '~6 lines. Captions byte-identical.',
    risk: 'low', effort: 'S', where: 'publish.py:156-169',
    rec: 'a',
  },
  {
    id: 'F23', group: 'Code cleanup (same output)',
    title: 'install.sh: one helper for the three identical venv-creation blocks',
    what: 'Render, ASR and align venvs repeat the same create-and-upgrade-pip block.',
    fix: 'One make_venv() shell function. Package installs stay separate.',
    gain: '~6–8 lines.',
    risk: 'low', effort: 'S', where: 'install.sh:81-85, 123-127, 157-161',
    rec: 'a',
  },
  // -------------------------------------------------------------- workflow
  {
    id: 'F24', group: 'Docs & workflow',
    title: 'make-post skill: reword the restarted-phrase (ibtidāʾ) instruction',
    what: '"It gets its own card: same words, separate group" reads as repeating the words in a second group. That breaks the word total; the code times the restart, it doesn\'t duplicate words.',
    fix: 'Say: keep each word once; make the restarted phrase its own group, which then spans both utterances.',
    gain: 'Saves a failed authoring round.',
    risk: 'low', effort: 'S', where: 'SKILL.md:67',
    rec: 'a',
  },
  {
    id: 'F25', group: 'Docs & workflow',
    title: 'The "one driver" for align → crop → generate → publish (todo.md)',
    what: 'Every stage already skips cached work (cached fetch 0.17s, align skip 0.002s, crop 0.09s). A driver mainly saves 2–3 commands, not compute, and must not skip your crop and translation review or repost.',
    fix: 'Either defer it and keep the todo, or build it with explicit review stops.',
    gain: 'Estimate: 2–3 fewer commands per reel.',
    risk: 'medium', effort: 'M', where: 'todo.md:8',
    options: [
      { id: 'a', label: 'Defer, keep it in todo.md' },
      { id: 'b', label: 'Build it now' },
    ],
    rec: 'a', why: 'Little time saved, and F7 should land first anyway.',
  },
]

const items: Item[] = findings.map((f) => ({
  kind: 'choice',
  id: f.id,
  group: f.group,
  title: f.title,
  options: f.options ?? DO,
  recommended: f.rec,
  why: f.why,
  detail: (
    <div className="grid gap-1.5">
      <p>{f.what}</p>
      <p>
        <span className="font-medium text-foreground/80">Fix: </span>
        {f.fix}
      </p>
      <p className="font-mono text-xs">
        <span className="text-foreground/80">{f.gain}</span>
        {f.measured && <span className="ml-2 uppercase tracking-[0.15em]">· measured</span>}
      </p>
      <p className="font-mono text-[11px] text-muted-foreground/80">
        risk {f.risk} · effort {f.effort} · {f.where}
      </p>
    </div>
  ),
}))

const stages: [string, string, number][] = [
  ['generate (render)', '8–72s', 100],
  ['align, fresh (3 configs)', '24.6s', 34],
  ['generate --verify-only', '1.7s', 2.4],
  ['crop (cached, --annotate)', '0.33s', 0.5],
  ['fetch (cached)', '0.17s', 0.3],
  ['align / transcribe (cached)', '<0.01s', 0.05],
]

export default function App() {
  return (
    <FeedbackProvider artifact="optimization-audit" title="Pipeline optimization audit" items={items}>
      <main className="mx-auto max-w-3xl px-6 py-12">
        <p className="font-mono text-xs uppercase tracking-[0.2em] text-muted-foreground">quran-clips · step 2 of 3</p>
        <h1 className="mt-3 text-3xl font-semibold tracking-tight">What the scouts found</h1>
        <p className="mt-4 text-sm leading-relaxed text-muted-foreground">
          9 GPT-6.1 Sol scouts went through every stage outside the render, plus a code-only pass over the renderers.
          I checked the main claims against the code and merged duplicates. 25 findings are left. Pick <em>Do it</em> or{' '}
          <em>Skip</em> on each, add notes where you want, then copy and paste back. Opus then makes one commit per
          approved item, each checked with the right test.
        </p>

        <section className="mt-10">
          <h2 className="border-b border-border pb-2 font-mono text-xs uppercase tracking-[0.2em] text-muted-foreground">
            Where time goes today (hujurat, cached)
          </h2>
          <div className="mt-3 grid gap-2">
            {stages.map(([name, t, pct]) => (
              <div key={name} className="grid grid-cols-[13rem_1fr_4.5rem] items-center gap-3 text-sm">
                <span>{name}</span>
                <span className="h-1.5 bg-muted">
                  <span className="block h-full bg-foreground/70" style={{ width: `${Math.max(pct, 0.6)}%` }} />
                </span>
                <span className="text-right font-mono text-xs tabular-nums">{t}</span>
              </div>
            ))}
          </div>
          <p className="mt-4 text-sm leading-relaxed text-muted-foreground">
            The other stages are already quick. The real speed wins are the split-check loop (F1, F2) and align
            (F3). Most of the rest is correctness (F7 is a real bug that gives wrong output without any error)
            and code cleanup.
          </p>
        </section>

        <FeedbackList />
        <IdeasBox />
      </main>
      <CopyBar />
    </FeedbackProvider>
  )
}
