import { useEffect } from 'react'
import type { CSSProperties } from 'react'
import { CREDITS, STAGES } from './data'

const A = '/assets/'
const i = (n: number) => ({ '--i': n }) as CSSProperties

/** Reveal [data-reveal] once as it enters the viewport; never leaves content hidden. */
function useReveal() {
  useEffect(() => {
    const targets = document.querySelectorAll<HTMLElement>('[data-reveal]')
    if (!('IntersectionObserver' in window)) {
      targets.forEach((el) => el.classList.add('is-in'))
      return
    }
    const io = new IntersectionObserver(
      (entries) => {
        for (const e of entries) {
          if (e.isIntersecting) {
            e.target.classList.add('is-in')
            io.unobserve(e.target)
          }
        }
      },
      { rootMargin: '0px 0px -8% 0px', threshold: 0.05 },
    )
    targets.forEach((el) => io.observe(el))
    const safety = window.setTimeout(() => targets.forEach((el) => el.classList.add('is-in')), 4000)
    return () => {
      io.disconnect()
      window.clearTimeout(safety)
    }
  }, [])
}

/** A silent loop is decoration: under reduced motion, stop it on its poster frame. */
function usePauseLoopsForReducedMotion() {
  useEffect(() => {
    const mq = window.matchMedia('(prefers-reduced-motion: reduce)')
    const apply = () =>
      document.querySelectorAll('video').forEach((v) => (mq.matches ? v.pause() : v.play().catch(() => {})))
    apply()
    mq.addEventListener('change', apply)
    return () => mq.removeEventListener('change', apply)
  }, [])
}

/**
 * The edge where one depth of water meets the next: two swells in the next chapter's colour,
 * drawn twice as wide as the screen so they can drift sideways without a seam.
 */
function Swell({ into }: { into: string }) {
  const front = 'M0,64 C180,104 360,24 540,64 C720,104 900,24 1080,64 C1260,104 1440,24 1620,64 C1800,104 1980,24 2160,64 C2340,104 2520,24 2700,64 C2790,84 2835,74 2880,64 L2880,140 L0,140 Z'
  const back = 'M0,84 C240,44 480,116 720,80 C960,44 1200,116 1440,80 C1680,44 1920,116 2160,80 C2400,44 2640,116 2880,80 L2880,140 L0,140 Z'
  return (
    <div className="swell" aria-hidden="true" style={{ '--into': `var(${into})` } as CSSProperties}>
      <svg className="swell-back" viewBox="0 0 2880 140" preserveAspectRatio="none"><path d={back} /></svg>
      <svg className="swell-front" viewBox="0 0 2880 140" preserveAspectRatio="none"><path d={front} /></svg>
    </div>
  )
}

function Header() {
  return (
    <header className="header">
      <a className="wordmark" href="#top">Pacific</a>
      <nav className="nav" aria-label="Sections">
        <a className="link" href="#what">What it is</a>
        <a className="link" href="#role">The role</a>
        <a className="link" href="#before-after">Before and after</a>
      </nav>
      <a className="button" href="#demo">See the demo</a>
    </header>
  )
}

function Hero() {
  return (
    <section className="chapter hero" id="top">
      <Header />
      <div className="hero-grid">
        <h1 className="title lines">
          <span className="line"><span data-line style={i(0)}>Pacific x Alpha</span></span>
          <span className="line"><span data-line style={i(1)}>Speedrun demo</span></span>
        </h1>
        <div className="current" aria-label="Pictures of the Pacific">
          <figure className="pebble p-wave" data-rise style={i(1)}>
            <img src={A + 'under-the-wave-off-kanagawa-kanagawa-oki.jpg'} width={1920} height={1313}
              alt="Hokusai's woodblock print of a great wave curling over three boats, Mount Fuji small in the distance" />
          </figure>
          <figure className="pebble p-clouds" data-rise style={i(2)}>
            <img src={A + 'iss034e016601-stratocumulus-clouds-pacific-ocean.jpg'} width={1920} height={1277}
              alt="Stratocumulus clouds over the Pacific Ocean seen from the International Space Station" />
          </figure>
          <figure className="pebble p-loop" data-rise style={i(3)}>
            <video autoPlay muted loop playsInline preload="metadata"
              poster={A + 'gravity-waves-propagating-through-the-stratocumulus-layer-poster.jpg'}
              aria-label="Satellite loop of gravity waves rippling through clouds over the South Pacific">
              <source src={A + 'gravity-waves-propagating-through-the-stratocumulus-layer.webm'} type="video/webm" />
            </video>
          </figure>
          <figure className="pebble p-chart" data-rise style={i(4)}>
            <img src={A + 'a-chart-of-the-discoveries-made-by.jpg'} width={1325} height={1600}
              alt="Engraved eighteenth-century chart of the North and South Pacific from Captain Cook's voyages" />
          </figure>
        </div>
      </div>
      <Swell into="--foam" />
    </section>
  )
}

