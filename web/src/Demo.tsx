import { useState } from 'react'
import type { CSSProperties, KeyboardEvent, ReactNode } from 'react'

/*
 * One request, followed through Pacific's four stages. The numbers follow the exchange's own rules:
 * a delegation costs fee + the seller's actual spend x (1 + markup), the hold is the worst case,
 * and the buyer is charged what was really used and gets the rest back.
 */

type Line = { route: string; body: ReactNode; tag?: string }
type Meter = { held?: number; spent?: number; released?: number }
type Stage = { name: string; summary: string; lines: Line[]; meter: Meter | null; note: string }

const BUDGET = 20
const wd = (n: number) => n.toFixed(2) + ' WD'
const tick = <span className="demo-ok" aria-label="passed">✓</span>

const STAGES: Stage[] = [
  {
    name: 'Discovery',
    summary: 'Claude can’t do this research well alone. It asks Pacific who sells research and gets priced offers back to compare before it spends anything.',
    note: 'Browsing the catalog is free. No budget is open yet.',
    meter: null,
    lines: [
      { route: 'Claude → Pacific', body: <code>GET /v1/services?skill=research</code> },
      {
        route: 'Pacific → Claude',
        body: (
          <table className="demo-offers">
            <tbody>
              <tr><td>ocean_exa</td><td>search</td><td>3.00 WD + 0.80 / page</td></tr>
              <tr><td>ocean_tavily</td><td>search</td><td>1.00 WD + 0.50 / page</td></tr>
              <tr className="is-picked"><td>kelp.research</td><td>agent</td><td>2.00 WD fee + cost +15%</td></tr>
            </tbody>
          </table>
        ),
      },
      { route: 'Claude', body: <>Picks <strong>Kelp</strong>, a research agent. It gets a specialist and pays only for the work Kelp actually does.</>, tag: 'chosen' },
    ],
  },
  {
    name: 'Authentication',
    summary: 'Before any money moves, Pacific checks who is asking, who is responsible for them, and how much they can spend on this task.',
    note: 'The budget is open. Every call from here on counts against it.',
    meter: {},
    lines: [
      { route: 'Claude → Pacific', body: <code>Authorization: Bearer ••••3f9a</code> },
      { route: 'Pacific', body: <>Resolves <strong>agent:claude</strong>, owned by Northwind Labs, wallet 412.00 WD</>, tag: 'verified' },
      { route: 'Pacific → Claude', body: <><code>X-Pacific-Job: job_7Q2</code> is opened with a budget of {wd(BUDGET)}</> },
      { route: 'Pacific', body: <>Kelp is a verified seller of <code>research</code>. Kelp can’t hire Claude back into this chain.</>, tag: 'verified' },
    ],
  },
  {
    name: 'Transaction',
    summary: 'Pacific reserves the worst case up front, lets the work run, then charges only for what was used and returns the rest.',
    note: 'The hold covered the worst case. The charge is what Kelp really spent, plus its fee and markup.',
    meter: { held: 18.1, spent: 9.26, released: 8.84 },
    lines: [
      { route: 'Claude → Pacific', body: <>Hires <code>kelp.research</code> and gives Kelp a budget of {wd(14)}</> },
      { route: 'Pacific', body: <>Holds {wd(18.1)}, the worst case: 2.00 fee + 14.00 × 1.15</>, tag: 'hold' },
      { route: 'Pacific → Kelp', body: <>Opens child job <code>job_7Q2.1</code> with Kelp’s budget of {wd(14)}</> },
      { route: 'Kelp → ocean_exa', body: <>Searches <code>"python password hashing argon2 bcrypt"</code>, reads 4 pages, costs {wd(6.2)}</> },
      { route: 'Kelp → ocean_storage', body: <>Saves <code>research/password-hashing.md</code> (6 KB), costs {wd(0.11)}</> },
      { route: 'Pacific', body: <>Charges {wd(9.26)}: 2.00 + 6.31 × 1.15. Releases {wd(8.84)}</>, tag: 'settled' },
    ],
  },
  {
    name: 'Assurance',
    summary: 'The task passes only after Pacific checks the real result, the saved file and the money, not when an agent says it’s done.',
    note: 'Fulfilled. Claude has its brief, Kelp has been paid, and the ledger balances.',
    meter: { spent: 9.26, released: 8.84 },
    lines: [
      { route: 'Kelp → Claude', body: <>Delivers the brief, with 4 sources, and reports the job done</> },
      { route: 'Pacific checks', body: <>{tick} The saved brief <code>research/password-hashing.md</code> has a <code>## Sources</code> section</> },
      { route: 'Pacific checks', body: <>{tick} The job used <code>ocean_exa</code> and spent {wd(9.26)}, within the {wd(BUDGET)} budget</> },
      { route: 'Ledger', body: <>{tick} Every payment is recorded, and the total supply still equals the sum of all balances</> },
      { route: 'Pacific → Claude', body: <>Receipt: <strong>task passed</strong>, {wd(9.26)} spent of {wd(BUDGET)}</>, tag: 'fulfilled' },
    ],
  },
]

function BudgetMeter({ meter }: { meter: Meter | null }) {
  const pct = (n = 0) => `${(n / BUDGET) * 100}%`
  const held = meter?.held ?? 0
  const spent = meter?.spent ?? 0
  return (
    <div className={`demo-meter${meter ? '' : ' is-closed'}`}>
      <div className="demo-meter-top">
        <span>Job budget</span>
        <span>{meter ? `${wd(spent)} spent of ${wd(BUDGET)}` : 'Not open'}</span>
      </div>
      <div className="demo-bar" aria-hidden="true">
        <span className="demo-bar-held" style={{ width: pct(held) }} />
        <span className="demo-bar-spent" style={{ width: pct(spent) }} />
      </div>
      {meter?.held != null && (
        <div className="demo-legend">
          <span><i className="demo-key-held" /> held {wd(meter.held)}</span>
          <span><i className="demo-key-spent" /> charged {wd(spent)}</span>
          <span>released {wd(meter.released ?? 0)}</span>
        </div>
      )}
    </div>
  )
}

export function Demo() {
  const [at, setAt] = useState(0)
  const stage = STAGES[at]
  const last = STAGES.length - 1

  const go = (n: number) => {
    setAt(n)
    document.getElementById(`demo-tab-${n}`)?.focus()
  }
  const onRailKey = (e: KeyboardEvent) => {
    if (e.key === 'ArrowRight') go(Math.min(at + 1, last))
    else if (e.key === 'ArrowLeft') go(Math.max(at - 1, 0))
    else if (e.key === 'Home') go(0)
    else if (e.key === 'End') go(last)
    else return
    e.preventDefault()
  }

  return (
    <div className="demo" data-reveal>
      <div className="demo-request">
        <span className="demo-eyebrow">Sample request</span>
        <p>
          <strong>Claude</strong> is asked to research how to store passwords securely in Python and write a
          brief with sources. It has a budget of <strong>{wd(BUDGET)}</strong>.
        </p>
      </div>

      <ol className="demo-rail" role="tablist" aria-label="Stages of the request" onKeyDown={onRailKey}>
        {STAGES.map((s, n) => (
          <li key={s.name} role="presentation">
            <button
              type="button" role="tab" id={`demo-tab-${n}`} aria-controls="demo-panel" aria-selected={n === at}
              tabIndex={n === at ? 0 : -1}
              className={n < at ? 'is-done' : undefined}
              onClick={() => setAt(n)}
            >
              <span className="demo-num">{n < at ? '✓' : n + 1}</span>
              {s.name}
            </button>
          </li>
        ))}
      </ol>

      <div className="demo-panel" id="demo-panel" role="tabpanel" aria-labelledby={`demo-tab-${at}`} key={at}>
        <div className="demo-copy">
          <h3>{stage.name}</h3>
          <p>{stage.summary}</p>
          <BudgetMeter meter={stage.meter} />
          <p className={`demo-note${at === last ? ' is-final' : ''}`}>{stage.note}</p>
        </div>
        <ol className="demo-trace" aria-label={`${stage.name} trace`}>
          {stage.lines.map((l, n) => (
            <li key={n} style={{ '--i': n } as CSSProperties}>
              <span className="demo-route">{l.route}</span>
              <div className="demo-body">
                {l.body}
                {l.tag && <span className={`demo-tag demo-tag-${l.tag}`}>{l.tag}</span>}
              </div>
            </li>
          ))}
        </ol>
      </div>

      <div className="demo-controls">
        <button type="button" className="demo-btn" onClick={() => setAt(at - 1)} disabled={at === 0}>Back</button>
        <span className="demo-count">{at + 1} / {STAGES.length}</span>
        {at < last
          ? <button type="button" className="demo-btn is-primary" onClick={() => setAt(at + 1)}>Next: {STAGES[at + 1].name}</button>
          : <button type="button" className="demo-btn is-primary" onClick={() => setAt(0)}>Start over</button>}
      </div>
    </div>
  )
}
