import stillToday from 'data-url:./assets/still-v2p-t2.webp'
import zoomToday from 'data-url:./assets/zoom-v2p-t2.webp'
import gpuStill053 from 'data-url:./assets/gpu-sbs-0p53.webp'
import gpuStill200 from 'data-url:./assets/gpu-sbs-2p00.webp'
import gpuStill1000 from 'data-url:./assets/gpu-sbs-10p00.webp'
import gpuZoom from 'data-url:./assets/gpu-zoom-2p00.webp'

const ACCENT = 'oklch(0.66 0.17 40)'

const cpuShare = [
  { name: 'Heat wave', pct: 34 },
  { name: 'Base picture + captions', pct: 18 },
  { name: 'Bar glow', pct: 14 },
  { name: 'Wipe mask', pct: 9 },
  { name: 'Text glow', pct: 9 },
  { name: 'Glow', pct: 8 },
  { name: 'Scan', pct: 7 },
  { name: 'Encode (x264)', pct: 5 },
  { name: 'Snow', pct: 0 },
]

const trials: { branch: string; change: string; time: string; speed: string; quality: string }[] = [
  {
    branch: 'exp/cheap-wins',
    change: 'Same pixels: wipe mask over the bar box only, static images decoded once, scan tint folded, faster snow bake, cached trim',
    time: '195 s → 175 s (26 s reel)',
    speed: '1.1x',
    quality: 'identical frames',
  },
  {
    branch: '(no code)',
    change: 'Render two reels at once',
    time: '128 s → 109 s for two',
    speed: '1.15x throughput',
    quality: 'identical',
  },
  {
    branch: 'exp/lowres V1',
    change: 'Heat at 1x instead of 2x',
    time: '317 → 300 s',
    speed: '1.06x',
    quality: '44.8 dB',
  },
  {
    branch: 'exp/lowres V4',
    change: 'Whole look at 1440x810',
    time: '317 → 172 s',
    speed: '1.8x',
    quality: '43.1 dB (footage 50.2)',
  },
  {
    branch: 'exp/lowres V2',
    change: 'Whole look at 1080x608 (the size the vertical file shows)',
    time: '317 → 107 s',
    speed: '3.0x',
    quality: '41.0 dB (footage 46.4). Looks the same by eye; text edges slightly softer',
  },
  {
    branch: 'exp/lowres V3',
    change: 'V2 + heat at 1x',
    time: '317 → 99 s',
    speed: '3.2x',
    quality: '36.6 dB. Visibly different, rejected',
  },
  {
    branch: 'exp/gpu-proto',
    change: 'Core Image (GPU) prototype of the full look, approximate, VideoToolbox encode, 20 s clip',
    time: '134 s → 26 s',
    speed: '~5x (≈9x at 1080x608, render only)',
    quality: 'Approximate look, not matched',
  },
  {
    branch: 'exp/gpu-proto',
    change: 'Same, encoded with x264 instead',
    time: '134 s → 50 s',
    speed: '~2.7x',
    quality: 'Encode becomes the bottleneck',
  },
]

const gpuNumbers: { name: string; value: string; note: string }[] = [
  {
    name: 'Band PSNR, luma',
    value: '21.2 dB',
    note: '1080x608 picture band over the 26.1 s overlap (average of all planes 22.9 dB). Approximate port, so far below the lowres variants',
  },
  {
    name: 'Band SSIM, luma',
    value: '0.830',
    note: '1 is identical (all planes 0.842)',
  },
  {
    name: 'Render time',
    value: '30 s',
    note: 'vs 195 s (quiet) to 317 s (under load) today for this reel, so 6–11x. GPU time is render and VideoToolbox encode only: no audio, no loudnorm',
  },
]

const sectionClass = 'border-t border-border py-10'
const labelClass = 'font-mono text-xs uppercase tracking-[0.2em] text-muted-foreground'
const h2Class = 'mt-2 text-xl font-semibold tracking-tight'