function What() {
  return (
    <section className="chapter what" id="what" aria-labelledby="what-head">
      <div className="what-grid">
        <h2 id="what-head" className="head" data-reveal>What Pacific is</h2>
        <figure className="pebble p-log shutter" data-reveal>
          <img src={A + 'hms-dolphin-log-book-2.jpg'} width={1920} height={1032} loading="lazy"
            alt="A handwritten page from the log book of HMS Dolphin, 1760s, recording wind, weather and work aboard" />
        </figure>
      </div>
      <Swell into="--ocean" />
    </section>
  )
}

function Role() {
  return (
    <section className="chapter role" id="role" aria-labelledby="role-head">
      <h2 id="role-head" className="head" data-reveal>The role it plays</h2>
      <div className="stream">
        <svg className="stream-line" viewBox="0 0 1200 240" preserveAspectRatio="none" aria-hidden="true">
          <path d="M0,196 C70,196 90,150 150,150 S330,70 450,70 S630,170 750,170 S930,70 1050,70 S1160,110 1200,104" />
        </svg>
        <ol className="stops">
          {STAGES.map((s, n) => (
            <li key={s} className="stop" data-reveal style={i(n)}>
              <span className="stop-dot" aria-hidden="true" />
              <span className="stop-label">{s}</span>
            </li>
          ))}
        </ol>
      </div>
      <Swell into="--deep" />
    </section>
  )
}

function BeforeAfter() {
  return (
    <section className="chapter before-after" id="before-after" aria-labelledby="ba-head">
      <h2 id="ba-head" className="sr-only">Before and after</h2>
      <svg className="tide tide-wide" viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden="true">
        <path d="M0,0 L54,0 C44,14 60,30 52,48 C44,66 62,82 50,100 L0,100 Z" />
      </svg>
      <svg className="tide tide-narrow" viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden="true">
        <path d="M0,0 L100,0 L100,48 C78,58 64,40 44,50 C26,59 14,46 0,52 Z" />
      </svg>
      <div className="ba-half ba-before" data-reveal>
        <p className="ba-word">Before</p>
      </div>
      <div className="ba-half ba-after" data-reveal style={i(1)}>
        <p className="ba-word">After</p>
      </div>
      <Swell into="--navy" />
    </section>
  )
}

function Demo() {
  return (
    <section className="chapter demo" id="demo" aria-labelledby="demo-head">
      <h2 id="demo-head" className="head" data-reveal>The demo</h2>
      <div className="stage" data-reveal>
        <figure className="pebble p-trieste">
          <img src={A + 'bathyscaphe-trieste-with-uss-lewis-de-535.jpg'} width={1920} height={1520} loading="lazy"
            alt="The bathyscaphe Trieste at the surface beside USS Lewis, before its dive into the Marianas Trench in 1960" />
        </figure>
        <div className="stage-slot" role="img" aria-label="Space for the live demo" />
      </div>
      <Swell into="--trench" />
    </section>
  )
}

function Footer() {
  return (
    <footer className="chapter trench">
      <div className="trench-grid">
        <p className="trench-mark" data-reveal>Pacific</p>
        <figure className="pebble p-map shutter" data-reveal>
          <img src={A + 'mariana-trench-map-1907.jpg'} width={1920} height={1529} loading="lazy"
            alt="Otto Krümmel's 1907 depth map of the Mariana Trench near Guam and Saipan" />
        </figure>
      </div>
      <div className="trench-foot">
        <ul className="credits">
          {CREDITS.map((c) => (
            <li key={c.url}><a className="link" href={c.url}>{c.title}</a>, {c.by}, {c.licence}</li>
          ))}
        </ul>
        <a className="button button-light" href="#top">Back to the surface</a>
      </div>
    </footer>
  )
}

export default function App() {
  useReveal()
  usePauseLoopsForReducedMotion()
  return (
    <>
      <main>
        <Hero />
        <What />
        <Role />
        <BeforeAfter />
        <Demo />
      </main>
      <Footer />
    </>
  )
}
