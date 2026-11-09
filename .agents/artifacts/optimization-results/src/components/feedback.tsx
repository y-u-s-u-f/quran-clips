// Feedback kit: questions, picks, notes and ideas the user answers on the page,
// then copies back to the agent as one Markdown block. Answers are saved in the
// browser per artifact, so they survive reloads and re-uploads of the same draft.
//
//   <FeedbackProvider artifact="pigeon-roadmap" title="Pigeon roadmap" items={items}>
//     <FeedbackList />          or <FeedbackItem id="Q1" /> placed anywhere
//     <IdeasBox />
//     <CopyBar />
//   </FeedbackProvider>
import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from 'react'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { RadioGroup, RadioGroupItem } from '@/components/ui/radio-group'
import { Textarea } from '@/components/ui/textarea'

export type Option = { id: string; label: string; detail?: string }

type Base = { id: string; title: string; detail?: ReactNode; group?: string }

/** One answer from a list (or several with `multiple`), plus an "Other" field unless `other: false`. */
export type ChoiceItem = Base & {
  kind: 'choice'
  options: Option[]
  recommended?: string | string[]
  why?: string
  multiple?: boolean
  other?: boolean
}
/** Something the user can select or leave alone, like a feature in a backlog. */
export type PickItem = Base & { kind: 'pick' }
/** An open question answered in the user's own words. */
export type TextItem = Base & { kind: 'text'; placeholder?: string }
export type Item = ChoiceItem | PickItem | TextItem

type Answer = { choice?: string[]; other?: string; picked?: boolean; text?: string; note?: string }
type State = { answers: Record<string, Answer>; ideas: string[]; note: string }

const EMPTY: State = { answers: {}, ideas: [], note: '' }
const OTHER = '__other'

const recommendedOf = (item: ChoiceItem) => (item.recommended == null ? [] : [item.recommended].flat())
const oneLine = (t: string) => t.trim().replace(/\s*\n\s*/g, ' / ')

function isAnswered(item: Item, a: Answer) {
  if (item.kind === 'pick') return !!a.picked
  if (item.kind === 'text') return !!a.text?.trim()
  const choice = a.choice ?? []
  return choice.some((c) => c !== OTHER) || (choice.includes(OTHER) && !!a.other?.trim())
}

function describeChoice(item: ChoiceItem, a: Answer) {
  const choice = a.choice ?? []
  const parts = item.options.filter((o) => choice.includes(o.id)).map((o) => `${o.id}) ${o.label}`)
  const usedOther = choice.includes(OTHER) && !!a.other?.trim()
  if (usedOther) parts.push(`Other: ${oneLine(a.other!)}`)
  const rec = recommendedOf(item)
  const picked = choice.filter((c) => c !== OTHER)
  const isRec = !usedOther && rec.length > 0 && rec.length === picked.length && rec.every((r) => picked.includes(r))
  return parts.join('; ') + (isRec ? ' (recommended)' : '')
}

export function buildFeedback(title: string, items: Item[], state: State) {
  const get = (id: string) => state.answers[id] ?? {}
  const noteLine = (id: string) => (get(id).note?.trim() ? [`  Note: ${oneLine(get(id).note!)}`] : [])
  const head = (i: Item) => `- [${i.id}] ${i.title}`

  const questions = items.filter((i) => i.kind !== 'pick')
  const answered = questions.filter((i) => isAnswered(i, get(i.id)))
  const unanswered = questions.filter((i) => !isAnswered(i, get(i.id)))
  const picks = items.filter((i) => i.kind === 'pick')

  const sections: [string, string[]][] = [
    [
      'Answers',
      answered.flatMap((i) => [
        `${head(i)} → ${i.kind === 'choice' ? describeChoice(i, get(i.id)) : oneLine(get(i.id).text!)}`,
        ...noteLine(i.id),
      ]),
    ],
    ['Selected', picks.filter((i) => get(i.id).picked).flatMap((i) => [head(i), ...noteLine(i.id)])],
    [
      'Not answered',
      unanswered.flatMap((i) => {
        const rec = i.kind === 'choice' ? recommendedOf(i) : []
        return [head(i) + (rec.length ? ` (recommended: ${rec.join(', ')})` : ''), ...noteLine(i.id)]
      }),
    ],
    ['Notes on items not selected', picks.filter((i) => !get(i.id).picked && get(i.id).note?.trim()).flatMap((i) => [head(i), ...noteLine(i.id)])],
    ['My ideas', state.ideas.filter((t) => t.trim()).map((t) => `- ${oneLine(t)}`)],
    ['Other notes', state.note.trim() ? [state.note.trim()] : []],
  ]

  const lines = [`# Feedback: ${title}`]
  if (questions.length) lines.push(`${answered.length} of ${questions.length} questions answered.`)
  for (const [name, body] of sections) if (body.length) lines.push('', `## ${name}`, ...body)
  return lines.join('\n')
}

