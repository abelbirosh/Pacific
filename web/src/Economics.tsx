/*
 * First customers and economics, compressed from the business and scaling plan (October 2026). Only the numbers and
 * the structure are kept. Spend ranges and the 5% take rate are hypotheses, and the page says so.
 */

const CUSTOMERS = [
  { tag: 'First', who: 'AI startups', what: 'building research, enrichment and automation agents', spend: '$500–$5K', unit: '/ month', why: 'Less integration work, services picked on the fly' },
  { tag: 'Second', who: 'Independent agent developers', what: 'building on their own', spend: '$1–$30', unit: '/ month', why: 'Free discovery, minimal setup' },
  { tag: 'Third', who: 'Enterprises', what: 'deploying production agents', spend: '$10K+', unit: '/ month', why: 'One point of access, spending controls, an audit trail' },
  { tag: 'Supply', who: 'Providers', what: 'enterprise endpoints and independent developers', spend: 'Free', unit: 'to list', why: 'Distribution and a way to get paid' },
]

const REVENUE: { group: string; items: [string, string][]; expansion?: boolean }[] = [
  { group: 'Free', items: [['Discovery and publishing', 'Free'], ['Integration and routing', 'Free allowance']] },
  {
    group: 'Paid now',
    items: [
      ['Facilitated transactions', '5% target take rate'],
      ['Prepaid credits', 'Same rate, budgets set per agent'],
      ['Pay-as-you-go', 'Metered, one consolidated bill'],
      ['Bring your own keys', 'Metered routing after the allowance'],
    ],
  },
  {
    group: 'Expansion',
    expansion: true,
    items: [['Hosted agents and endpoints', 'Usage-based compute and storage'], ['Enterprise governance', 'Subscription or contract']],
  },
]

// a 5% fee on top of the provider's price: monthly GMV, monthly revenue, annual revenue
const LADDER: [string, string, string][] = [
  ['$10K', '$500', '$6K'],
  ['$100K', '$5K', '$60K'],
  ['$1M', '$50K', '$600K'],
  ['$10M', '$500K', '$6M'],
  ['$100M', '$5M', '$60M'],
]

const PHASES = [
  {
    when: 'Months 0–3',
    name: 'Validate the network',
    does: 'A curated catalog, self-hosted endpoint registration, bring-your-own-keys access and limited managed transactions.',
    target: '10 paying production developers · 10–20 provider integrations',
  },
  {
    when: 'Months 3–12',
    name: 'Scale the marketplace',
    does: 'Self-service publishing, automated validation of new services, and smarter routing between providers.',
    target: 'Positive contribution margin per transaction · growing wallet-funded GMV',
  },
  {
    when: 'Year 2+',
    name: 'Host the agents',
    does: 'Agents and endpoints run on Pacific’s servers: hosting, deployment, scheduling, execution environments and enterprise governance.',
    target: 'Pacific’s agent host already runs five agents that sell skills to each other in this prototype',
    expansion: true,
  },
]

export function Economics() {
  return (
    <div className="econ">
      <div className="econ-block" data-reveal>
        <h3 className="econ-head">First customers</h3>
        <ul className="econ-customers">
          {CUSTOMERS.map((c) => (
            <li key={c.who} className={c.tag === 'Supply' ? 'is-supply' : undefined}>
              <span className="econ-tag">{c.tag}</span>
              <p className="econ-spend">{c.spend} <span>{c.unit}</span></p>
              <p className="econ-who"><strong>{c.who}</strong> {c.what}</p>
              <p className="econ-why">{c.why}</p>
            </li>
          ))}
        </ul>
        <p className="econ-fine">
          The first 10 paying customers: small AI startups already using three or more external APIs, starting with research
          and data services, where providers can be compared objectively. Spend ranges are hypotheses, not measured averages.
        </p>
      </div>

      <div className="econ-split">
        <div className="econ-block" data-reveal>
          <h3 className="econ-head">Revenue model</h3>
          <div className="econ-revenue">
            {REVENUE.map((r) => (
              <div key={r.group} className={r.expansion ? 'is-expansion' : undefined}>
                <span className="econ-tag">{r.group}</span>
                <dl>
                  {r.items.map(([k, v]) => (
                    <div key={k}><dt>{k}</dt><dd>{v}</dd></div>
                  ))}
                </dl>
              </div>
            ))}
          </div>
        </div>

        <div className="econ-block" data-reveal>
          <h3 className="econ-head">Unit economics at 5%</h3>
          <table className="econ-ladder">
            <thead>
              <tr><th scope="col">Monthly GMV</th><th scope="col">Monthly revenue</th><th scope="col">Annual revenue</th></tr>
            </thead>
            <tbody>
              {LADDER.map(([gmv, month, year]) => (
                <tr key={gmv}><td>{gmv}</td><td>{month}</td><td>{year}</td></tr>
              ))}
            </tbody>
          </table>
          <p className="econ-callout"><strong>$20M</strong> of annual GMV is only <strong>$1M</strong> of revenue. Scale needs volume, higher-margin services, or both.</p>
          <p className="econ-fine">
            Gross fee revenue, before payment processing, settlement, hosting and operations. Costs stay low through prepaid
            credits and consolidated billing instead of per-call payments, and providers host their own compute at first.
          </p>
        </div>
      </div>

      <div className="econ-block" data-reveal>
        <h3 className="econ-head">Scaling</h3>
        <ol className="econ-phases">
          {PHASES.map((p, n) => (
            <li key={p.when} className={p.expansion ? 'is-expansion' : undefined}>
              <span className="econ-tag">{String(n + 1).padStart(2, '0')} · {p.when}</span>
              <p className="econ-phase-name">{p.name}</p>
              <p className="econ-phase-does">{p.does}</p>
              <p className="econ-phase-target">{p.target}</p>
            </li>
          ))}
        </ol>
      </div>
    </div>
  )
}
