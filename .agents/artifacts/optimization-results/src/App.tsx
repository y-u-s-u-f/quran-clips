const ACCENT = 'oklch(0.66 0.17 40)'

type Speed = { what: string; before: string; after: string; b: number; a: number; note?: string }

const speed: Speed[] = [
  { what: 'Split check, hujurat vertical (--verify-only)', before: '1.83s', after: '0.36s', b: 1.83, a: 0.36, note: 'preview cut now cached; the three hujurat styles share one' },
  { what: 'Split check, ahzab', before: '2.36s', after: '0.35s', b: 2.36, a: 0.35 },
  { what: 'Wrong group word total, time to error', before: '1.47s', after: '<0.01s', b: 1.47, a: 0.004, note: 'now fails at load: before align, crop or any cut' },
  { what: 'Missing / stale render_gpu, time to error', before: '15.4s', after: '<0.01s', b: 15.4, a: 0.004, note: 'checked before the trim and audio work' },
  { what: 'align.py, three styles off one window', before: '19.6s', after: '14.5s', b: 19.6, a: 14.5, note: '1 MMS inference instead of 3, identical sidecars; measured with other agents running' },
  { what: 'fetch.py on a cached source', before: '481ms', after: '207ms', b: 0.481, a: 0.207, note: 'one ffprobe instead of three (hajri; ahzab 269 → 136ms)' },
]

const fixes: [string, string][] = [
  ['Stale alignment accepted', 'Change a reel to another verse with the same word count and the old timings were used without a word. Tested: 49:4-5 timings were accepted for 2:142. generate.py now refuses, and align.py re-aligns on its own.'],
  ['align.py edited a config it then rejected', 'An identified span was written into the yaml before the group totals were checked. It now checks first (found in review, with a regression test).'],
  ['Live post reported as failed', 'If the permalink lookup failed after a post went live, publish.py counted it as a failure and skipped the Finder tag (found in review).'],
  ['Cover error could stop a post', 'A Facebook thumbnail error ended the upload, and a cover cleanup error could leave an uploaded Instagram container unpublished (found in review).'],
  ['Draft reported as live', 'A partial --draft run said "went out to" (found in review).'],
  ['Parity test could pass with no golden', 'graph_parity.py wrote a missing golden and passed. It now fails unless you pass --bless.'],
  ['Empty-shot crop showed an old image', 'crop.py --annotate wrote nothing when there was no reciter, so /tmp/c.png could be the last reel. It now always writes, to /tmp/<reel>-crop.png.'],
  ['install.sh checked the wrong ffmpeg', 'It ignored QC_FFMPEG / QC_FFPROBE / QC_YT_DLP. It also wrote bytecode during --check (found in review).'],
]

const cleanup = [
  'One hard-shadow helper for both renderers (bars no longer runs an affine transform for an integer shift)',
  'One part-file helper for the trim, heat and snow caches',
  'functools.cache for the aligner load and for character advances',
  'itertools.groupby for shared tafsir paragraphs',
  'norm_ar in place of strip_tashkeel',
  'quran._index: unused 5 MB list and a dedupe set that could never fire, removed',
  'Code that can never run removed from align_words, en_tokens and silence clipping',
  'render_gpu.swift: two duplicate Metal kernels and three write-only fields removed',
  'install.sh: one venv-creation helper; one first-fit in wrap_parity.py',
  'SKILL.md: ibtidāʾ instruction matches the code; README quickstart has the decode check',
]

const checks: [string, string][] = [
  ['gpu_parity', '12 / 12 cases, every frame identical'],
  ['input_parity', '14 / 14'],
  ['graph_parity', '4 / 4 goldens'],
  ['wrap_parity', '11 797 cards + 677 solver-only'],
  ['config_checks', 'new, passes'],
  ['--verify-only vs main', '4 / 4 configs byte-identical'],
  ['Full renders vs main', '4 / 4 reels: video + audio framemd5 identical'],
  ['install.sh --check', 'clean, read-only'],
]

function Label({ children }: { children: React.ReactNode }) {
  return (
    <h2 className="border-b border-border pb-2 font-mono text-xs uppercase tracking-[0.2em] text-muted-foreground">
      {children}
    </h2>
  )
}

