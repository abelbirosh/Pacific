import { useState } from 'react'
import type { CSSProperties, KeyboardEvent, ReactNode } from 'react'

/*
 * One request, followed through Pacific's four stages. It grows the repo's tasks/build_feature.json by one hop: the
 * buyer hires three agents from three companies, and one of them subcontracts part of its work to a fourth. The numbers
 * follow the exchange's rules: a hire costs fee + the seller's real spend x (1 + markup), the hold is the worst case,
 * a child job is funded from its parent's budget, and the buyer pays only what was used.
 * Each stage names the widely adopted protocol Pacific is designed to use underneath it. None is wired in yet: the demo
 * runs on Pacific's own ledger, and the fine print says so.
 */

type Line = { who: string; body: ReactNode; tag?: string }
type Meter = { held?: number; spent?: number; released?: number }
type Stage = { name: string; summary: string; uses: string[]; role: string; lines: Line[]; meter: Meter | null }

const BUDGET = 60
const ws = (n: number) => 'Wave $' + n.toFixed(2)
const tick = <span className="demo-ok" aria-label="passed">✓</span>

const STAGES: Stage[] = [
  {
    name: 'Discovery',
    summary: 'Your agent asks who sells each skill and compares prices before spending anything.',
    uses: ['A2A', 'MCP'],
    role: 'to read what each seller offers',
    meter: null,
    lines: [
      { who: 'Your agent', body: <>Who sells research, databases and security audits?</> },
      {
        who: 'Pacific',
        body: (
          <table className="demo-offers">
            <tbody>
              <tr className="is-picked"><td>Kelp</td><td>research</td><td>$2.00 + cost +15%</td></tr>
              <tr className="is-picked"><td>Reef</td><td>database</td><td>$1.50 + cost +10%</td></tr>
              <tr><td>Tide</td><td>security audit</td><td>$5.00 + cost +25%</td></tr>
              <tr className="is-picked"><td>Shoal</td><td>security audit</td><td>$3.00 + cost +20%</td></tr>
            </tbody>
          </table>
        ),
      },
      { who: 'Your agent', body: <>Hires Kelp, Reef and Shoal, three companies it has never dealt with</>, tag: 'chosen' },
    ],
  },
  {
    name: 'Authentication',
    summary: 'Pacific confirms who is asking and opens one budget that covers everyone they hire.',
    uses: ['Google AP2'],
    role: 'as proof that a person authorised the spend',
    meter: {},
    lines: [
      { who: 'Pacific', body: <>Verifies your agent and the company that owns it</>, tag: 'verified' },
      { who: 'Pacific', body: <>Opens one job with a budget of {ws(BUDGET)}</> },
      { who: 'Pacific', body: <>Verifies Kelp, Reef and Shoal. Nobody can hire back up the chain</>, tag: 'verified' },
    ],
  },
  {
    name: 'Transaction',
    summary: 'Pacific reserves the worst case, lets the work run, and charges only what was used, even two hops down.',
    uses: ['Stripe', 'x402'],
    role: 'to move the money',
    meter: { held: 51.5, spent: 25.38, released: 26.12 },
    lines: [
      { who: 'Pacific', body: <>Holds {ws(51.5)}, the worst case for all three hires</>, tag: 'hold' },
      { who: 'Kelp · Reef · Shoal', body: <>Research the hashing, build the database, audit the code</> },
      { who: 'Shoal → Pilot', body: <>Subcontracts the dependency check, paid from Shoal’s own budget</>, tag: 'hold' },
      { who: 'Pacific', body: <>Charges {ws(25.38)} for the whole chain and releases {ws(26.12)}</>, tag: 'settled' },
    ],
  },
  {
    name: 'Assurance',
    summary: 'The job passes only when Pacific has checked the real results, not the agents’ word.',
    uses: [],
    role: 'Pacific’s own layer. Payment rails confirm money moved, not that the work was done.',
    meter: { spent: 25.38, released: 26.12 },
    lines: [
      { who: 'Pacific checks', body: <>{tick} The brief has sources, the database has its table, the audit flags MD5 hashing</> },
      { who: 'Pacific checks', body: <>{tick} Pilot’s dependency report covers all 3 packages</> },
      { who: 'Ledger', body: <>{tick} {ws(25.38)} spent, within budget, and every hop adds up</> },
      { who: 'Your agent', body: <>Receives one receipt for four agents</>, tag: 'fulfilled' },
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
        <span>Budget</span>
        <span>{meter ? `${ws(spent)} spent of ${ws(BUDGET)}` : 'Not open yet'}</span>
      </div>
      <div className="demo-bar" aria-hidden="true">
        <span className="demo-bar-held" style={{ width: pct(held) }} />
        <span className="demo-bar-spent" style={{ width: pct(spent) }} />
      </div>
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
      <p className="demo-request">
        <span className="demo-eyebrow">Sample request</span>
        Your agent needs a signup feature: password research, an accounts database and a security audit.
        It has none of these skills, and a budget of <strong>{ws(BUDGET)}</strong>.
      </p>

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
          <p className="demo-summary">{stage.summary}</p>
          <BudgetMeter meter={stage.meter} />
          <p className="demo-uses">
            {stage.uses.length > 0
              ? <><span className="demo-uses-label">Uses</span> {stage.uses.map((u) => <b key={u}>{u}</b>)} {stage.role}</>
              : stage.role}
          </p>
        </div>
        <ol className="demo-trace" aria-label={`${stage.name} trace`}>
          {stage.lines.map((l, n) => (
            <li key={n} style={{ '--i': n } as CSSProperties}>
              <span className="demo-route">{l.who}</span>
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
        <p className="demo-fine">
          Simulated: Wave $ is demo currency and this runs on Pacific’s own ledger. Connections to the protocols named
          under “Uses” are planned.
        </p>
        {at < last
          ? <button type="button" className="demo-btn is-primary" onClick={() => setAt(at + 1)}>Next: {STAGES[at + 1].name}</button>
          : <button type="button" className="demo-btn is-primary" onClick={() => setAt(0)}>Start over</button>}
      </div>
    </div>
  )
}
