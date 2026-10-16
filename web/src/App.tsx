import { useEffect } from 'react'
import type { CSSProperties } from 'react'
import { AfterDiagram, BeforeDiagram } from './Diagrams'
import { Demo } from './Demo'
import { Economics } from './Economics'

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

/**
 * Draw the diagrams' wires when they are really on screen, on every load. Unlike the tile reveal there is
 * no early fallback, so the drawing is never spent before anyone sees it.
 */
function useDrawDiagrams() {
  useEffect(() => {
    const diagrams = document.querySelectorAll<SVGSVGElement>('.diagram')
    if (!('IntersectionObserver' in window)) {
      diagrams.forEach((d) => d.classList.add('is-drawn'))
      return
    }
    const io = new IntersectionObserver(
      (entries) => {
        for (const e of entries) {
          if (e.isIntersecting) {
            e.target.classList.add('is-drawn')
            io.unobserve(e.target)
          }
        }
      },
      { threshold: 0.35 },
    )
    diagrams.forEach((d) => io.observe(d))
    return () => io.disconnect()
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
 * Dim the sea as the hero scrolls away: one flat layer over the fixed footage whose opacity follows the scroll, so
 * every chapter below sits on the same moving water with no edges between them.
 */
function useDimSeaOnScroll() {
  useEffect(() => {
    const dim = document.querySelector<HTMLElement>('.sea-dim')
    const hero = document.querySelector<HTMLElement>('.hero')
    if (!dim || !hero) return
    // scroll events already arrive once per frame; one opacity write each is cheap
    const update = () => {
      dim.style.opacity = String(Math.min(Math.max(window.scrollY / (hero.offsetHeight * 0.85), 0), 1))
    }
    update()
    window.addEventListener('scroll', update, { passive: true })
    window.addEventListener('resize', update)
    return () => {
      window.removeEventListener('scroll', update)
      window.removeEventListener('resize', update)
    }
  }, [])
}

function Header() {
  return (
    <header className="header">
      <a className="wordmark" href="#top">Pacific</a>
      <nav className="nav" aria-label="Sections">
        <a className="link" href="#before-after">Before and After</a>
        <a className="link" href="#demo">Demo</a>
        <a className="link" href="#economics">Economics</a>
        <a className="link" href="#future">Future</a>
      </nav>
    </header>
  )
}

function Hero() {
  return (
    <section className="hero" id="top">
      <video className="hero-video" autoPlay muted loop playsInline preload="auto"
        poster={A + 'pacific-background-poster.jpg'} aria-hidden="true">
        {/* phones get 720p, large and high-density screens the 4K master, everything between 1080p */}
        <source src={A + 'pacific-background.mp4'} type="video/mp4" media="(max-width: 600px)" />
        <source src={A + 'pacific-background-4k.mp4'} type="video/mp4" media="(min-width: 1024px)" />
        <source src={A + 'pacific-background-hd.mp4'} type="video/mp4" />
      </video>
      <div className="sea-dim" aria-hidden="true" />
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

function Chapters() {
  return (
    <>
      <section className="chapter interaction" id="demo" aria-labelledby="interaction-head">
        <h2 id="interaction-head" data-reveal>Agent-to-agent Interaction Demo</h2>
        <Demo />
      </section>
      <section className="chapter economics" id="economics" aria-labelledby="economics-head">
        <h2 id="economics-head" data-reveal>First customers and economics</h2>
        <Economics />
      </section>
      <section className="chapter future" id="future" aria-labelledby="future-head">
        <h2 id="future-head" data-reveal>Future</h2>
      </section>
    </>
  )
}

export default function App() {
  useReveal()
  useDrawDiagrams()
  usePauseLoopsForReducedMotion()
  useDimSeaOnScroll()
  return (
    <>
      <main>
        <Hero />
        {/* The sea runs on under every chapter, each on its own flat field of colour. */}
        <div className="board">
          <BeforeAfter />
        </div>
        <Chapters />
      </main>
    </>
  )
}