export default function App() {
  return (
    <main className="min-h-screen bg-background text-foreground">
      <div className="mx-auto max-w-3xl px-6 py-12">
        <p className="font-mono text-xs uppercase tracking-[0.2em] text-muted-foreground">quran-clips · step 3 of 3 · merged</p>
        <h1 className="mt-3 text-3xl font-semibold tracking-tight">Pipeline audit: what changed</h1>
        <p className="mt-4 max-w-2xl text-sm leading-relaxed text-muted-foreground">
          PR #4 is merged into main (<span className="font-mono">e90c148</span>). GPT-6.1 Sol scouts found the issues and
          Opus agents fixed them, one commit per item. Two Sol reviewers then checked the PR and fixed what they found.
          Reels come out exactly as before: every test reel matches main frame for frame.
        </p>

        <div className="mt-8 grid grid-cols-2 gap-px border border-border bg-border sm:grid-cols-4">
          {[
            ['40', 'commits'],
            ['21', 'files'],
            ['8', 'bugs fixed'],
            ['4 / 4', 'reels identical'],
          ].map(([n, l]) => (
            <div key={l} className="bg-background px-4 py-3">
              <div className="font-mono text-2xl tabular-nums">{n}</div>
              <div className="text-xs text-muted-foreground">{l}</div>
            </div>
          ))}
        </div>

        <section className="mt-12">
          <Label>Faster</Label>
          <div className="mt-2">
            {speed.map((s) => (
              <div key={s.what} className="grid gap-2 border-b border-border py-3 sm:grid-cols-[1fr_14rem]">
                <div>
                  <div className="text-sm">{s.what}</div>
                  {s.note && <div className="text-xs text-muted-foreground">{s.note}</div>}
                </div>
                <div className="grid gap-1">
                  <div className="flex items-center gap-2">
                    <span className="h-1.5 bg-muted-foreground/40" style={{ width: '100%' }} />
                    <span className="w-14 shrink-0 text-right font-mono text-xs tabular-nums text-muted-foreground">{s.before}</span>
                  </div>
                  <div className="flex items-center gap-2">
                    <span className="h-1.5" style={{ width: `${Math.max((s.a / s.b) * 100, 1.5)}%`, background: ACCENT }} />
                    <span className="ml-auto w-14 shrink-0 text-right font-mono text-xs tabular-nums">{s.after}</span>
                  </div>
                </div>
              </div>
            ))}
          </div>
          <p className="mt-3 text-xs leading-relaxed text-muted-foreground">
            Also faster: publish.py posts to Facebook while Instagram processes its upload, with no threads (not timed,
            since nothing was posted). install.sh imports each package once on a rerun instead of twice.
          </p>
        </section>

        <section className="mt-12">
          <Label>Bugs fixed</Label>
          <dl className="mt-2">
            {fixes.map(([t, d]) => (
              <div key={t} className="grid gap-1 border-b border-border py-3 sm:grid-cols-[13rem_1fr] sm:gap-4">
                <dt className="text-sm font-medium">{t}</dt>
                <dd className="text-sm leading-relaxed text-muted-foreground">{d}</dd>
              </div>
            ))}
          </dl>
        </section>

        <section className="mt-12">
          <Label>Cleaner, same output</Label>
          <ul className="mt-3 grid gap-1.5 text-sm leading-relaxed">
            {cleanup.map((c) => (
              <li key={c} className="flex gap-3">
                <span className="mt-2 h-1 w-1 shrink-0 bg-foreground/60" />
                {c}
              </li>
            ))}
          </ul>
        </section>

        <section className="mt-12">
          <Label>How it was checked</Label>
          <table className="mt-2 w-full text-sm">
            <tbody>
              {checks.map(([k, v]) => (
                <tr key={k} className="border-b border-border">
                  <td className="py-2 pr-4 font-mono text-xs">{k}</td>
                  <td className="py-2 text-right text-muted-foreground">{v}</td>
                  <td className="w-6 py-2 text-right font-mono text-xs" style={{ color: ACCENT }}>
                    ✓
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>

        <section className="mt-12">
          <Label>Changes you'll notice</Label>
          <ul className="mt-3 grid gap-2 text-sm leading-relaxed text-muted-foreground">
            <li>
              <span className="text-foreground">Crop annotations</span> go to <span className="font-mono">/tmp/&lt;reel&gt;-crop.png</span>{' '}
              instead of <span className="font-mono">/tmp/c.png</span>.
            </li>
            <li>
              <span className="text-foreground">Publishing:</span> if Instagram fails while uploading, Facebook still posts. It still
              says which platform went out and exits with an error.
            </li>
            <li>
              <span className="text-foreground">Alignment:</span> after you change a reel's verses, align.py re-aligns on its own and
              generate.py refuses the old file instead of using it.
            </li>
            <li>
              <span className="text-foreground">Group counts</span> that don't add up now fail as soon as any stage loads the config.
            </li>
          </ul>
        </section>

        <section className="mt-12">
          <Label>Left open</Label>
          <ul className="mt-3 grid gap-2 text-sm leading-relaxed text-muted-foreground">
            <li>
              <span className="text-foreground">VideoToolbox under heavy load.</span> While many agents were rendering at once, two
              renders of the same reel came out different (vertical 44.9 dB, horizontal 54.4 dB). On a quiet machine, repeat
              renders and main vs this branch were frame-identical. AGENTS.md says renders are deterministic run to run, which held
              only on a quiet machine. Worth a look if you ever render in parallel.
            </li>
            <li>
              <span className="text-foreground">The "one driver" in todo.md</span> stays deferred, as decided.
            </li>
            <li>
              <span className="text-foreground">Small leftover:</span> load_config doesn't close a file it opens (a pre-existing
              ResourceWarning), left out of scope.
            </li>
          </ul>
        </section>

        <p className="mt-12 border-t border-border pt-4 font-mono text-xs text-muted-foreground">
          github.com/y-u-s-u-f/quran-clips/pull/4 · local main up to date · tools/render_gpu rebuilt · temporary worktrees and branches removed
        </p>
      </div>
    </main>
  )
}