// --------------------------------------------------------------- state ---

type Ctx = {
  title: string
  items: Item[]
  state: State
  setState: (fn: (s: State) => State) => void
}

const FeedbackContext = createContext<Ctx | null>(null)

export function FeedbackProvider({
  artifact,
  title,
  items,
  children,
}: {
  /** Stable storage key; use the artifact folder name. */
  artifact: string
  /** Shown at the top of the copied text. */
  title: string
  items: Item[]
  children: ReactNode
}) {
  const ids = new Set<string>()
  for (const i of items) {
    if (ids.has(i.id)) throw new Error(`Feedback item id "${i.id}" is used twice`)
    ids.add(i.id)
  }

  const key = `feedback:${artifact}`
  const [state, setState] = useState<State>(() => {
    try {
      return { ...EMPTY, ...JSON.parse(localStorage.getItem(key) ?? '{}') }
    } catch {
      return EMPTY
    }
  })
  useEffect(() => {
    try {
      localStorage.setItem(key, JSON.stringify(state))
    } catch {
      /* storage unavailable */
    }
  }, [key, state])

  return <FeedbackContext.Provider value={{ title, items, state, setState }}>{children}</FeedbackContext.Provider>
}

/** Read and change answers from custom UI, e.g. a "Selected" filter or a progress count. */
export function useFeedback() {
  const ctx = useContext(FeedbackContext)
  if (!ctx) throw new Error('useFeedback must be used inside <FeedbackProvider>')
  const { title, items, state, setState } = ctx

  return useMemo(() => {
    const answer = (id: string): Answer => state.answers[id] ?? {}
    const update = (id: string, patch: Partial<Answer>) =>
      setState((s) => ({ ...s, answers: { ...s.answers, [id]: { ...s.answers[id], ...patch } } }))
    const questions = items.filter((i) => i.kind !== 'pick')
    const unansweredWithRec = items.filter(
      (i): i is ChoiceItem => i.kind === 'choice' && recommendedOf(i).length > 0 && !isAnswered(i, answer(i.id)),
    )
    return {
      items,
      state,
      setState,
      answer,
      update,
      isAnswered: (id: string) => {
        const item = items.find((i) => i.id === id)
        return item ? isAnswered(item, answer(id)) : false
      },
      counts: {
        questions: questions.length,
        answered: questions.filter((i) => isAnswered(i, answer(i.id))).length,
        picked: items.filter((i) => i.kind === 'pick' && answer(i.id).picked).length,
        notes: Object.values(state.answers).filter((a) => a.note?.trim()).length,
        recommendable: unansweredWithRec.length,
      },
      acceptRecommended: () => {
        for (const i of unansweredWithRec) update(i.id, { choice: recommendedOf(i) })
      },
      text: () => buildFeedback(title, items, state),
    }
  }, [title, items, state, setState])
}

// ---------------------------------------------------------------- views ---

const inputClass =
  'min-w-0 flex-1 rounded-sm border border-input bg-transparent px-2 py-1 text-sm outline-none placeholder:text-muted-foreground focus-visible:ring-1 focus-visible:ring-ring'
const rowClass = 'flex cursor-pointer items-start gap-3 rounded-sm px-2 py-1.5 hover:bg-muted'

function OptionText({ option, recommended }: { option: Option; recommended: boolean }) {
  return (
    <>
      <span className="w-4 shrink-0 font-mono text-xs leading-5 text-muted-foreground">{option.id}</span>
      <span className="min-w-0 flex-1 text-sm leading-5">
        {option.label}
        {option.detail && <span className="block text-xs text-muted-foreground">{option.detail}</span>}
      </span>
      {recommended && (
        <span className="shrink-0 font-mono text-[10px] uppercase leading-5 tracking-[0.15em] text-muted-foreground">
          Recommended
        </span>
      )}
    </>
  )
}

