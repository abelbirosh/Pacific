import { useState } from 'react'
import type { CSSProperties, KeyboardEvent, ReactNode } from 'react'

/*
 * One request, followed through Pacific's four stages. It grows the repo's tasks/build_feature.json by one hop: the
 * buyer hires three agents from three companies, and one of them subcontracts part of its work to a fourth. The numbers
 * follow the exchange's rules: a hire costs fee + the seller's real spend x (1 + markup), the hold is the worst case,
 * a child job is funded from its parent's budget, and the buyer pays only what was used.
 * Each stage also names the industry rails Pacific sits above; only widely adopted ones are listed.
 */

type Line = { route: string; body: ReactNode; tag?: string }
type Meter = { held?: number; spent?: number; released?: number }
type Rails = { label: string; names: string[]; text: string }
type Stage = { name: string; summary: string; rails: Rails; lines: Line[]; meter: Meter | null; note: string }

const BUDGET = 60
const ws = (n: number) => 'Wave $' + n.toFixed(2)
const tick = <span className="demo-ok" aria-label="passed">✓</span>

const STAGES: Stage[] = [
  {
    name: 'Discovery',
    summary: 'Your agent can’t do any of the three jobs itself. It asks Pacific who sells each skill and compares priced offers before it spends anything.',
    rails: {
      label: 'Built on',
      names: ['A2A Agent Cards', 'MCP'],
      text: 'Sellers already describe themselves with A2A Agent Cards and MCP tools. Pacific gathers them into one catalog with prices and owners attached.',
    },
    note: 'Browsing the catalog is free. No budget is open yet.',
    meter: null,
    lines: [
      { route: 'Your agent → Pacific', body: <code>GET /v1/services?skill=research,database,security_audit</code> },
      { route: 'Pacific', body: <>Reads each seller’s A2A Agent Card and adds its price plan and owner</> },
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
    summary: 'Before any money moves, Pacific checks who is asking, who is responsible for them, and how much they can spend, including what the agents they hire go on to spend.',
    rails: {
      label: 'Built on',
      names: ['Google AP2', 'Visa Trusted Agent Protocol', 'Mastercard Agent Pay'],
      text: 'These prove that a person authorised your agent to spend. Pacific turns that one authorisation into a job budget that every hire, and every hire’s hire, draws from.',
    },
    note: 'One budget now covers the whole job, however many hands it passes through.',
    meter: {},
    lines: [
      { route: 'Your agent → Pacific', body: <code>Authorization: Bearer ••••3f9a</code> },
      { route: 'Pacific', body: <>Resolves <strong>your agent</strong>, owned by your company, and reads its spending mandate for this task</>, tag: 'verified' },
      { route: 'Pacific → Your agent', body: <><code>X-Pacific-Job: job_7Q2</code> is opened with a budget of {ws(BUDGET)}</> },
      { route: 'Pacific', body: <>Kelp, Reef and Shoal are verified sellers. Anyone they hire is checked the same way, and nobody can hire back up the chain.</>, tag: 'verified' },
    ],
  },
  {
    name: 'Transaction',
    summary: 'Pacific reserves the worst case across every hire, including a subcontract two levels down, lets the work run, then charges only for what was used.',
    rails: {
      label: 'Built on',
      names: ['Stripe', 'x402'],
      text: 'Stripe and x402 move each payment. Pacific decides which payments the job allows: it reserves the worst case across the whole chain and charges only what was used.',
    },
    note: 'Every hop paid for its own services. Each buyer was charged its seller’s real cost, plus fee and markup.',
    meter: { held: 51.5, spent: 25.38, released: 26.12 },
    lines: [
      { route: 'Your agent → Pacific', body: <>Hires Kelp, Reef and Shoal with budgets of {ws(12)}, {ws(12)} and {ws(15)}</> },
      { route: 'Pacific', body: <>Holds {ws(51.5)}, the worst case for all three, inside the {ws(BUDGET)} budget</>, tag: 'hold' },
      { route: 'Kelp → ocean_exa', body: <>Researches password hashing, reads 4 pages, saves the brief. Costs {ws(6.31)}</> },
      { route: 'Reef → ocean_db', body: <>Creates the <code>accounts</code> database and seeds the first row. Costs {ws(5.1)}</> },
      { route: 'Shoal → Pacific', body: <>Subcontracts the dependency check to <strong>Pilot</strong> (Keel Systems) with {ws(4)} of its own budget. Pacific holds {ws(5.4)} inside Shoal’s {ws(15)}</>, tag: 'hold' },
      { route: 'Pilot → ocean_shield', body: <>Checks 3 pinned packages for known vulnerabilities. Costs {ws(1.75)}</> },
      { route: 'Shoal → ocean_shield', body: <>Scans <code>signup.py</code> and flags MD5 password hashing and SQL built from strings. Costs {ws(2.08)}</> },
      { route: 'Pacific', body: <>Charges {ws(25.38)} for the whole chain, Pilot’s bill included in Shoal’s. Releases {ws(26.12)}</>, tag: 'settled' },
    ],
  },
  {
    name: 'Assurance',
    summary: 'The job passes only after Pacific checks the real result of every hire, at every level, and the money. An agent saying it’s done isn’t enough.',
    rails: {
      label: 'What the rails don’t do',
      names: [],
      text: 'Payment rails confirm that money moved. They don’t check that the work was done. Pacific checks the real result before the job counts as passed.',
    },
    note: 'Fulfilled. Four agents, two levels deep, four services: one budget, one receipt.',
    meter: { spent: 25.38, released: 26.12 },
    lines: [
      { route: 'Pacific checks', body: <>{tick} Kelp’s brief <code>signup/research.md</code> has a <code>## Sources</code> section</> },
      { route: 'Pacific checks', body: <>{tick} Reef’s <code>accounts</code> table has <code>email</code> and <code>password_hash</code> and at least 1 row</> },
      { route: 'Pacific checks', body: <>{tick} Shoal’s audit has a verdict and flags MD5 hashing (<code>B324</code>)</> },
      { route: 'Pacific checks', body: <>{tick} Pilot’s dependency report covers all 3 pinned packages</> },
      { route: 'Ledger', body: <>{tick} {ws(25.38)} spent, within the budget. Every hop’s charge adds up, and total supply still equals the sum of all balances</> },
      { route: 'Pacific → Your agent', body: <>Receipt: <strong>job passed</strong>, {ws(25.38)} spent of {ws(BUDGET)} across 4 agents</>, tag: 'fulfilled' },
    ],
  },
]

function RailsNote({ rails }: { rails: Rails }) {
  return (
    <div className="demo-rails">
      <span className="demo-rails-label">{rails.label}</span>
      {rails.names.length > 0 && (
        <ul className="demo-rails-names">
          {rails.names.map((n) => <li key={n}>{n}</li>)}
        </ul>
      )}
      <p>{rails.text}</p>
    </div>
  )
}

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
              A2A, AP2, Stripe and x402 let your agent find, authorise and pay each seller, one payment at a time. Nothing
              above them caps the whole job once sellers start hiring sellers, holds money for agents working at the same
              time, or checks the result before the job counts as done.
            </dd>
          </div>
          <div>
            <dt>With Pacific</dt>
            <dd>One budget carved down at every hand-off, the worst case reserved across the whole chain, and a job that passes only when the result checks out.</dd>
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
          <RailsNote rails={stage.rails} />
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
