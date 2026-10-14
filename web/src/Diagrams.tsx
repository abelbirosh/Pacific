import type { CSSProperties } from 'react'

/*
 * The before-and-after from pacific_infra.excalidraw, redrawn in the page's colours.
 * Both views share one coordinate system (640 x 560) and the same agent column on the left,
 * so the only thing that changes between them is what sits between agents and providers.
 */

type Pt = [number, number]
type Group = { label: string; y: number; items: string[] }

const W = 640
const H = 560
const CHIP = 36
const STEP = 44
const COL_W = 124

const AGENTS: Group[] = [
  { label: 'Personal agents', y: 0, items: ['Muse', 'Dots', 'Grok'] },
  { label: 'General agents', y: 184, items: ['Claude', 'ChatGPT', 'Gemini'] },
  { label: 'Financial agents', y: 368, items: ['Coinbase', 'Capitalise AI'] },
]
const SERVICES = ['OSS', 'Data storage', 'Search API', 'Payment APIs', 'Voice-to-speech API']
const DATA = ['Sources written for people', 'Sources written for agents', 'Data APIs']
const LAYERS = ['Discovery', 'Authentication', 'Transaction', 'Assurance']

const groupHeight = (n: number) => 36 + n * STEP
const chipY = (g: Group, k: number) => g.y + 34 + k * STEP
const wire = (n: number) => ({ '--i': n }) as CSSProperties

/** A soft curve from a to b that leaves and arrives horizontally. */
function curve([x1, y1]: Pt, [x2, y2]: Pt) {
  const dx = Math.max(Math.abs(x2 - x1) * 0.5, 24)
  return `M${x1},${y1} C${x1 + dx},${y1} ${x2 - dx},${y2} ${x2},${y2}`
}

/** The point on a circle's edge facing p. */
function edge(c: Pt, r: number, p: Pt): Pt {
  const a = Math.atan2(p[1] - c[1], p[0] - c[0])
  return [c[0] + r * Math.cos(a), c[1] + r * Math.sin(a)]
}

/** Rotate p by deg around c, to find chips inside the tilted service card. */
function turn(c: Pt, deg: number, p: Pt): Pt {
  const a = (deg * Math.PI) / 180
  const [x, y] = [p[0] - c[0], p[1] - c[1]]
  return [c[0] + x * Math.cos(a) - y * Math.sin(a), c[1] + x * Math.sin(a) + y * Math.cos(a)]
}

function Label({ x, y, children, anchor = 'start', size = 13, weight = 500 }: {
  x: number; y: number; children: string; anchor?: 'start' | 'middle'; size?: number; weight?: number
}) {
  const lines = children.split('\n')
  return (
    <text x={x} y={y} textAnchor={anchor} fontSize={size} fontWeight={weight}>
      {lines.map((l, n) => <tspan key={l} x={x} dy={n === 0 ? 0 : size * 1.2}>{l}</tspan>)}
    </text>
  )
}

/** A labelled column of chips: agents on the left, providers on the right. */
function Stack({ x, w, group }: { x: number; w: number; group: Group }) {
  return (
    <g>
      <rect className="dg-panel" x={x} y={group.y} width={w} height={groupHeight(group.items.length)} rx={14} />
      <Label x={x + 12} y={group.y + 22} size={12}>{group.label}</Label>
      {group.items.map((item, k) => (
        <g key={item}>
          <rect className="dg-chip" x={x + 10} y={chipY(group, k)} width={w - 20} height={CHIP} rx={9} />
          <Label x={x + w / 2} y={chipY(group, k) + 23} anchor="middle">{item}</Label>
        </g>
      ))}
    </g>
  )
}

function Agents({ customInColumn }: { customInColumn: boolean }) {
  return (
    <g>
      {AGENTS.map((g) => <Stack key={g.label} x={0} w={COL_W} group={g} />)}
      {customInColumn && (
        <g>
          <rect className="dg-panel" x={0} y={508} width={COL_W} height={44} rx={14} />
          <Label x={COL_W / 2} y={535} anchor="middle">Custom agents</Label>
        </g>
      )}
    </g>
  )
}