function ChoiceView({ item }: { item: ChoiceItem }) {
  const { answer, update } = useFeedback()
  const a = answer(item.id)
  const chosen = a.choice ?? []
  const rec = recommendedOf(item)
  const allowOther = item.other !== false

  const setOther = (v: string) => {
    const without = chosen.filter((c) => c !== OTHER)
    if (item.multiple) update(item.id, { other: v, choice: v.trim() ? [...without, OTHER] : without })
    else update(item.id, { other: v, choice: v.trim() ? [OTHER] : chosen[0] === OTHER ? [] : chosen })
  }
  const otherInput = (
    <input
      value={a.other ?? ''}
      onChange={(e) => setOther(e.target.value)}
      placeholder="Other answer…"
      aria-label={`Other answer for ${item.id}`}
      className={inputClass}
    />
  )

  const options = item.multiple ? (
    <div role="group" aria-label={item.title} className="grid gap-0.5">
      {item.options.map((o) => (
        <label key={o.id} className={rowClass}>
          <Checkbox
            checked={chosen.includes(o.id)}
            onCheckedChange={(on) =>
              update(item.id, { choice: on ? [...chosen, o.id] : chosen.filter((c) => c !== o.id) })
            }
            className="mt-0.5"
          />
          <OptionText option={o} recommended={rec.includes(o.id)} />
        </label>
      ))}
      {allowOther && (
        <div className="flex items-center gap-3 px-2 py-1.5">
          <Checkbox
            checked={chosen.includes(OTHER)}
            onCheckedChange={(on) =>
              update(item.id, { choice: on ? [...chosen, OTHER] : chosen.filter((c) => c !== OTHER) })
            }
            aria-label="Other"
          />
          {otherInput}
        </div>
      )}
    </div>
  ) : (
    <RadioGroup
      value={chosen[0] ?? ''}
      onValueChange={(v) => update(item.id, { choice: [v] })}
      aria-label={item.title}
      className="gap-0.5"
    >
      {item.options.map((o) => (
        <label key={o.id} className={rowClass}>
          <RadioGroupItem value={o.id} className="mt-0.5" />
          <OptionText option={o} recommended={rec.includes(o.id)} />
        </label>
      ))}
      {allowOther && (
        <div className="flex items-center gap-3 px-2 py-1.5">
          <RadioGroupItem value={OTHER} aria-label="Other" />
          {otherInput}
        </div>
      )}
    </RadioGroup>
  )

  return (
    <div className="mt-3">
      {options}
      {item.why && (
        <p className="mt-2 px-2 text-xs leading-relaxed text-muted-foreground">
          {rec.length > 0 && <span className="font-medium text-foreground/80">Recommended {rec.join(', ')}. </span>}
          {item.why}
        </p>
      )}
    </div>
  )
}

function NoteField({ id }: { id: string }) {
  const { answer, update } = useFeedback()
  const note = answer(id).note ?? ''
  const [open, setOpen] = useState(false)

  if (!open && !note)
    return (
      <button type="button" onClick={() => setOpen(true)} className="mt-2 text-xs text-muted-foreground hover:text-foreground">
        + Add a note
      </button>
    )
  return (
    <Textarea
      autoFocus={open && !note}
      value={note}
      onChange={(e) => update(id, { note: e.target.value })}
      onBlur={() => setOpen(false)}
      aria-label={`Note on ${id}`}
      placeholder="Your note"
      rows={1}
      className="mt-2 min-h-0 resize-none field-sizing-content"
    />
  )
}

/** One item, rendered by id. Place these anywhere inside the provider. */
export function FeedbackItem({ id }: { id: string }) {
  const { items, answer, update, isAnswered: answered } = useFeedback()
  const item = items.find((i) => i.id === id)
  if (!item) throw new Error(`No feedback item with id "${id}"`)
  const a = answer(id)
  const checkboxId = `fb-pick-${id}`

  return (
    <div id={`fb-${id}`} className="scroll-mt-24 border-b border-border py-4">
      <div className="flex items-baseline gap-3">
        {item.kind === 'pick' && (
          <Checkbox
            id={checkboxId}
            checked={!!a.picked}
            onCheckedChange={(on) => update(id, { picked: on === true })}
            className="translate-y-0.5"
          />
        )}
        <span className="shrink-0 font-mono text-xs text-muted-foreground">{id}</span>
        {item.kind === 'pick' ? (
          <label htmlFor={checkboxId} className="cursor-pointer text-sm font-medium leading-snug">
            {item.title}
          </label>
        ) : (
          <h3 className="text-sm font-medium leading-snug">{item.title}</h3>
        )}
        {item.kind === 'choice' && answered(id) && !item.multiple && (
          <button
            type="button"
            onClick={() => update(id, { choice: [], other: '' })}
            className="ml-auto shrink-0 text-xs text-muted-foreground hover:text-foreground"
          >
            Clear
          </button>
        )}
      </div>
      {item.detail && <div className="mt-1 text-sm leading-relaxed text-muted-foreground">{item.detail}</div>}
      {item.kind === 'choice' && <ChoiceView item={item} />}
      {item.kind === 'text' && (
        <Textarea
          value={a.text ?? ''}
          onChange={(e) => update(id, { text: e.target.value })}
          aria-label={item.title}
          placeholder={item.placeholder ?? 'Your answer'}
          rows={2}
          className="mt-3 min-h-0 resize-none field-sizing-content"
        />
      )}
      <NoteField id={id} />
    </div>
  )
}