export default function App() {
  return (
    <main className="min-h-screen bg-background px-6 py-16 text-foreground">
      <div className="mx-auto max-w-4xl">
        <header className="pb-2">
          <p className={labelClass}>bars · render speed</p>
          <h1 className="mt-3 text-4xl font-semibold tracking-tight">
            Why a bars vertical reel takes 5 minutes, and how to fix it
          </h1>
          <p className="mt-4 max-w-2xl text-base leading-relaxed text-muted-foreground">
            The look runs ~20 full-frame image passes per frame on the CPU, one at 4K, at a size the
            vertical file then throws away. The iPad app does the same passes on the GPU.
          </p>
        </header>

        <section className={sectionClass}>
          <p className={labelClass}>01</p>
          <h2 className={h2Class}>Where the time goes</h2>
          <p className="mt-2 max-w-2xl text-sm leading-relaxed text-muted-foreground">
            CPU share of the full bars stack, measured on this iMac (Apple M1, 8 cores) by turning
            each effect off one at a time.
          </p>
          <div className="mt-6 space-y-2">
            {cpuShare.map((row, i) => (
              <div key={row.name} className="grid grid-cols-[14rem_1fr_3.5rem] items-center gap-4">
                <span className="text-sm">{row.name}</span>
                <div className="h-4 bg-muted">
                  <div
                    className="h-4"
                    style={{
                      width: `${(row.pct / 34) * 100}%`,
                      background: i === 0 ? ACCENT : 'var(--muted-foreground)',
                    }}
                  />
                </div>
                <span className="text-right font-mono text-sm tabular-nums">
                  {row.pct === 0 ? '~0%' : `${row.pct}%`}
                </span>
              </div>
            ))}
          </div>
          <p className="mt-6 max-w-2xl text-sm leading-relaxed text-muted-foreground">
            All effects off: 24 frames/s. All on: 4.2 frames/s. A 45 s reel ≈ 300 s. Only ~5 of 8
            cores are busy; ffmpeg thread settings changed nothing.
          </p>
        </section>

        <section className={sectionClass}>
          <p className={labelClass}>02</p>
          <h2 className={h2Class}>What we're doing wrong</h2>
          <ol className="mt-6 max-w-2xl list-decimal space-y-4 pl-5 text-sm leading-relaxed">
            <li>
              <span className="font-medium">Everything is CPU.</span> This ffmpeg build has no GPU
              filters for blur, blend or displace (only <code className="font-mono">scale_vt</code>
              ). Each effect is a separate pass that reads and writes a whole frame in memory.
            </li>
            <li>
              <span className="font-medium">We render big, then shrink.</span> The whole stack runs
              at 1920x1080, then the vertical file scales it to 1080x608. About two thirds of the
              pixel work never reaches the video.
            </li>
            <li>
              <span className="font-medium">Heat runs at 4K.</span> It upscales the frame and both
              noise maps to 3840x2160 for every frame.
            </li>
            <li>
              <span className="font-medium">Small waste.</span> The wipe mask is computed over the
              full frame for the whole reel, although it only matters for one card's box. Snow is
              re-baked for almost every reel, because its cache key includes the bar colour.
            </li>
          </ol>
        </section>

        <section className={sectionClass}>
          <p className={labelClass}>03</p>
          <h2 className={h2Class}>Why the iPad is faster</h2>
          <ul className="mt-6 max-w-2xl list-disc space-y-3 pl-5 text-sm leading-relaxed">
            <li>
              Effects are GPU shaders. A 1080p blur takes well under a millisecond (a shader is a
              small program that runs on every pixel at once).
            </li>
            <li>Frames stay on the GPU between effects, so nothing is copied back to memory.</li>
            <li>A separate video chip does the encode, so the CPU stays free.</li>
          </ul>
        </section>

        <section className={sectionClass}>
          <p className={labelClass}>04</p>
          <h2 className={h2Class}>What we tried</h2>
          <div className="mt-6 overflow-x-auto">
            <table className="w-full min-w-[44rem] border-collapse text-left text-sm">
              <thead>
                <tr className="border-b border-border font-mono text-xs uppercase tracking-[0.12em] text-muted-foreground">
                  <th className="py-2 pr-4 font-normal">Branch</th>
                  <th className="py-2 pr-4 font-normal">What</th>
                  <th className="py-2 pr-4 font-normal">Wall time</th>
                  <th className="py-2 pr-4 font-normal">Speed-up</th>
                  <th className="py-2 font-normal">Quality</th>
                </tr>
              </thead>
              <tbody>
                {trials.map((t, i) => (
                  <tr key={i} className="border-b border-border align-top">
                    <td className="py-3 pr-4 font-mono text-xs">{t.branch}</td>
                    <td className="py-3 pr-4 leading-relaxed">{t.change}</td>
                    <td className="py-3 pr-4 font-mono text-xs tabular-nums whitespace-nowrap">
                      {t.time}
                    </td>
                    <td className="py-3 pr-4 font-mono text-xs tabular-nums whitespace-nowrap">
                      {t.speed}
                    </td>
                    <td className="py-3 leading-relaxed">{t.quality}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="mt-4 max-w-2xl text-xs leading-relaxed text-muted-foreground">
            dB = PSNR against today's output (a score for how close two images are; higher is
            closer). The repo calls ≥45 dB "same look". Timings were taken while other tests ran,
            so trust ratios more than seconds.
          </p>
        </section>

        <section className={sectionClass}>
          <p className={labelClass}>05</p>
          <h2 className={h2Class}>Would a Rust rewrite help?</h2>
          <p className="mt-4 max-w-2xl text-sm leading-relaxed">
            No. Python only writes the filter instructions and starts ffmpeg. 99% of the time is
            inside ffmpeg's C code, which is already compiled and optimised. Rust would run the same
            filters at the same speed. Speed comes from <span className="font-medium">where</span>{' '}
            pixels are processed (GPU) and <span className="font-medium">how many</span> (render at
            the delivered size). Either can be done from Python or a small Swift helper.
          </p>
        </section>

        <section className={sectionClass}>
          <p className={labelClass}>06</p>
          <h2 className={h2Class}>Recommended path</h2>
          <ol className="mt-6 max-w-2xl list-decimal space-y-4 pl-5 text-sm leading-relaxed">
            <li>
              <span className="font-medium">Step 1: land the same-pixel fixes.</span> Effort: hours.
              Gain: ~10%, plus ~15% more when several reels render at once. Needs a golden re-bless,
              because the filter text changes, not the pixels.
            </li>
            <li>
              <span className="font-medium">Step 2: render vertical reels at 1080x608.</span> Effort:
              a day. Gain: ~3x. A look decision, so compare the stills first.
            </li>
            <li>
              <span className="font-medium">Step 3: GPU renderer in Swift + Core Image/Metal.</span>{' '}
              Effort: 1–2 weeks approximate, 3–5 weeks to match today's look within 45 dB. Gain:
              5–10x. It changes how the look is verified (today's golden is an ffmpeg string), and
              GPU output can differ between Macs.
            </li>
          </ol>
          <p className="mt-6 max-w-2xl text-sm leading-relaxed">
            Steps 1+2 together: a 45 s reel from ~300 s to roughly 90 s. With step 3: roughly 20–40
            s.
          </p>
        </section>

        <section className={sectionClass}>
          <p className={labelClass}>07</p>
          <h2 className={h2Class}>Side by side</h2>
          <div className="mt-6 grid gap-8">
            <figure>
              <img
                src={stillToday}
                alt="Card up at 2 s, today's output on the left and 1080x608 on the right"
                className="w-full border border-border"
              />
              <figcaption className="mt-2 font-mono text-xs text-muted-foreground">
                Card up at 2 s — left today, right 1080x608
              </figcaption>
            </figure>
            <figure>
              <img
                src={zoomToday}
                alt="400% crop of a glyph edge"
                className="w-full border border-border"
              />
              <figcaption className="mt-2 font-mono text-xs text-muted-foreground">
                400% crop of a glyph edge
              </figcaption>
            </figure>
          </div>
        </section>

        <section className={sectionClass}>
          <p className={labelClass}>08</p>
          <h2 className={h2Class}>GPU prototype vs today</h2>
          <p className="mt-2 max-w-2xl text-sm leading-relaxed text-muted-foreground">
            The same 26 s reel: same trim, crop, caption boxes and timings, pill #AD7047. Left
            today, right the Core Image prototype (exp/gpu-proto).
          </p>
          <div className="mt-6 grid gap-8">
            <figure>
              <img
                src={gpuStill053}
                alt="Card wiping in at 0.53 s, today on the left and the GPU prototype on the right"
                className="w-full border border-border"
              />
              <figcaption className="mt-2 font-mono text-xs text-muted-foreground">
                0.53 s, wipe in — left today, right GPU
              </figcaption>
            </figure>
            <figure>
              <img
                src={gpuStill200}
                alt="Card 1 fully up at 2.00 s, today on the left and the GPU prototype on the right"
                className="w-full border border-border"
              />
              <figcaption className="mt-2 font-mono text-xs text-muted-foreground">
                2.00 s, card 1 up — left today, right GPU
              </figcaption>
            </figure>
            <figure>
              <img
                src={gpuStill1000}
                alt="Card 2 up at 10.00 s, today on the left and the GPU prototype on the right"
                className="w-full border border-border"
              />
              <figcaption className="mt-2 font-mono text-xs text-muted-foreground">
                10.00 s, card 2 up — left today, right GPU
              </figcaption>
            </figure>
            <figure>
              <img
                src={gpuZoom}
                alt="400% crop of a glyph edge at 2 s, today on the left and the GPU prototype on the right"
                className="w-full border border-border"
              />
              <figcaption className="mt-2 font-mono text-xs text-muted-foreground">
                400% crop of a glyph edge at 2.00 s — left today, right GPU
              </figcaption>
            </figure>
          </div>
          <dl className="mt-8 max-w-2xl divide-y divide-border text-sm">
            {gpuNumbers.map((row) => (
              <div key={row.name} className="grid grid-cols-[9rem_6rem_1fr] gap-4 py-3">
                <dt className="font-medium">{row.name}</dt>
                <dd className="font-mono text-xs tabular-nums whitespace-nowrap">{row.value}</dd>
                <dd className="leading-relaxed text-muted-foreground">{row.note}</dd>
              </div>
            ))}
          </dl>
          <p className="mt-6 max-w-2xl text-sm leading-relaxed">
            Layout, caption placement and the wipe direction match. By eye the prototype is darker
            with a cooler shadow, its caption glow is whiter and wider, and the snow flecks are
            fainter.
          </p>
        </section>

        <footer className="border-t border-border pt-6">
          <p className="font-mono text-xs leading-relaxed text-muted-foreground">
            Worktrees: /Users/yusuf/dev/qc-wt-cheap · qc-wt-lowres · qc-wt-gpu — nothing merged into
            main.
          </p>
        </footer>
      </div>
    </main>
  )
}
