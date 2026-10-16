/*
 * First customers and economics, compressed from the business and scaling plan (October 2026). Only the numbers and
 * the structure are kept: who pays and how much, what is free and what is charged, and how the network grows into
 * hosting agents and a marketplace for them. Spend ranges and the 5% take rate are hypotheses.
 */

const CUSTOMERS = [
  { who: 'AI startups', what: 'building research, enrichment and automation agents', spend: '$500–$5K', why: 'Less integration work, services picked on the fly' },
  { who: 'Independent agent developers', what: 'building on their own', spend: '$1–$30', why: 'Free discovery, minimal setup' },
  { who: 'Enterprises', what: 'deploying production agents', spend: '$10K+', why: 'One point of access, spending controls, an audit trail' },
]

const REVENUE: { group: string; items: [string, string][]; expansion?: boolean }[] = [
  { group: 'Free', items: [['Discovery and publishing', 'Free'], ['Integration and routing', 'Free allowance']] },
  {
    group: 'Paid now',
    items: [
      ['Facilitated transactions', '5% target take rate'],
      ['Prepaid credits', 'Same rate, budgets set per agent'],
      ['Pay-as-you-go', 'Metered, one consolidated bill'],
      ['Request Integration', 'Metered routing'],
    ],
  },
  {
    group: 'Expansion',
    expansion: true,
    items: [['Hosted agents and endpoints', 'Usage-based compute and storage'], ['Agent marketplace', 'Commission on sales']],
  },
]

const PHASES = [
  {
    name: 'Validate the network',
    does: 'A curated catalog, self-hosted endpoint registration, integration requests and limited managed transactions.',
  },
  {
    name: 'Scale the marketplace',
    does: 'Self-service publishing, automated validation of new services, and smarter routing between providers.',
  },
  {
    name: 'Host and sell agents',
    does: 'Agents and endpoints run on Pacific’s servers, and a marketplace lets third parties sell agents, workflows and services to the network.',
    target: 'Pacific’s agent host already runs five agents that sell skills to each other in this prototype',
    expansion: true,
  },
]

export function Economics() {
  return (
    <div className="econ">
      <div className="econ-block" data-reveal>
        <h3 className="econ-head">First customers</h3>
        <div className="econ-distribution">
          <p className="econ-spend">Discovery is free</p>
          <p className="econ-who">
            <strong>That is the distribution strategy.</strong> Providers, from enterprise endpoints to independent
            developers, list once and reach every agent on the network, with a way to get paid.
          </p>
        </div>
        <ol className="econ-customers">
          {CUSTOMERS.map((c) => (
            <li key={c.who}>
              <p className="econ-spend">{c.spend} <span>/ month</span></p>
              <p className="econ-who"><strong>{c.who}</strong> {c.what}</p>
              <p className="econ-why">{c.why}</p>
            </li>
          ))}
        </ol>
      </div>

      <div className="econ-split">
        <div className="econ-block" data-reveal>
          <h3 className="econ-head">Revenue model</h3>
          <div className="econ-revenue">
            {REVENUE.map((r) => (
              <div key={r.group} className={r.expansion ? 'is-expansion' : undefined}>
                <h4 className="econ-group">{r.group}</h4>
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
          <h3 className="econ-head">Scaling</h3>
          <ol className="econ-phases">
            {PHASES.map((p) => (
              <li key={p.name} className={p.expansion ? 'is-expansion' : undefined}>
                <p className="econ-phase-name">{p.name}</p>
                <p className="econ-phase-does">{p.does}</p>
                {p.target && <p className="econ-phase-target">{p.target}</p>}
              </li>
            ))}
          </ol>
        </div>
      </div>
    </div>
  )
}
