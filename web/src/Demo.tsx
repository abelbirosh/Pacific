import { useState } from 'react'
import type { CSSProperties, KeyboardEvent, ReactNode } from 'react'

/*
 * One request, followed through Pacific's four stages. It is the repo's tasks/build_feature.json: a job that needs
 * three skills the buyer lacks, so it hires three agents run by three different companies, each of which pays for
 * services of its own. The numbers follow the exchange's rules: a hire costs fee + the seller's real spend
 * x (1 + markup), the hold is the worst case, and the buyer pays only what was used.
 */

type Line = { route: string; body: ReactNode; tag?: string }
type Meter = { held?: number; spent?: number; released?: number }
type Stage = { name: string; summary: string; lines: Line[]; meter: Meter | null; note: string }

const BUDGET = 60
const ws = (n: number) => 'Wave $' + n.toFixed(2)
const tick = <span className="demo-ok" aria-label="passed">✓</span>

const STAGES: Stage[] = [
  {
    name: 'Discovery',
    summary: 'Your agent can’t do any of the three jobs itself. It asks Pacific who sells each skill and compares priced offers before it spends anything.',
    note: 'Browsing the catalog is free. No budget is open yet.',
    meter: null,
    lines: [
      { route: 'Your agent → Pacific', body: <code>GET /v1/services?skill=research,database,security_audit</code> },
      {
        route: 'Pacific → Your agent',
        body: (
          <table className="demo-offers">
            <tbody>
              <tr className="is-picked"><td>kelp.research</td><td>Tidewater Labs</td><td>$2.00 fee + cost +15%</td></tr>
              <tr className="is-picked"><td>reef.database</td><td>Harbor Data</td><td>$1.50 fee + cost +10%</td></tr>
              <tr><td>tide.security_audit</td><td>Breakwater</td><td>$5.00 fee + cost +25%</td></tr>
              <tr className="is-picked"><td>shoal.security_audit</td><td>Lighthouse Sec</td><td>$3.00 fee + cost +20%</td></tr>
            </tbody>
          </table>
        ),
      },
      { route: 'Your agent', body: <>Picks <strong>Kelp</strong>, <strong>Reef</strong> and <strong>Shoal</strong>: three agents from three companies it has never dealt with</>, tag: 'chosen' },
    ],
  },
  {
    name: 'Authentication',
    summary: 'Before any money moves, Pacific checks who is asking, who is responsible for them, and how much they can spend across every agent they hire.',
    note: 'One budget now covers the whole job, including everyone your agent hires.',
    meter: {},
    lines: [
      { route: 'Your agent → Pacific', body: <code>Authorization: Bearer ••••3f9a</code> },
      { route: 'Pacific', body: <>Resolves <strong>your agent</strong>, owned by your company, with a wallet of {ws(412)}</>, tag: 'verified' },
      { route: 'Pacific → Your agent', body: <><code>X-Pacific-Job: job_7Q2</code> is opened with a budget of {ws(BUDGET)}</> },
      { route: 'Pacific', body: <>Kelp, Reef and Shoal are verified sellers. None of them can hire your agent back into this job.</>, tag: 'verified' },
    ],
  },
  {
    name: 'Transaction',
    summary: 'Pacific reserves the worst case for all three hires, lets the work run, then charges only for what each agent actually used.',
    note: 'Each agent paid for its own services. Your agent was charged their real cost plus fee and markup.',
    meter: { held: 51.5, spent: 21.87, released: 29.63 },
    lines: [
      { route: 'Your agent → Pacific', body: <>Hires Kelp, Reef and Shoal with budgets of {ws(12)}, {ws(12)} and {ws(15)}</> },
      { route: 'Pacific', body: <>Holds {ws(51.5)}, the worst case for all three, inside the {ws(BUDGET)} budget</>, tag: 'hold' },
      { route: 'Kelp → ocean_exa', body: <>Researches password hashing, reads 4 pages, saves the brief. Costs {ws(6.31)}</> },
      { route: 'Reef → ocean_db', body: <>Creates the <code>accounts</code> database and seeds the first row. Costs {ws(5.1)}</> },
      { route: 'Shoal → ocean_shield', body: <>Scans <code>signup.py</code> and flags MD5 password hashing and SQL built from strings. Costs {ws(2.08)}</> },
      { route: 'Pacific', body: <>Charges {ws(21.87)}: each agent’s fee, plus its real cost and markup. Releases {ws(29.63)}</>, tag: 'settled' },
    ],
  },
  {
    name: 'Assurance',
    summary: 'The job passes only after Pacific checks the real result of every hire and the money, not when the agents say they’re done.',
    note: 'Fulfilled. Three agents and four services, settled from one budget on one receipt.',
    meter: { spent: 21.87, released: 29.63 },
    lines: [
      { route: 'Pacific checks', body: <>{tick} Kelp’s brief <code>signup/research.md</code> has a <code>## Sources</code> section</> },
      { route: 'Pacific checks', body: <>{tick} Reef’s <code>accounts</code> table has <code>email</code> and <code>password_hash</code> and at least 1 row</> },
      { route: 'Pacific checks', body: <>{tick} Shoal’s audit has a verdict and flags MD5 hashing (<code>B324</code>)</> },
      { route: 'Ledger', body: <>{tick} {ws(21.87)} spent, within the budget. Total supply still equals the sum of all balances</> },
      { route: 'Pacific → Your agent', body: <>Receipt: <strong>job passed</strong>, {ws(21.87)} spent of {ws(BUDGET)} across 3 agents</>, tag: 'fulfilled' },
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
        <span>{meter ? `${ws(spent)} spent of ${ws(BUDGET)}` : 'Not open'}</span>
      </div>
      <div className="demo-bar" aria-hidden="true">
        <span className="demo-bar-held" style={{ width: pct(held) }} />
        <span className="demo-bar-spent" style={{ width: pct(spent) }} />
      </div>
      {meter?.held != null && (
        <div className="demo-legend">
          <span><i className="demo-key-held" /> held {ws(meter.held)}</span>
          <span><i className="demo-key-spent" /> charged {ws(spent)}</span>
          <span>released {ws(meter.released ?? 0)}</span>
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
          <strong>Your agent</strong> is asked to ship a signup feature: research how to hash passwords, build the
          accounts database, and security-audit the signup code. It can’t do any of the three itself. Its budget
          is <strong>{ws(BUDGET)}</strong>.
        </p>
        <dl className="demo-why">
          <div>
            <dt>Without Pacific</dt>
            <dd>
              A separate account, key, contract and bill with three agents from three companies, and each of them needs
              the same with every service it uses. No single cap covers them all, and nobody checks the work. Every extra
              hand-off multiplies the setup again.
            </dd>
          </div>
          <div>
            <dt>With Pacific</dt>
            <dd>One key, one budget across every hire, checks on the real result, and one receipt.</dd>
          </div>
        </dl>
        <p className="demo-fine">Wave $ is simulated currency, used only for this demo.</p>
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
