const folders = [
  {
    path: '~/dev/quran-clips',
    kind: 'Main checkout',
    branch: 'main @ 4003032',
    verdict: 'Keep',
    note: 'The real repo. Owns the three Python environments under tools/ (render, asr, align). The other two folders share its git history.',
  },
  {
    path: '~/dev/qc-wt-gpubars',
    kind: 'Worktree',
    branch: 'gpu-render @ 9640df8',
    verdict: 'Keep',
    note: 'The GPU renderer work: 27 commits ahead of main, 0 behind. Its tools/*-venv are symlinks to the main checkout, so it has the same broken Whisper.',
  },
  {
    path: '~/dev/quran-clips-torch-nightly',
    kind: 'Worktree (detached)',
    branch: 'bfdfe77, no branch',
    verdict: 'Safe to remove',
    note: 'Leftover from the Oct 23 fix that let torch install on Python 3.15. That commit is already in main, nothing here is uncommitted, and it holds its own 1 GB copy of the align environment.',
  },
]

const timeline = [
  ['Sep 28 – Oct 23', 'Whisper (mlx) transcribes 8 sources without trouble.'],
  ['Oct 23 09:51', 'Last good transcript: gJUqxFxusbk/whisper.json.'],
  ['Oct 23 09:53', 'All three environments rebuilt on Python 3.15. The Whisper install fails, so tools/asr-venv ends up holding only pip.'],
  ['Oct 23 22:14', 'Commit 2037f2d adds the nightly-torch fallback, but only to the align environment. The Whisper one is left as it was.'],
  ['Oct 26 11:18', 'Session "Video clip posts": install.sh fails on mlx-whisper, so the abdulaziz 39:67–68 reel was made without a transcript and without the repeat check.'],
]

const traces = [
  {
    label: 'The Whisper environment is empty',
    cmd: 'tools/asr-venv/bin/python -m pip list',
    out: 'Package Version\n------- -------\npip     26.2.2',
  },
  {
    label: 'So transcribe.py cannot start',
    cmd: 'tools/asr-venv/bin/python -c "import mlx_whisper"',
    out: "ModuleNotFoundError: No module named 'mlx_whisper'",
  },
  {
    label: 'Why the install fails (fresh Python 3.15 env)',
    cmd: 'pip install --dry-run mlx-whisper',
    out: 'mlx-0.32.4-cp315 ✓   numba-0.68.0-cp315 ✓   numpy-2.5.4-cp315 ✓\n\nThe conflict is caused by:\n    mlx-whisper 0.4.3 depends on torch\n…no matching distributions available for your environment:\n    torch\nERROR: ResolutionImpossible',
  },
  {
    label: 'mlx-whisper never uses torch when transcribing',
    cmd: 'grep -l "import torch" mlx_whisper/*.py',
    out: 'torch_whisper.py   ← only the model-conversion helper.\n                     transcribe.py, load_models.py and __init__.py never import it.',
  },
]

const fixDiff = `     "$APY" -c "import $ASR_MOD" 2>/dev/null || {
         say "  installing $ASR_PKG (first transcription will also download the model)"
-        "$APY" -m pip -q install "$ASR_PKG"
+        case "$ASR_BACKEND" in
+            # mlx-whisper lists torch, but only its checkpoint converter imports
+            # it, and torch has no Python 3.15 wheel on PyPI (2026-10).
+            mlx) "$APY" -m pip -q install --no-deps mlx-whisper &&
+                 "$APY" -m pip -q install mlx numba numpy tqdm more-itertools \\
+                     tiktoken huggingface_hub scipy;;
+            *)   "$APY" -m pip -q install "$ASR_PKG";;
+        esac
     }`

const testRows = [
  ['Words', '175', '175'],
  ['Segments', '24', '24'],
  ['Same word text', '—', '175 / 175'],
  ['Largest timing shift', '—', '0.02 s'],
  ['Average timing shift', '—', '0.0005 s'],
]