const agentChip = (g: number, k: number): Pt => [COL_W - 10, chipY(AGENTS[g], k) + CHIP / 2]

/* ---------- before: every agent wires itself to every service, through whatever is in reach ---------- */

const WEB: Pt = [282, 128]
const GEO: Pt = [252, 280]
const PAY: Pt = [280, 424]
const HUB_R = 56
const CARD = { x: 418, y: 20, w: 216, h: 170, deg: -10 }
const CARD_C: Pt = [CARD.x + CARD.w / 2, CARD.y + CARD.h / 2]
const DATA_G: Group = { label: 'Data providers', y: 250, items: DATA }
const CUSTOM_BAR = { x: 156, y: 508, w: 250, h: 44 }

/* service chips inside the card: two per row, the last one full width */
const SERVICE_CHIPS = SERVICES.map((s, k) => {
  const row = Math.floor(k / 2)
  const full = k === SERVICES.length - 1
  const w = full ? CARD.w - 20 : (CARD.w - 28) / 2
  const x = CARD.x + 10 + (full || k % 2 === 0 ? 0 : w + 8)
  return { s, x, y: CARD.y + 34 + row * STEP, w }
})

export function BeforeDiagram() {
  const wires: [Pt, Pt][] = []
  const toHub = (from: Pt, hub: Pt) => wires.push([from, edge(hub, HUB_R, from)])
  // personal agents reach for web search and GEO-optimized search
  for (let k = 0; k < 3; k++) { toHub(agentChip(0, k), WEB); toHub(agentChip(0, k), GEO) }
  // general agents lean on GEO-optimized search
  for (let k = 0; k < 3; k++) toHub(agentChip(1, k), GEO)
  // financial agents go through payment and trading software, and search the web too
  for (let k = 0; k < 2; k++) { toHub(agentChip(2, k), PAY); toHub(agentChip(2, k), WEB) }
  // GEO-optimized search feeds web search
  wires.push([edge(GEO, HUB_R, WEB), edge(WEB, HUB_R, GEO)])
  // web search fans out to every service and every data source
  for (const c of SERVICE_CHIPS) {
    const end = turn(CARD_C, CARD.deg, [c.x, c.y + CHIP / 2])
    wires.push([edge(WEB, HUB_R, end), end])
  }
  DATA.forEach((_, k) => {
    const end: Pt = [CARD.x + 10, chipY(DATA_G, k) + CHIP / 2]
    wires.push([edge(WEB, HUB_R, end), end])
  })
  // custom agents wire into all three, by hand
  for (const [hub, dx] of [[WEB, 210], [GEO, 120], [PAY, 40]] as [Pt, number][]) {
    const from: Pt = [CUSTOM_BAR.x + dx, CUSTOM_BAR.y]
    wires.push([from, edge(hub, HUB_R, from)])
  }

  return (
    <svg className="diagram dg-before" viewBox={`0 0 ${W} ${H}`} role="img"
      aria-label="Before Pacific: personal, general, financial and custom agents each wire themselves to web search, GEO-optimized search and payment and trading software, which fan out to service providers and data providers in a tangle.">
      {wires.map(([a, b], n) => <path key={n} className="dg-wire" d={curve(a, b)} pathLength={1} style={wire(n)} />)}
      <Agents customInColumn={false} />
      <g>
        <rect className="dg-panel" x={CUSTOM_BAR.x} y={CUSTOM_BAR.y} width={CUSTOM_BAR.w} height={CUSTOM_BAR.h} rx={14} />
        <Label x={CUSTOM_BAR.x + CUSTOM_BAR.w / 2} y={CUSTOM_BAR.y + 27} anchor="middle">Custom agents</Label>
      </g>
      {([[WEB, 'Web\nsearch'], [GEO, 'GEO-optimized\nsearch'], [PAY, 'Payment and\ntrading software']] as [Pt, string][]).map(([c, t]) => (
        <g key={t}>
          <circle className="dg-hub" cx={c[0]} cy={c[1]} r={HUB_R} />
          <Label x={c[0]} y={c[1] - 3} anchor="middle" size={12}>{t}</Label>
        </g>
      ))}
      <g transform={`rotate(${CARD.deg} ${CARD_C[0]} ${CARD_C[1]})`}>
        <rect className="dg-panel" x={CARD.x} y={CARD.y} width={CARD.w} height={CARD.h} rx={14} />
        <Label x={CARD.x + 12} y={CARD.y + 22} size={12}>Service providers</Label>
        {SERVICE_CHIPS.map((c) => (
          <g key={c.s}>
            <rect className="dg-chip" x={c.x} y={c.y} width={c.w} height={CHIP} rx={9} />
            <Label x={c.x + c.w / 2} y={c.y + 23} anchor="middle">{c.s}</Label>
          </g>
        ))}
      </g>
      <Stack x={CARD.x} w={CARD.w} group={DATA_G} />
    </svg>
  )
}

