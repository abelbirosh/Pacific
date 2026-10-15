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
const LAYERS = ['Discovery', 'Authentication', 'Transaction', 'Assurance']

const groupHeight = (n: number) => 36 + n * STEP
const chipY = (g: Group, k: number) => g.y + 34 + k * STEP
const wire = (n: number) => ({ '--i': n }) as CSSProperties

const ARROW = 9

/** A straight wire from a to b, stopping short so an arrowhead can sit with its tip on b. */
function arrowLine([x1, y1]: Pt, [x2, y2]: Pt) {
  const len = Math.hypot(x2 - x1, y2 - y1)
  const k = (len - ARROW + 1) / len
  return `M${x1},${y1} L${x1 + (x2 - x1) * k},${y1 + (y2 - y1) * k}`
}

/** The arrowhead at b, pointing along a→b. */
function arrowHead([x1, y1]: Pt, [x2, y2]: Pt) {
  const a = Math.atan2(y2 - y1, x2 - x1)
  const side = (s: number): Pt => [x2 - ARROW * Math.cos(a) + s * 4.5 * Math.sin(a), y2 - ARROW * Math.sin(a) - s * 4.5 * Math.cos(a)]
  const [l, r] = [side(1), side(-1)]
  return `${x2},${y2} ${l[0]},${l[1]} ${r[0]},${r[1]}`
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
      <rect className="dg-panel" x={x} y={group.y} width={w} height={groupHeight(group.items.length)} />
      <Label x={x + 12} y={group.y + 22} size={12}>{group.label}</Label>
      {group.items.map((item, k) => (
        <g key={item}>
          <rect className="dg-chip" x={x + 10} y={chipY(group, k)} width={w - 20} height={CHIP} />
          <Label x={x + w / 2} y={chipY(group, k) + 23} anchor="middle" size={item.length > 24 ? 12 : 13}>{item}</Label>
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
          <rect className="dg-panel" x={0} y={508} width={COL_W} height={44} />
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
const CUSTOM_BAR = { x: 156, y: 508, w: 250, h: 44 }

/* the providers, loose on the table: no grouping, each at its own angle */
const OPEN_MEDIA = 'Accessible web media'
const SHUT_MEDIA = 'Web media blocked to agents'
const LOOSE: { s: string; x: number; y: number; w: number; deg: number }[] = [
  { s: 'OSS', x: 452, y: 4, w: 72, deg: -7 },
  { s: 'Data storage', x: 538, y: 40, w: 102, deg: 6 },
  { s: 'Search API', x: 412, y: 100, w: 98, deg: 4 },
  { s: 'Voice-to-speech API', x: 470, y: 166, w: 162, deg: -5 },
  { s: OPEN_MEDIA, x: 410, y: 236, w: 168, deg: 6 },
  { s: SHUT_MEDIA, x: 440, y: 304, w: 196, deg: -4 },
  { s: 'Payment APIs', x: 520, y: 374, w: 112, deg: -9 },
  { s: 'Data APIs', x: 430, y: 448, w: 98, deg: 3 },
]
const looseAt = (name: string) => LOOSE.find((l) => l.s === name)!
/** Where a wire meets a loose chip: the middle of its left edge, turned with the chip. */
const looseEnd = (name: string): Pt => {
  const l = looseAt(name)
  return turn([l.x + l.w / 2, l.y + CHIP / 2], l.deg, [l.x, l.y + CHIP / 2])
}

type Wire = { from: Pt; to: Pt; blocked?: boolean }

export function BeforeDiagram() {
  const wires: Wire[] = []
  const toHub = (from: Pt, hub: Pt) => wires.push({ from, to: edge(hub, HUB_R, from) })
  const fromHub = (hub: Pt, to: Pt, blocked = false) => wires.push({ from: edge(hub, HUB_R, to), to, blocked })
  // personal agents reach for web search and GEO-optimized search
  for (let k = 0; k < 3; k++) { toHub(agentChip(0, k), WEB); toHub(agentChip(0, k), GEO) }
  // general agents lean on GEO-optimized search
  for (let k = 0; k < 3; k++) toHub(agentChip(1, k), GEO)
  // financial agents go through payment and trading software, and search the web too
  for (let k = 0; k < 2; k++) { toHub(agentChip(2, k), PAY); toHub(agentChip(2, k), WEB) }
  // and some skip the middle entirely with integrations of their own
  wires.push({ from: agentChip(0, 2), to: looseEnd('Search API') })
  wires.push({ from: agentChip(2, 0), to: looseEnd('Payment APIs') })
  // GEO-optimized search feeds web search
  wires.push({ from: edge(GEO, HUB_R, WEB), to: edge(WEB, HUB_R, GEO) })
  // web search fans out to everything on the table; the part of the web that turns agents away stops it at the door
  for (const l of LOOSE) fromHub(WEB, looseEnd(l.s), l.s === SHUT_MEDIA)
  fromHub(GEO, looseEnd(OPEN_MEDIA))
  fromHub(GEO, looseEnd('Search API'))
  fromHub(PAY, looseEnd('Payment APIs'))
  fromHub(PAY, looseEnd('Data APIs'))
  // custom agents wire into all three, by hand
  for (const [hub, dx] of [[WEB, 210], [GEO, 120], [PAY, 40]] as [Pt, number][]) {
    const from: Pt = [CUSTOM_BAR.x + dx, CUSTOM_BAR.y]
    wires.push({ from, to: edge(hub, HUB_R, from) })
  }
  // the crossed-out wire ends at an X just in front of the blocked chip
  const blocked = wires.find((w) => w.blocked)!
  const back = 22 / Math.hypot(blocked.to[0] - blocked.from[0], blocked.to[1] - blocked.from[1])
  const cross: Pt = [blocked.to[0] - (blocked.to[0] - blocked.from[0]) * back, blocked.to[1] - (blocked.to[1] - blocked.from[1]) * back]

  return (
    <svg className="diagram dg-before" viewBox={`0 0 ${W} ${H}`} role="img"
      aria-label="Before Pacific: personal, general, financial and custom agents each wire themselves to web search, GEO-optimized search, payment and trading software and straight to individual services, which lie scattered with no common interface. Some web media is accessible to agents; the rest is blocked to them.">
      {wires.map((w, n) => (
        <g key={n} style={wire(n)}>
          {w.blocked
            ? <path className="dg-wire" d={`M${w.from[0]},${w.from[1]} L${cross[0]},${cross[1]}`} pathLength={1} />
            : <>
                <path className="dg-wire" d={arrowLine(w.from, w.to)} pathLength={1} />
                <polygon className="dg-head" points={arrowHead(w.from, w.to)} />
              </>}
        </g>
      ))}
      <Agents customInColumn={false} />
      <g>
        <rect className="dg-panel" x={CUSTOM_BAR.x} y={CUSTOM_BAR.y} width={CUSTOM_BAR.w} height={CUSTOM_BAR.h} />
        <Label x={CUSTOM_BAR.x + CUSTOM_BAR.w / 2} y={CUSTOM_BAR.y + 27} anchor="middle">Custom agents</Label>
      </g>
      {([[WEB, 'Web\nsearch'], [GEO, 'GEO-optimized\nsearch'], [PAY, 'Payment and\ntrading software']] as [Pt, string][]).map(([c, t]) => (
        <g key={t}>
          <circle className="dg-hub" cx={c[0]} cy={c[1]} r={HUB_R} />
          <Label x={c[0]} y={c[1] - 3} anchor="middle" size={12}>{t}</Label>
        </g>
      ))}
      {LOOSE.map((l) => (
        <g key={l.s} className={l.s === SHUT_MEDIA ? 'dg-shut' : undefined} transform={`rotate(${l.deg} ${l.x + l.w / 2} ${l.y + CHIP / 2})`}>
          <rect className="dg-chip dg-loose" x={l.x} y={l.y} width={l.w} height={CHIP} />
          <Label x={l.x + l.w / 2} y={l.y + 23} anchor="middle">{l.s}</Label>
        </g>
      ))}
      <g className="dg-block" transform={`translate(${cross[0]} ${cross[1]})`}>
        <circle r={11} />
        <path d="M-5,-5 L5,5 M5,-5 L-5,5" />
      </g>
    </svg>
  )
}

/* ---------- after: agents talk to one layer, and the layer talks to the providers ---------- */

const SLAB = { x: 196, y: 0, w: 180, h: 552 }
const RIGHT_X = 420
const RIGHT_W = W - RIGHT_X
const PROVIDERS: Group[] = [
  { label: 'Service providers', y: 0, items: ['Data storage', 'Search API', 'Payment APIs', 'Voice-to-speech API'] },
  { label: 'Data providers', y: 228, items: ['Data sources written for people', 'Data sources written for agents', 'Data APIs'] },
  { label: 'Public providers', y: 412, items: ['Open source software', 'Custom agents'] },
]

export function AfterDiagram() {
  const mid = (g: Group) => g.y + groupHeight(g.items.length) / 2
  const left: number[] = [...AGENTS.map(mid), 530]
  const right: number[] = PROVIDERS.map(mid)
  const bandTop = 58
  const bandGap = 6
  const bandH = (SLAB.h - bandTop - 10 - bandGap * (LAYERS.length - 1)) / LAYERS.length

  return (
    <svg className="diagram dg-after" viewBox={`0 0 ${W} ${H}`} role="img"
      aria-label="After Pacific: every agent connects to one Pacific layer of discovery, authentication, transaction and assurance, which connects to service, data and public providers.">
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
        <rect x={SLAB.x} y={SLAB.y} width={SLAB.w} height={SLAB.h} />
        <Label x={SLAB.x + 18} y={36} size={24} weight={600}>Pacific</Label>
        {LAYERS.map((l, k) => {
          const y = bandTop + k * (bandH + bandGap)
          return (
            <g key={l}>
              <rect className="dg-band" x={SLAB.x + 10} y={y} width={SLAB.w - 20} height={bandH} />
              <Label x={SLAB.x + 24} y={y + 30} size={16} weight={600}>{l}</Label>
            </g>
          )
        })}
      </g>
      {PROVIDERS.map((g) => <Stack key={g.label} x={RIGHT_X} w={RIGHT_W} group={g} />)}
    </svg>
  )
}
