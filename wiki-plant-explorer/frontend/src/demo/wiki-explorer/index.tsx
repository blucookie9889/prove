import { useEffect, useRef, useState, type ReactNode } from 'react'
import { Bar, Connector, Fly, Phone, Stage, States, Swap, Window, box, type Act, type Layout } from '../kit'
import taro from './taro.jpg'
import lena from './lena.jpg'
import anthocyanins from './anthocyanins.jpg'
import lotus from './lotus.jpg'
import permafrost from './permafrost.jpg'
import infrared from './infrared.jpg'
import './wx.css'

// Wiki Explorer's flow: a detail of the picture opens a door to a concept, and
// the door's picture becomes the next picture. A taro leaf leads to Murray's
// law and the Lena delta; the delta's channels lead back to the leaf's veins.

type Door = { leap: string; title: string; hook: string; thumb: string; dot: [number, number]; box: [number, number, number, number] }
type Page = { img: string; seeing: string; credit: string; leap: string; title: string; hook: string; doors: Door[] }

const PAGES: Record<'leaf' | 'delta', Page> = {
  leaf: {
    img: taro,
    seeing: 'Il rovescio di una foglia di taro, in controluce.',
    credit: 'Avenue · CC BY-SA 3.0',
    leap: 'altrove · i canali del delta',
    title: 'Nervatura fogliare',
    hook: 'Una rete che porta acqua a ogni cellula, ramificandosi come i canali di un delta.',
    doors: [
      { leap: 'dentro', title: 'Antocianine', hook: 'Il viola delle nervature vira dal rosso al blu cambiando il pH.', thumb: anthocyanins, dot: [0.69, 0.86], box: [0.63, 0.74, 0.75, 0.98] },
      { leap: 'perché', title: 'Effetto loto', hook: "La foglia è superidrofobica: l'acqua rotola via e porta con sé lo sporco.", thumb: lotus, dot: [0.22, 0.24], box: [0.1, 0.1, 0.34, 0.38] },
      { leap: 'altrove', title: 'Legge di Murray', hook: 'Il cubo del raggio del ramo padre è la somma dei cubi dei rami figli.', thumb: lena, dot: [0.68, 0.56], box: [0.6, 0.46, 0.76, 0.66] },
    ],
  },
  delta: {
    img: lena,
    seeing: 'Il delta della Lena visto da Landsat, in falsi colori.',
    credit: 'NASA, Landsat 7 · Pubblico dominio',
    leap: 'altrove · le nervature che si dividono',
    title: 'Legge di Murray',
    hook: 'Il cubo del raggio del ramo padre è la somma dei cubi dei rami figli.',
    doors: [
      { leap: 'perché', title: 'Permafrost', hook: "Sotto il delta il suolo resta gelato tutto l'anno, per centinaia di metri.", thumb: permafrost, dot: [0.13, 0.17], box: [0.02, 0.04, 0.26, 0.3] },
      { leap: 'dentro', title: 'Falso colore', hook: "Landsat vede l'infrarosso: la vegetazione si accende, l'acqua diventa viola.", thumb: infrared, dot: [0.13, 0.6], box: [0.04, 0.5, 0.24, 0.72] },
      { leap: 'altrove', title: 'Nervatura fogliare', hook: 'Le foglie risolvono lo stesso problema del delta: distribuire un flusso.', thumb: taro, dot: [0.43, 0.25], box: [0.33, 0.12, 0.53, 0.38] },
    ],
  },
}

type S = {
  page: 'leaf' | 'delta'
  hover: number
  /** Doors written so far; the others are placeholders until the curator writes them */
  ready: boolean[]
  fly: boolean
  /** The door whose thumbnail is in flight: hidden in its row */
  gone: number
}

const initial: S = { page: 'leaf', hover: -1, ready: [true, true, true], fly: false, gone: -1 }
const POSTER: S = { ...initial, hover: 2 }
const REST = { wide: { x: 330, y: 150 }, compact: { x: 250, y: 330 } }
const NARROW = 640 // below it, the compact design: the app on a phone, tapped

/** Hover a detail, follow its line to the door, open it: the thumbnail becomes the next picture. */
async function dive(a: Act<S>, next: S['page']) {
  await a.move('dot-2')
  await a.set({ hover: 2 })
  await a.wait(1100)
  await a.move('door-2')
  await a.wait(200)
  await a.click()
  await a.set({ hover: -1, fly: true, gone: 2 })
  // The doors fade to placeholders, last first, while the thumbnail grows into the picture
  await a.wait(100)
  for (const i of [2, 1, 0]) {
    await a.set((s) => ({ ready: s.ready.map((r, j) => (j === i ? false : r)) }))
    await a.wait(70)
  }
  await a.wait(700)
  await a.set({ page: next, fly: false, gone: -1 })
  a.move('rest')
  await a.wait(450)
  // The curator writes the doors one at a time
  for (const i of [0, 1, 2]) {
    await a.set((s) => ({ ready: s.ready.map((r, j) => (j === i ? true : r)) }))
    await a.wait(380)
  }
  await a.wait(1100)
}

async function script(a: Act<S>) {
  await a.wait(500)
  await dive(a, 'delta')
  await dive(a, 'leaf')
}

