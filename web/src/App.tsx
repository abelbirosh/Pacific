import { useEffect } from 'react'
import type { CSSProperties } from 'react'
import { CREDITS, STAGES } from './data'
import { AfterDiagram, BeforeDiagram } from './Diagrams'

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

function Header() {
  return (
    <header className="header">
      <a className="wordmark" href="#top">Pacific</a>
      <nav className="nav" aria-label="Sections">
        <a className="link" href="#before-after">Before and after</a>
        <a className="link" href="#what">What it is</a>
        <a className="link" href="#role">The role</a>
      </nav>
    </header>
  )
}

function Hero() {
  return (
    <section className="hero" id="top">
      <video className="hero-video" autoPlay muted loop playsInline preload="auto"
        poster={A + 'pacific-swell-poster.jpg'} aria-hidden="true">
        <source src={A + 'pacific-swell-720.mp4'} type="video/mp4" media="(max-width: 600px)" />
        <source src={A + 'pacific-swell-1080.mp4'} type="video/mp4" />
      </video>
      <Header />
      <div className="hero-copy">
        <h1 className="title lines">
          <span className="line">
            <span data-line style={i(0)}>
              Pacific x{' '}
              <span className="alpha">
                <span className="alpha-word">alpha</span>{' '}
                <span className="alpha-run">speedrun</span>
              </span>{' '}
              demo
            </span>
          </span>
        </h1>
        <p className="lede lines">
          <span className="line"><span data-line style={i(1)}>The infrastructure for agent-to-agent services</span></span>
        </p>
      </div>
    </section>
  )
}

function BeforeAfter() {
  return (
    <section className="row" id="before-after" aria-labelledby="ba-head">
      <h2 id="ba-head" className="sr-only">Before and after</h2>
      <figure className="tile ba-before" data-reveal>
        <figcaption className="ba-word">Before</figcaption>
        <BeforeDiagram />
      </figure>
      <figure className="tile ba-after" data-reveal style={i(1)}>
        <figcaption className="ba-word">After</figcaption>
        <AfterDiagram />
      </figure>
    </section>
  )
}

function What() {
  return (
    <section className="row" id="what" aria-labelledby="what-head">
      <div className="tile what-head" data-reveal>
        <h2 id="what-head" className="head">What Pacific is</h2>
      </div>
      <figure className="tile tile-picture what-picture shutter" data-reveal style={i(1)}>
        <img src={A + 'hms-dolphin-log-book-2.jpg'} width={1920} height={1032} loading="lazy"
          alt="A handwritten page from the log book of HMS Dolphin, 1760s, recording wind, weather and work aboard" />
      </figure>
    </section>
  )
}

function Role() {
  return (
    <section className="row" id="role" aria-labelledby="role-head">
      <ol className="stages">
        {STAGES.map((s, n) => (
          <li key={s} className="tile stage" data-reveal style={i(n)}>
            {n === 0 && <h2 id="role-head" className="head">The role it plays</h2>}
            <span className="stage-word">{s}</span>
          </li>
        ))}
      </ol>
    </section>
  )
}

function Demo() {
  return (
    <section className="row" id="demo" aria-labelledby="demo-head">
      <div className="tile demo-head" data-reveal>
        <h2 id="demo-head" className="head">The demo</h2>
      </div>
      <figure className="tile tile-picture demo-picture" data-reveal style={i(1)}>
        <img src={A + 'bathyscaphe-trieste-with-uss-lewis-de-535.jpg'} width={1920} height={1520} loading="lazy"
          alt="The bathyscaphe Trieste at the surface beside USS Lewis, before its dive into the Marianas Trench in 1960" />
      </figure>
      <div className="tile demo-slot" role="img" aria-label="Space for the live demo" data-reveal style={i(2)} />
    </section>
  )
}

function Footer() {
  return (
    <footer className="board">
      <div className="row">
        <p className="tile foot-mark" data-reveal>Pacific</p>
        <figure className="tile tile-picture foot-map shutter" data-reveal style={i(1)}>
          <img src={A + 'mariana-trench-map-1907.jpg'} width={1920} height={1529} loading="lazy"
            alt="Otto Krümmel's 1907 depth map of the Mariana Trench near Guam and Saipan" />
        </figure>
        <ul className="tile credits">
          {CREDITS.map((c) => (
            <li key={c.url}><a className="link" href={c.url}>{c.title}</a>, {c.by}, {c.licence}</li>
          ))}
        </ul>
        <a className="tile foot-action" href="#top">Back to the surface</a>
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
        {/* below the video the page is one board: flat tiles butted together, the gaps are the lines */}
        <div className="board">
          <BeforeAfter />
          <What />
          <Role />
          <Demo />
        </div>
      </main>
      <Footer />
    </>
  )
}