/** Every item (or the ones passed in), with a heading whenever `group` changes. */
export function FeedbackList({ items: only }: { items?: Item[] }) {
  const { items } = useFeedback()
  const list = only ?? items
  return (
    <div>
      {list.map((item, i) => (
        <div key={item.id}>
          {item.group && item.group !== list[i - 1]?.group && (
            <h2 className="mt-10 border-b border-border pb-2 font-mono text-xs uppercase tracking-[0.2em] text-muted-foreground">
              {item.group}
            </h2>
          )}
          <FeedbackItem id={item.id} />
        </div>
      ))}
    </div>
  )
}

/** The user's own ideas plus one free-form note, both included in the copied text. */
export function IdeasBox() {
  const { state, setState } = useFeedback()
  const setIdeas = (ideas: string[]) => setState((s) => ({ ...s, ideas }))

  return (
    <section className="py-8">
      <h2 className="font-mono text-xs uppercase tracking-[0.2em] text-muted-foreground">Your ideas</h2>
      <div className="mt-3 grid gap-2">
        {state.ideas.map((idea, i) => (
          <div key={i} className="flex items-start gap-2">
            <Textarea
              autoFocus={!idea}
              value={idea}
              onChange={(e) => setIdeas(state.ideas.map((t, j) => (j === i ? e.target.value : t)))}
              aria-label={`Idea ${i + 1}`}
              placeholder="Describe your idea"
              rows={1}
              className="min-h-0 resize-none field-sizing-content"
            />
            <Button variant="ghost" size="sm" onClick={() => setIdeas(state.ideas.filter((_, j) => j !== i))}>
              Remove
            </Button>
          </div>
        ))}
      </div>
      <Button variant="outline" size="sm" className="mt-3" onClick={() => setIdeas([...state.ideas, ''])}>
        + Add an idea
      </Button>
      <Textarea
        value={state.note}
        onChange={(e) => setState((s) => ({ ...s, note: e.target.value }))}
        aria-label="Anything else"
        placeholder="Anything else?"
        rows={2}
        className="mt-6 min-h-0 resize-none field-sizing-content"
      />
    </section>
  )
}

async function copyText(text: string) {
  try {
    await navigator.clipboard.writeText(text)
    return true
  } catch {
    const ta = document.createElement('textarea')
    ta.value = text
    ta.style.position = 'fixed'
    ta.style.opacity = '0'
    document.body.appendChild(ta)
    ta.select()
    const ok = document.execCommand('copy')
    ta.remove()
    return ok
  }
}

/** Sticky bar with progress, "use recommended for the rest", Clear and Copy. */
export function CopyBar() {
  const { counts, acceptRecommended, setState, text } = useFeedback()
  const [copied, setCopied] = useState(false)
  const [fallback, setFallback] = useState<string | null>(null)

  const copy = async () => {
    const t = text()
    if (await copyText(t)) {
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    } else setFallback(t)
  }

  const status = [
    counts.questions > 0 && `${counts.answered}/${counts.questions} answered`,
    counts.picked > 0 && `${counts.picked} selected`,
    counts.notes > 0 && `${counts.notes} ${counts.notes === 1 ? 'note' : 'notes'}`,
  ].filter(Boolean)

  return (
    <>
      <div className="h-24" aria-hidden />
      <div className="fixed inset-x-0 bottom-0 z-40 border-t border-border bg-background/95 backdrop-blur">
        <div className="mx-auto flex max-w-5xl flex-wrap items-center gap-x-4 gap-y-2 px-4 py-3 sm:px-6">
          <span className="font-mono text-xs tabular-nums text-muted-foreground">{status.join(' · ')}</span>
          <span className="ml-auto flex flex-wrap items-center gap-2">
            <span aria-live="polite" className="text-xs text-muted-foreground">
              {copied ? 'Copied' : ''}
            </span>
            {counts.recommendable > 0 && (
              <Button variant="ghost" size="sm" onClick={acceptRecommended}>
                Use recommended for the other {counts.recommendable}
              </Button>
            )}
            <Button
              variant="ghost"
              size="sm"
              onClick={() => window.confirm('Clear all your answers, notes and ideas?') && setState(() => EMPTY)}
            >
              Clear
            </Button>
            <Button size="sm" onClick={copy}>
              Copy feedback
            </Button>
          </span>
        </div>
      </div>
      <Dialog open={fallback !== null} onOpenChange={(open) => !open && setFallback(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Copy your feedback</DialogTitle>
            <DialogDescription>Your browser blocked copying. Select the text below and copy it.</DialogDescription>
          </DialogHeader>
          <Textarea
            readOnly
            autoFocus
            value={fallback ?? ''}
            onFocus={(e) => e.currentTarget.select()}
            rows={12}
            className="font-mono text-xs"
          />
        </DialogContent>
      </Dialog>
    </>
  )
}