function Label({ children }: { children: React.ReactNode }) {
  return (
    <p className="font-mono text-xs uppercase tracking-[0.2em] text-muted-foreground">
      {children}
    </p>
  )
}

function Section({ n, title, children }: { n: string; title: string; children: React.ReactNode }) {
  return (
    <section className="border-b border-border py-10">
      <Label>{n}</Label>
      <h2 className="mt-2 text-xl font-semibold tracking-tight">{title}</h2>
      <div className="mt-5">{children}</div>
    </section>
  )
}

export default function App() {
  return (
    <main className="min-h-screen bg-background px-6 pb-20 pt-14 text-foreground">
      <div className="max-w-3xl">
        <Label>quran-clips · Oct 26 2026</Label>
        <h1 className="mt-3 text-3xl font-semibold tracking-tight">
          Whisper is broken because its install fails on Python 3.15
        </h1>
        <p className="mt-4 max-w-2xl text-[15px] leading-relaxed text-muted-foreground">
          Whisper itself is fine. Since Oct 23 the environment that runs it is empty, because{' '}
          <code className="font-mono text-foreground">mlx-whisper</code> lists torch as a
          requirement and torch has no Python 3.15 build on PyPI. mlx-whisper doesn't actually
          use torch to transcribe, so the fix is to install it without that one requirement. I
          tested this and the output matches the old transcript.
        </p>

        <div className="mt-8 grid grid-cols-1 gap-px overflow-hidden rounded-sm border border-border bg-border sm:grid-cols-3">
          {[
            ['Broken since', 'Oct 23, 09:53'],
            ['Sources missing a transcript', '2'],
            ['Tested fix vs old output', '175/175 words'],
          ].map(([k, v]) => (
            <div key={k} className="bg-background p-4">
              <p className="text-xs text-muted-foreground">{k}</p>
              <p className="mt-1 font-mono text-lg tabular-nums">{v}</p>
            </div>
          ))}
        </div>

        <Section n="01" title="The three folders">
          <div className="divide-y divide-border border-y border-border">
            {folders.map((f) => (
              <div key={f.path} className="grid gap-2 py-4 sm:grid-cols-[230px_1fr]">
                <div>
                  <p className="font-mono text-sm">{f.path}</p>
                  <p className="mt-1 text-xs text-muted-foreground">{f.kind}</p>
                  <p className="font-mono text-xs text-muted-foreground">{f.branch}</p>
                </div>
                <div>
                  <span
                    className={
                      'inline-block rounded-sm px-1.5 py-0.5 font-mono text-[11px] uppercase tracking-wider ' +
                      (f.verdict === 'Keep'
                        ? 'bg-muted text-foreground'
                        : 'bg-amber-100 text-amber-900')
                    }
                  >
                    {f.verdict}
                  </span>
                  <p className="mt-2 text-sm leading-relaxed">{f.note}</p>
                </div>
              </div>
            ))}
          </div>
        </Section>

        <Section n="02" title="What happened">
          <ol className="relative ml-1 border-l border-border">
            {timeline.map(([when, what], i) => (
              <li key={when} className="relative pb-5 pl-6 last:pb-0">
                <span
                  className={
                    'absolute -left-[5px] top-1.5 h-2.5 w-2.5 rounded-full ' +
                    (i >= 2 ? 'bg-red-600' : 'bg-foreground/40')
                  }
                />
                <p className="font-mono text-xs tabular-nums text-muted-foreground">{when}</p>
                <p className="mt-0.5 text-sm leading-relaxed">{what}</p>
              </li>
            ))}
          </ol>
        </Section>

        <Section n="03" title="Evidence">
          <div className="space-y-6">
            {traces.map((t) => (
              <div key={t.label}>
                <p className="text-sm font-medium">{t.label}</p>
                <pre className="mt-2 overflow-x-auto rounded-sm bg-zinc-950 p-4 font-mono text-[12.5px] leading-relaxed text-zinc-300">
                  <span className="text-zinc-500">$ </span>
                  <span className="text-zinc-100">{t.cmd}</span>
                  {'\n'}
                  {t.out}
                </pre>
              </div>
            ))}
          </div>
        </Section>

        <Section n="04" title="The fix: one change in install.sh">
          <p className="mb-4 text-sm leading-relaxed text-muted-foreground">
            Install mlx-whisper without its requirement list, then install everything it needs
            except torch. All of those have Python 3.15 builds. Nothing in{' '}
            <code className="font-mono">transcribe.py</code> changes.
          </p>
          <pre className="overflow-x-auto rounded-sm bg-zinc-950 p-4 font-mono text-[12.5px] leading-relaxed">
            {fixDiff.split('\n').map((line, i) => (
              <div
                key={i}
                className={
                  line.startsWith('+')
                    ? 'text-emerald-400'
                    : line.startsWith('-')
                      ? 'text-red-400'
                      : 'text-zinc-400'
                }
              >
                {line}
              </div>
            ))}
          </pre>

          <h3 className="mt-8 text-sm font-medium">Tested on OiSKzvnUxbs (4:46 of audio)</h3>
          <p className="mt-1 text-sm text-muted-foreground">
            I built a scratch Python 3.15 environment with this recipe (outside the repo) and ran
            transcribe.py into a temp folder. The real whisper.json was not touched. Took 1 min 49 s,
            including the first-run setup.
          </p>
          <table className="mt-4 w-full max-w-md border-collapse text-sm">
            <thead>
              <tr className="border-b border-border text-left text-xs text-muted-foreground">
                <th className="py-2 font-normal"></th>
                <th className="py-2 text-right font-normal">Oct 21 (old)</th>
                <th className="py-2 text-right font-normal">Fixed install</th>
              </tr>
            </thead>
            <tbody>
              {testRows.map(([k, a, b]) => (
                <tr key={k} className="border-b border-border">
                  <td className="py-2">{k}</td>
                  <td className="py-2 text-right font-mono tabular-nums text-muted-foreground">{a}</td>
                  <td className="py-2 text-right font-mono tabular-nums">{b}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Section>

        <Section n="05" title="Options I did not pick">
          <ul className="space-y-3 text-sm leading-relaxed">
            <li>
              <span className="font-medium">Install nightly torch, as the align environment does.</span>{' '}
              <span className="text-muted-foreground">Works, but adds about 1 GB that Whisper never loads.</span>
            </li>
            <li>
              <span className="font-medium">Switch to the faster-whisper backend.</span>{' '}
              <span className="text-muted-foreground">
                Its word timings are different, so every existing reel cut from a transcript would get re-timed.
              </span>
            </li>
            <li>
              <span className="font-medium">Build the Whisper environment on an older Python.</span>{' '}
              <span className="text-muted-foreground">Needs a second Python install to manage. Not worth it.</span>
            </li>
          </ul>
        </Section>

        <Section n="06" title="What to clean up after the fix">
          <ul className="list-disc space-y-2 pl-5 text-sm leading-relaxed">
            <li>
              Run <code className="font-mono">./install.sh</code> in{' '}
              <code className="font-mono">~/dev/quran-clips</code>. The worktree picks it up through its
              symlink.
            </li>
            <li>
              Transcribe <code className="font-mono">VNCFBt8VYjc</code>, then re-run align on the
              abdulaziz 39:67–68 reel so its repeat check finally runs.
            </li>
            <li>
              The main checkout has two stray source folders from that session (
              <code className="font-mono">VNCFBt8VYjc</code>,{' '}
              <code className="font-mono">abdulaziz-photo-VNCFBt8VYjc</code>). The real ones are in the
              gpu-render worktree.
            </li>
            <li>
              Remove the <code className="font-mono">quran-clips-torch-nightly</code> worktree.
            </li>
            <li>
              Separate issue from the same session: crop.py could not run because the worktree's{' '}
              <code className="font-mono">.env</code> has no <code className="font-mono">QC_VISION_API_KEY</code>.
              The main checkout's <code className="font-mono">.env</code> has one, so copy it across.
            </li>
          </ul>
        </Section>
      </div>
    </main>
  )
}