export default function WikiExplorer() {
  // A finger on the phone, a pointer on the desktop: the cursor follows the layout the host width picks
  const host = useRef<HTMLDivElement>(null)
  const [narrow, setNarrow] = useState(false)
  useEffect(() => {
    const el = host.current
    if (!el) return
    const watch = new ResizeObserver(([entry]) => setNarrow(entry.contentRect.width < NARROW))
    watch.observe(el)
    return () => watch.disconnect()
  }, [])
  return (
    <div ref={host}>
    <Stage
      width={880}
      height={560}
      compact={{ below: NARROW, width: 358, height: 620 }}
      initial={initial}
      poster={POSTER}
      script={script}
      fidelity="realistic"
      cursor={{ at: REST, style: narrow ? 'touch' : 'real' }}
      className="wx"
    >
      {(s, rect, layout) => <Scene s={s} rect={rect} layout={layout} />}
    </Stage>
    </div>
  )
}

function Scene({ s, rect, layout }: { s: S; rect: (name: string) => { x: number; y: number; width: number; height: number }; layout: Layout }) {
  const page = PAGES[s.page]
  const next = PAGES[s.page === 'leaf' ? 'delta' : 'leaf']
  const dot = rect(`dot-${s.hover < 0 ? 2 : s.hover}`)
  const thumb = rect(`thumb-${s.hover < 0 ? 2 : s.hover}`)
  return (
    <>
      <Frame layout={layout}>
        <div className="wx-app">
          <header className="wx-header">
            <span>Wiki Explorer</span>
            {layout === 'wide' && <span className="wx-search">Cerca</span>}
          </header>
          <div className="wx-spread">
            <figure className="wx-figure">
              <div className="wx-picture" data-target="picture">
                <img src={page.img} alt="" />
                {page.doors.map((d, i) => (
                  <div key={`${s.page}-hole-${i}`} className="wx-hole" data-show={s.hover === i} style={hole(d.box)} />
                ))}
                {page.doors.map((d, i) => (
                  <span
                    key={`${s.page}-dot-${i}`}
                    className="wx-dot"
                    data-target={`dot-${i}`}
                    data-cursor="hand"
                    data-show={s.ready[i]}
                    data-on={s.hover === i}
                    style={{ left: `${d.dot[0] * 100}%`, top: `${d.dot[1] * 100}%` }}
                  />
                ))}
              </div>
              {layout === 'wide' && (
                <figcaption>
                  <Swap id={s.page}>
                    <span className="wx-seeing">{page.seeing}</span>
                    <span className="wx-credit">{page.credit}</span>
                  </Swap>
                </figcaption>
              )}
            </figure>

            <aside className="wx-aside">
              {layout === 'wide' && (
                <Swap id={s.page} className="wx-via">
                  <div className="wx-leap">{page.leap}</div>
                  <div className="wx-title">{page.title}</div>
                </Swap>
              )}
              <div className="wx-doors-head">
                <span>Da qui</span>
                <Swap id={String(s.ready.every(Boolean))} className="wx-mute">{s.ready.every(Boolean) ? '1–3 per aprire' : 'Cerco le porte'}</Swap>
              </div>
              {page.doors.map((d, i) => (
                <div key={i} className="wx-door" data-target={`door-${i}`} data-cursor="hand" data-dim={s.hover >= 0 && s.hover !== i}>
                  <States
                    value={s.ready[i] ? 'door' : 'wait'}
                    align="start"
                    className="wx-door-states"
                    states={{
                      wait: (
                        <div className="wx-door-grid wx-door-wait">
                          <span className="wx-thumb wx-thumb--wait" />
                          <div className="wx-door-text">
                            <Bar w={52} h={7} tone={2} />
                            <Bar w={120} h={9} tone={3} />
                            <Bar w={190} h={7} tone={2} />
                          </div>
                        </div>
                      ),
                      door: (
                        <div className="wx-door-grid">
                          <img className="wx-thumb" data-target={`thumb-${i}`} src={d.thumb} alt="" data-gone={s.gone === i} />
                          <div className="wx-door-text">
                            <div className="wx-door-leap">
                              <span>{i + 1}</span>
                              <span>{d.leap}</span>
                            </div>
                            <div className="wx-door-title">{d.title}</div>
                            <div className="wx-door-hook">{d.hook}</div>
                          </div>
                        </div>
                      ),
                    }}
                  />
                </div>
              ))}
            </aside>
          </div>
        </div>
      </Frame>

      <Fly from={rect('thumb-2')} to={rect('picture')} go={s.fly} duration={900} className="wx-fly">
        <img src={next.img} alt="" style={{ width: rect('thumb-2').width || 64, height: rect('thumb-2').height || 48 }} />
      </Fly>

      <Connector
        from={{ x: dot.x + dot.width / 2, y: dot.y + dot.height / 2 }}
        to={{ x: thumb.x - 10, y: thumb.y + thumb.height / 2 }}
        route={layout === 'wide' ? 'curve' : 'v'}
        show={layout === 'wide' && s.hover >= 0}
        className="wx-line"
      />
      <span data-target="rest" style={{ position: 'absolute', ...box({ ...REST[layout], width: 1, height: 1 }) }} />
    </>
  )
}

/** A browser on the desktop; on a phone column, the same app on a phone, cropped at the bottom. */
function Frame({ layout, children }: { layout: Layout; children: ReactNode }) {
  if (layout === 'compact') return <Phone width={310} className="wx-phone">{children}</Phone>
  return <Window url="localhost:5173" className="wx-window">{children}</Window>
}

function hole([x0, y0, x1, y1]: Door['box']) {
  return { left: `${x0 * 100}%`, top: `${y0 * 100}%`, width: `${(x1 - x0) * 100}%`, height: `${(y1 - y0) * 100}%` }
}