/* ---------- after: agents talk to one layer, and the layer talks to the providers ---------- */

const SLAB = { x: 196, y: 0, w: 200, h: 552 }
const RIGHT_X = 452
const RIGHT_W = W - RIGHT_X
const SERVICE_G: Group = { label: 'Service providers', y: 0, items: SERVICES }
const DATA_AFTER: Group = { label: 'Data providers', y: 276, items: DATA }

export function AfterDiagram() {
  const mid = (g: Group) => g.y + groupHeight(g.items.length) / 2
  const left: number[] = [...AGENTS.map(mid), 530]
  const right: number[] = [mid(SERVICE_G), mid(DATA_AFTER)]
  const bandTop = 58
  const bandGap = 6
  const bandH = (SLAB.h - bandTop - 10 - bandGap * (LAYERS.length - 1)) / LAYERS.length

  return (
    <svg className="diagram dg-after" viewBox={`0 0 ${W} ${H}`} role="img"
      aria-label="After Pacific: every agent connects to one Pacific layer of discovery, authentication, transaction and assurance, which connects to service providers and data providers.">
      {left.map((y, n) => (
        <g key={y}>
          <path className="dg-wire" d={`M${COL_W},${y} L${SLAB.x},${y}`} pathLength={1} style={wire(n)} />
          <circle className="dg-end" cx={SLAB.x} cy={y} r={4} />
        </g>
      ))}
      {right.map((y, n) => (
        <g key={y}>
          <path className="dg-wire" d={`M${SLAB.x + SLAB.w},${y} L${RIGHT_X},${y}`} pathLength={1} style={wire(n + left.length)} />
          <circle className="dg-end" cx={RIGHT_X} cy={y} r={4} />
        </g>
      ))}
      <Agents customInColumn />
      <g className="dg-slab">
        <rect x={SLAB.x} y={SLAB.y} width={SLAB.w} height={SLAB.h} rx={18} />
        <Label x={SLAB.x + 18} y={36} size={24} weight={600}>Pacific</Label>
        {LAYERS.map((l, k) => {
          const y = bandTop + k * (bandH + bandGap)
          return (
            <g key={l}>
              <rect className="dg-band" x={SLAB.x + 10} y={y} width={SLAB.w - 20} height={bandH} rx={10} />
              <Label x={SLAB.x + 24} y={y + 30} size={16} weight={600}>{l}</Label>
            </g>
          )
        })}
      </g>
      <Stack x={RIGHT_X} w={RIGHT_W} group={SERVICE_G} />
      <Stack x={RIGHT_X} w={RIGHT_W} group={DATA_AFTER} />
    </svg>
  )
}
