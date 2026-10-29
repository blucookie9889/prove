import { useCallback, useEffect, useRef, useState } from "react";
import { flushSync } from "react-dom";

const SUGGESTIONS = ["foglie", "texture", "architettura gotica", "minerali", "conchiglie", "nuvole", "farfalle", "mosaici"];
// First answer embeds only a few new images so it comes back fast; follow-up rounds refine it
// (each round also explores the categories of the best matches so far).
const FIRST_BUDGET = 24;
const REFINE_BUDGET = 64;
const REFINE_ROUNDS = 3;
// Similar images shown at first; scrolling down asks for this many more each time.
const SIMILAR_PAGE = 60;
// Search pages are shown at once, then checked with CLIP (up to this many new images per page).
const SEARCH_BUDGET = 72;
// Cards that get their own view-transition name, so they glide to their new place on re-rank.
const ANIMATED_CARDS = 40;
const TRAIL_KEY = "wiki-explorer-trail";

async function api(path, { signal, body } = {}) {
  const res = await fetch(`/api${path}`, {
    signal,
    method: body ? "POST" : "GET",
    headers: body ? { "content-type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) throw new Error((await res.json().catch(() => null))?.detail || "Qualcosa è andato storto, riprova.");
  return res.json();
}

/** Run a DOM-changing update inside a View Transition when the browser supports it. */
function withTransition(update) {
  const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (!document.startViewTransition || reduce || document.hidden) {
    update();
    return;
  }
  document.startViewTransition(() => flushSync(update));
}

function vtName(title) {
  let h = 0;
  for (let i = 0; i < title.length; i += 1) h = (h * 31 + title.charCodeAt(i)) | 0;
  return `c${(h >>> 0).toString(36)}`;
}

/* ------------------------------------------------------------------ routing (URL params) */

function readRoute() {
  const p = new URLSearchParams(window.location.search);
  if (p.get("file")) return { view: "file", file: p.get("file"), q: p.get("q") || "" };
  if (p.get("c")) return { view: "concept", qid: p.get("c"), q: "" };
  if (p.get("view") === "saved") return { view: "saved", q: "" };
  if (p.get("view") === "featured") return { view: "featured", q: "" };
  if (p.get("q")) return { view: "search", q: p.get("q") };
  return { view: "feed", q: "" };
}

function routeUrl(r) {
  const p = new URLSearchParams();
  if (r.view === "file") {
    p.set("file", r.file);
    if (r.q) p.set("q", r.q);
  } else if (r.view === "concept") p.set("c", r.qid);
  else if (r.view === "saved" || r.view === "featured") p.set("view", r.view);
  else if (r.view === "search") p.set("q", r.q);
  const s = p.toString();
  return s ? `?${s}` : window.location.pathname;
}

function bigThumb(item) {
  if (!item) return "";
  if (!item.thumb.includes("upload.wikimedia.org")) return item.full || item.thumb; // museums, iNaturalist
  if (item.full_width && item.full_width <= 1280) return item.full;
  return item.thumb.replace(/\/\d+px-/, "/1280px-");
}

function readTrail() {
  try {
    return JSON.parse(sessionStorage.getItem(TRAIL_KEY) || "[]");
  } catch {
    return [];
  }
}

/** Remember an opened image; ``anchor`` = the part of the previous image that led here ({ src, box }). */
function pushTrail(item, anchor) {
  const prev = readTrail().find((t) => t.title === item.title);
  const next = [{ title: item.title, thumb: item.thumb, name: item.name, width: item.width, height: item.height, kind: item.kind, anchor: anchor || prev?.anchor }]
    .concat(readTrail().filter((t) => t.title !== item.title))
    .slice(0, 16);
  try {
    sessionStorage.setItem(TRAIL_KEY, JSON.stringify(next));
  } catch {
    /* private mode: the trail just isn't remembered */
  }
  return next;
}

/* ------------------------------------------------------------------ header */

/** One quiet line: the name, the search (plain text over a hairline), the saved count. */
function Header({ route, savedCount, onSearch, onHome, onSaved }) {
  const [text, setText] = useState(route.view === "search" ? route.q : "");

  useEffect(() => {
    if (route.view === "search") setText(route.q);
    if (route.view === "feed") setText("");
  }, [route]);

  return (
    <header className="sticky top-0 z-30 bg-paper/90 backdrop-blur-md">
      <div className="mx-auto flex h-16 max-w-[1500px] items-center gap-6 px-6">
        <button type="button" onClick={onHome} className="shrink-0 text-ink">
          Wiki Explorer
        </button>
        <form
          className="min-w-0 flex-1"
          onSubmit={(e) => {
            e.preventDefault();
            if (text.trim()) onSearch(text.trim());
          }}
        >
          <input
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder="Cerca"
            aria-label="Cerca tra le immagini libere"
            className="calm w-full border-b border-rule-2 bg-transparent py-1.5 text-ink transition-colors outline-none focus:border-ink"
            enterKeyHint="search"
          />
        </form>
        <button type="button" onClick={onSaved} className={`calm shrink-0 transition-colors ${route.view === "saved" ? "text-ink" : "text-mute hover:text-ink"}`}>
          Salvati{savedCount > 0 && <span className="ml-1.5 text-mute">{savedCount}</span>}
        </button>
      </div>
    </header>
  );
}

/* ------------------------------------------------------------------ cards & grid */

/**
 * An image in a grid. Nothing on it at rest; on hover a line of muted text appears below it
 * (its name, and the quiet actions). The grid makes the other images recede.
 */
function Card({ item, index, animated, onOpen, onSave, onVote, voting }) {
  const [loaded, setLoaded] = useState(false);
  const ratio = item.width && item.height ? `${item.width} / ${item.height}` : "4 / 5";
  const imgRef = useRef(null);

  return (
    <figure
      className="rise group relative"
      style={{ "--i": index % 24, viewTransitionName: animated ? vtName(item.title) : undefined }}
    >
      <button
        type="button"
        onClick={() => onOpen(item, imgRef.current)}
        className={`placeholder relative block w-full overflow-hidden rounded-[2px] ${item.vote === 1 ? "outline outline-1 outline-offset-2 outline-ink" : ""}`}
        style={{ aspectRatio: ratio }}
        aria-label={item.name}
      >
        <img
          ref={imgRef}
          src={item.thumb}
          alt={item.name}
          loading="lazy"
          decoding="async"
          onLoad={() => setLoaded(true)}
          className={`calm h-full w-full object-cover transition-[opacity,filter] ${loaded ? "opacity-100 blur-0" : "opacity-0 blur-[2px]"}`}
        />
      </button>
      <figcaption className="calm flex items-baseline gap-3 pt-1.5 pb-1 text-mute opacity-0 transition-opacity group-focus-within:opacity-100 group-hover:opacity-100">
        <span className="min-w-0 flex-1 truncate">{item.name}</span>
        {item.match != null && <span>{Math.round(item.match * 100)}</span>}
        {onVote && (
          <>
            <button type="button" disabled={voting} onClick={() => onVote(item, item.vote === 1 ? 0 : 1)} className="link shrink-0" title="Più così: affina i risultati verso questa immagine">
              più
            </button>
            <button type="button" disabled={voting} onClick={() => onVote(item, -1)} className="link shrink-0" title="Meno così: allontana i risultati da questa immagine">
              meno
            </button>
          </>
        )}
        <button type="button" onClick={() => onSave(item)} className={`link shrink-0 ${item.saved ? "text-ink" : ""}`}>
          {item.saved ? "salvata" : "salva"}
        </button>
      </figcaption>
    </figure>
  );
}

function Masonry({ items, animate = false, ...handlers }) {
  return (
    <div className="masonry recede">
      {items.map((it, i) => (
        <Card key={it.title} item={it} index={i} animated={animate && i < ANIMATED_CARDS} {...handlers} />
      ))}
    </div>
  );
}

function SkeletonGrid({ n = 18 }) {
  const ratios = [0.8, 1.25, 0.75, 1, 1.4, 0.9, 1.1, 0.7, 1.2];
  return (
    <div className="masonry">
      {Array.from({ length: n }, (_, i) => (
        <div key={i} className="placeholder fade-in rounded-[2px]" style={{ aspectRatio: ratios[i % ratios.length] }} />
      ))}
    </div>
  );
}

function InfiniteSentinel({ onVisible, active }) {
  const ref = useRef(null);
  useEffect(() => {
    if (!active || !ref.current) return undefined;
    const io = new IntersectionObserver((e) => e[0].isIntersecting && onVisible(), { rootMargin: "1200px" });
    io.observe(ref.current);
    return () => io.disconnect();
  }, [active, onVisible]);
  return <div ref={ref} className="h-px" />;
}

/** A 1px line at the very top that grows slowly while something loads. */
function ProgressBar({ value, active }) {
  return (
    <div className="pointer-events-none fixed inset-x-0 top-0 z-40 h-px">
      <div
        className="h-full bg-ink transition-all duration-[1600ms] ease-[cubic-bezier(.16,1,.3,1)]"
        style={{ width: `${Math.max(4, value * 100)}%`, opacity: active ? 1 : 0 }}
      />
    </div>
  );
}

/** "Working" as the portfolio says it: a 4px dot breathing next to a muted line. */
function Breathing({ children }) {
  return (
    <span className="inline-flex items-center gap-2 text-mute">
      <span className="breathe inline-block h-1 w-1 rounded-full bg-ink" />
      {children}
    </span>
  );
}

/* ------------------------------------------------------------------ browse (feed / search / saved) */

function Browse({ route, cardHandlers }) {
  const [items, setItems] = useState([]);
  const [next, setNext] = useState(null);
  const [english, setEnglish] = useState("");
  const [loading, setLoading] = useState(true);
  const [checking, setChecking] = useState(false);
  const [hidden, setHidden] = useState(0);
  const [error, setError] = useState("");
  const [personal, setPersonal] = useState(true);
  const [indexed, setIndexed] = useState(0);
  const feedKind = useRef("auto");
  const busy = useRef(false);
  const itemsRef = useRef(items);
  itemsRef.current = items;

  const load = useCallback(
    async (cursor, signal) => {
      busy.current = true;
      // Earlier pages stay; the page starting at ``cursor`` is appended (or replaced when checked).
      const base = cursor ? itemsRef.current : [];
      const apply = (data) => {
        const seen = new Set(base.map((p) => p.title));
        setItems([...base, ...data.items.filter((i) => !seen.has(i.title))]);
        setNext(data.next ?? null);
        setEnglish(data.english || "");
        setError("");
      };
      try {
        if (route.view === "saved") apply(await api("/saved", { signal }));
        else if (route.view === "feed" && feedKind.current !== "featured") {
          const data = await api(`/foryou?page=${cursor || 0}`, { signal });
          if (data.personal) apply(data);
          else {
            // Nothing to personalise yet: show the featured pictures until the user opens or saves some.
            feedKind.current = "featured";
            setPersonal(false);
            apply(await api("/feed", { signal }));
          }
        } else if (route.view === "feed" || route.view === "featured")
          apply(await api(`/feed${cursor ? `?cont=${encodeURIComponent(cursor)}` : ""}`, { signal }));
        else {
          const url = `/search?q=${encodeURIComponent(route.q)}&offset=${cursor || 0}`;
          if (!cursor) {
            // First page: show what Commons found right away, then let CLIP clean it up.
            const quick = await api(`${url}&budget=0`, { signal });
            apply(quick);
            setHidden(quick.hidden || 0);
            setLoading(false);
            if (!quick.pending) return;
            setChecking(true);
          }
          const full = await api(`${url}&budget=${SEARCH_BUDGET}`, { signal });
          withTransition(() => {
            apply(full);
            setHidden((h) => (cursor ? h : 0) + (full.hidden || 0));
          });
        }
      } catch (e) {
        if (e.name !== "AbortError") setError("Wikimedia non risponde, riprova tra poco.");
      } finally {
        busy.current = false;
        if (!signal?.aborted) {
          // An aborted load (route changed, StrictMode re-run) must not end the next one's spinner.
          setLoading(false);
          setChecking(false);
        }
      }
    },
    [route],
  );

  useEffect(() => {
    const ctl = new AbortController();
    setLoading(true);
    setItems([]);
    setHidden(0);
    load(null, ctl.signal);
    return () => ctl.abort();
  }, [load]);

  useEffect(() => {
    if (route.view !== "feed" && route.view !== "featured") return;
    api("/health")
      .then((h) => setIndexed(h.indexed))
      .catch(() => {});
  }, [route.view]);

  // Keep the "Salvata" state in sync when a card is saved from another view.
  useEffect(() => {
    const onSaved = (e) =>
      setItems((prev) =>
        route.view === "saved" && !e.detail.saved
          ? prev.filter((p) => p.title !== e.detail.title)
          : prev.map((p) => (p.title === e.detail.title ? { ...p, saved: e.detail.saved } : p)),
      );
    window.addEventListener("saved-changed", onSaved);
    return () => window.removeEventListener("saved-changed", onSaved);
  }, [route.view]);

  const more = useCallback(() => {
    if (!busy.current && next != null) load(next);
  }, [load, next]);

  const home = route.view === "feed" || route.view === "featured";
  return (
    <main className="mx-auto max-w-[1500px] px-6 pt-16 pb-24 md:pt-24">
      <div className="mx-auto mb-16 flex max-w-[560px] flex-col gap-3.5">
        {home && (
          <>
            <p className="rise rise-1">
              Ogni immagine è una pagina. Le cose che contiene sono collegamenti: portano a ciò che le spiega, e da lì ad altre
              immagini.
            </p>
            <p className="rise rise-2 flex flex-wrap gap-x-5 gap-y-1">
              {[
                ["feed", "Per te"],
                ["featured", "In evidenza"],
              ].map(([view, label]) => (
                <button
                  key={view}
                  type="button"
                  onClick={() => cardHandlers.onNav(view)}
                  className={`calm transition-colors ${route.view === view ? "text-ink" : "text-mute hover:text-ink"}`}
                >
                  {label}
                </button>
              ))}
            </p>
            <p className="rise rise-3 text-mute">
              {route.view === "feed" && !personal
                ? "Apri e salva qualche immagine: qui nascerà il tuo feed."
                : indexed
                  ? `${indexed.toLocaleString("it-IT")} immagini nell'indice, che cresce mentre navighi.`
                  : " "}
            </p>
            <p className="rise rise-4 text-mute">
              Prova con{" "}
              {SUGGESTIONS.map((s, i) => (
                <span key={s}>
                  <button type="button" onClick={() => cardHandlers.onSearch(s)} className="link text-ink-2">
                    {s}
                  </button>
                  {i < SUGGESTIONS.length - 1 ? ", " : "."}
                </span>
              ))}
            </p>
          </>
        )}
        {route.view === "search" && !loading && (
          <>
            <p className="rise rise-1">
              {route.q}
              {english && english.toLowerCase() !== route.q.toLowerCase() && <span className="text-mute"> · cercato come {english}</span>}
            </p>
            <p className="rise rise-2 min-h-[1.6em] text-mute">
              {checking ? (
                <Breathing>Controllo che ogni immagine mostri davvero {english || route.q}</Breathing>
              ) : hidden ? (
                `${hidden} immagini nascoste: fuori tema o poco belle.`
              ) : null}
            </p>
          </>
        )}
        {route.view === "saved" && <p className="rise rise-1">Salvati</p>}
      </div>
      {route.view === "search" && <ProgressBar value={checking ? 0.6 : 1} active={checking} />}

      {error && <p className="py-20 text-center text-mute">{error}</p>}
      {loading ? (
        <SkeletonGrid />
      ) : items.length ? (
        <Masonry items={items} animate={route.view === "search"} onOpen={cardHandlers.onOpen} onSave={cardHandlers.onSave} />
      ) : (
        !error && (
          <p className="mx-auto max-w-[560px] text-mute">
            {route.view === "saved" ? "Non hai ancora salvato nessuna immagine." : "Nessun risultato."}
          </p>
        )
      )}
      <InfiniteSentinel onVisible={more} active={!loading && next != null} />
    </main>
  );
}

/* ------------------------------------------------------------------ detail + similar */

function lookLabel(w) {
  if (w >= 0.85) return "Solo aspetto";
  if (w >= 0.6) return "Soprattutto aspetto";
  if (w > 0.4) return "Bilanciato";
  if (w > 0.15) return "Soprattutto soggetto";
  return "Solo soggetto";
}

/** Width / height of ``box`` (fractions of an image whose own aspect ratio is ``ratio``). */
function boxAspect(box, ratio) {
  return ((box[2] - box[0]) * (ratio || 1)) / (box[3] - box[1]);
}

/** CSS that shows only ``box`` (fractions of the image: x0, y0, x1, y1) of ``src``, filling the element. */
function cropStyle(src, box) {
  const [x0, y0, x1, y1] = box;
  const bw = x1 - x0;
  const bh = y1 - y0;
  return {
    backgroundImage: `url("${src}")`,
    backgroundSize: `${100 / bw}% ${100 / bh}%`,
    backgroundPosition: `${bw < 1 ? (x0 / (1 - bw)) * 100 : 0}% ${bh < 1 ? (y0 / (1 - bh)) * 100 : 0}%`,
  };
}

/** The way here, like a browser history of articles: each step, and the detail that led to it. */
function Trail({ trail, current, onOpen }) {
  const path = trail.slice(0, 10).reverse();
  if (path.length < 2) return null;
  return (
    <nav className="fade-in mx-auto mb-14 flex max-w-[560px] items-center gap-2 overflow-x-auto" aria-label="Il tuo percorso">
      {path.map((t, i) => (
        <span key={t.title} className="flex shrink-0 items-center gap-2">
          {i > 0 && (
            <span className="flex items-center gap-2">
              <span className="h-px w-3 bg-rule-2" />
              {t.anchor && (
                <span
                  title="Il dettaglio che ti ha portato qui"
                  className="h-5 rounded-[2px] bg-wash"
                  style={{ ...cropStyle(t.anchor.src, t.anchor.box), aspectRatio: `${boxAspect(t.anchor.box, t.anchor.ratio)}` }}
                />
              )}
              <span className="h-px w-3 bg-rule-2" />
            </span>
          )}
          <button
            type="button"
            title={t.name}
            disabled={t.title === current}
            onClick={() => onOpen(t)}
            className={`calm h-7 w-7 overflow-hidden rounded-[2px] bg-wash transition-opacity ${t.title === current ? "opacity-100" : "opacity-40 hover:opacity-100"}`}
          >
            <img src={t.thumb} alt={t.name} className="h-full w-full object-cover" />
          </button>
        </span>
      ))}
    </nav>
  );
}

function bytes(b64) {
  return Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
}

/**
 * The image as a page: the things in it (found by DINOv2) are its links. A small breathing dot
 * marks each one; hovering lights it up and previews what it is (the Wikipedia concept, once
 * named); clicking dives into it: that part of the image grows into the concept's page (or,
 * without a concept, into the most similar image).
 */
function PageHero({ origin, page, concepts, hover, setHover, onFollow, onConcept }) {
  const [bigLoaded, setBigLoaded] = useState(false);
  const shade = useRef(null);
  const dive = useRef(null);
  const [diving, setDiving] = useState(-1);
  const touch = useRef(false);
  const ratio = origin?.width && origin?.height ? origin.width / origin.height : page ? page.w / page.h : 4 / 3;
  const links = page?.links || [];
  const map = page?.map;

  // Veil everything but the hovered thing.
  useEffect(() => {
    const canvas = shade.current;
    if (!canvas || !map) return;
    canvas.width = page.w;
    canvas.height = page.h;
    const ctx = canvas.getContext("2d");
    const img = ctx.createImageData(page.w, page.h);
    for (let i = 0; i < map.length; i += 1) {
      if (map[i] === hover) continue;
      img.data.set([250, 249, 246, 150], i * 4); // the paper colour, not black: the rest fades back
    }
    ctx.putImageData(img, 0, 0);
  }, [map, hover, page]);

  const linkAt = (e) => {
    if (!map) return -1;
    const r = e.currentTarget.getBoundingClientRect();
    const gx = Math.min(page.w - 1, Math.floor(((e.clientX - r.left) / r.width) * page.w));
    const gy = Math.min(page.h - 1, Math.floor(((e.clientY - r.top) / r.height) * page.h));
    const i = map[gy * page.w + gx];
    return i === 255 ? -1 : i;
  };

  const follow = (i) => {
    flushSync(() => setDiving(i));
    if (concepts?.[i]) onConcept(links[i], concepts[i], dive.current);
    else onFollow(links[i], links[i].items[0], dive.current);
  };

  const shown = hover >= 0 ? links[hover] : null;
  const idea = hover >= 0 ? concepts?.[hover] : null;
  const big = bigThumb(origin);
  return (
    <div
      className="relative mx-auto"
      style={{ aspectRatio: `${ratio}`, width: `min(100%, calc(78vh * ${ratio}))` }}
      onPointerDown={(e) => {
        touch.current = e.pointerType === "touch";
      }}
      onMouseMove={(e) => !touch.current && setHover(linkAt(e))}
      onMouseLeave={() => setHover(-1)}
      onClick={(e) => {
        const i = linkAt(e);
        // No hover on a phone: the first tap previews where a part leads, the second one goes there.
        if (touch.current && i !== hover) setHover(i);
        else if (i >= 0) follow(i);
      }}
    >
      <div className={`placeholder absolute inset-0 overflow-hidden rounded-[2px] ${hover >= 0 ? "cursor-pointer" : ""}`}>
        {origin?.thumb && (
          <>
            {/* The grid thumbnail is already cached: show it at once (it is what the card morphs into). */}
            <img data-hero src={origin.thumb} alt={origin.name} className="h-full w-full object-cover" style={{ viewTransitionName: "hero" }} />
            <img
              src={big}
              alt=""
              onLoad={() => setBigLoaded(true)}
              className={`absolute inset-0 h-full w-full object-cover transition-opacity duration-[1800ms] ease-[cubic-bezier(.16,1,.3,1)] ${bigLoaded ? "opacity-100" : "opacity-0"}`}
            />
          </>
        )}
        <canvas
          ref={shade}
          className={`pointer-events-none absolute inset-0 h-full w-full transition-opacity duration-[1200ms] ease-[cubic-bezier(.16,1,.3,1)] ${hover >= 0 ? "opacity-100" : "opacity-0"}`}
        />
        {diving >= 0 && (
          // The part being followed, drawn over itself: the View Transition grows it into the next page.
          <span ref={dive} className="absolute bg-no-repeat" style={{
            left: `${links[diving].box[0] * 100}%`,
            top: `${links[diving].box[1] * 100}%`,
            width: `${(links[diving].box[2] - links[diving].box[0]) * 100}%`,
            height: `${(links[diving].box[3] - links[diving].box[1]) * 100}%`,
            ...cropStyle(bigLoaded ? big : origin.thumb, links[diving].box),
          }} />
        )}
        {links.map((l, i) => (
          <span
            key={i}
            className="fade-in pointer-events-none absolute -translate-x-1/2 -translate-y-1/2"
            style={{ left: `${l.dot[0] * 100}%`, top: `${l.dot[1] * 100}%`, animationDelay: `${0.3 + i * 0.25}s` }}
          >
            <span
              className={`block h-1.5 w-1.5 rounded-full bg-paper outline outline-1 outline-ink/25 transition-transform duration-[1200ms] ease-[cubic-bezier(.16,1,.3,1)] ${
                hover === i ? "scale-[1.8]" : "breathe"
              }`}
              style={{ animationDelay: `${i * 0.7}s` }}
            />
          </span>
        ))}
      </div>

      {shown && (
        // What this part is, like Wikipedia's hover preview.
        <div
          key={hover}
          className="fade-in pointer-events-none absolute z-20 w-72 rounded-[2px] border border-rule bg-paper p-3"
          style={{
            left: shown.dot[0] > 0.55 ? undefined : `calc(${shown.dot[0] * 100}% + 16px)`,
            right: shown.dot[0] > 0.55 ? `calc(${(1 - shown.dot[0]) * 100}% + 16px)` : undefined,
            top: shown.dot[1] > 0.55 ? undefined : `calc(${shown.dot[1] * 100}% - 12px)`,
            bottom: shown.dot[1] > 0.55 ? `calc(${(1 - shown.dot[1]) * 100}% - 12px)` : undefined,
          }}
        >
          {idea ? (
            <div className="mb-3 flex flex-col gap-1">
              <p className="first-letter:uppercase">{idea.name}</p>
              {idea.extract && <p className="line-clamp-4 text-ink-2">{idea.extract}</p>}
            </div>
          ) : concepts === null ? (
            <p className="mb-3">
              <Breathing>Cerco cos'è</Breathing>
            </p>
          ) : null}
          <div className="grid grid-cols-3 gap-2">
            {shown.items.slice(0, 3).map((it) => (
              <img key={it.title} src={it.thumb} alt="" className="aspect-[4/5] w-full rounded-[2px] bg-wash object-cover" />
            ))}
          </div>
          <p className="mt-2.5 text-mute">{idea ? "Clicca per leggere la voce" : "Clicca per entrare"}</p>
        </div>
      )}
    </div>
  );
}

/**
 * "In questa immagine": every thread leaving this image, as list rows. A row shows the part and
 * what it is; opened, the explanation and where it leads. Hovering a row lights its part above.
 */
function Threads({ origin, page, concepts, hover, setHover, onFollow, onConcept }) {
  const [open, setOpen] = useState(-1);
  const ratio = origin?.width && origin?.height ? origin.width / origin.height : 1;
  if (!page) {
    return (
      <p className="border-t border-rule py-[11px]">
        <Breathing>Guardo cosa c'è nell'immagine</Breathing>
      </p>
    );
  }
  if (!page.links.length) return <p className="border-t border-rule py-[11px] text-mute">Nessuna parte da seguire in questa immagine.</p>;
  const src = bigThumb(origin);
  return (
    <div className="recede flex flex-col">
      {page.links.map((l, i) => {
        const c = concepts?.[i];
        const isOpen = open === i;
        return (
          <div
            key={i}
            className="rise group border-t border-rule"
            style={{ "--i": i * 3 }}
            onMouseEnter={() => setHover(i)}
            onMouseLeave={() => setHover(-1)}
          >
            <button
              type="button"
              onClick={() => setOpen(isOpen ? -1 : i)}
              aria-expanded={isOpen}
              className="grid w-full grid-cols-[108px_1fr_auto] items-center gap-x-0 py-[11px] text-left"
            >
              <span
                className="h-9 max-w-[84px] rounded-[2px] bg-wash bg-no-repeat"
                style={{ ...cropStyle(src, l.box), aspectRatio: `${Math.min(2, Math.max(0.6, boxAspect(l.box, ratio)))}` }}
              />
              <span className="calm min-w-0 truncate transition-transform group-hover:translate-x-[3px]">
                {c ? <span className="first-letter:uppercase">{c.name}</span> : concepts === null ? <Breathing>Cerco cos'è</Breathing> : <span className="text-mute">Una parte dell'immagine</span>}
              </span>
              <span className="plus ml-4 text-mute" data-open={isOpen} aria-hidden="true" />
            </button>
            <div className="fold" data-open={isOpen}>
              <div>
                <div className="flex flex-col gap-3.5 pb-6 pl-[108px]">
                  {c?.extract && <p className="text-ink-2">{c.extract}</p>}
                  <div className="grid grid-cols-2 gap-2">
                    {l.items.slice(0, 4).map((it) => (
                      <button key={it.title} type="button" title={it.name} onClick={(e) => onFollow(l, it, e.currentTarget.querySelector("img"))} className="block">
                        <img src={it.thumb} alt={it.name} loading="lazy" className="aspect-[4/5] w-full rounded-[2px] bg-wash object-cover" />
                      </button>
                    ))}
                  </div>
                  <p className="flex gap-5">
                    {c && (
                      <button type="button" onClick={() => onConcept(l, c, null)} className="link">
                        Leggi la voce
                      </button>
                    )}
                    <button type="button" onClick={() => onFollow(l, l.items[0], null)} className="link text-ink-2">
                      Entra nella prima immagine
                    </button>
                  </p>
                </div>
              </div>
            </div>
          </div>
        );
      })}
      <div className="border-t border-rule" />
    </div>
  );
}

function Detail({ route, preview, cardHandlers, onBack }) {
  const [origin, setOrigin] = useState(preview);
  const [items, setItems] = useState([]);
  const [weight, setWeight] = useState(0.75);
  const [phase, setPhase] = useState("loading"); // loading | refining | ready | error
  const [info, setInfo] = useState({ pending: 0, candidates: 0, more: false });
  const [voting, setVoting] = useState(false);
  const [error, setError] = useState("");
  const [trail, setTrail] = useState(readTrail);
  const [page, setPage] = useState(null);
  const [concepts, setConcepts] = useState(null); // null while the links are being named
  const [hover, setHover] = useState(-1);
  const run = useRef(0);
  const shown = useRef(false);

  const limit = useRef(SIMILAR_PAGE);

  const fetchSimilar = useCallback(
    async (budget, w) => {
      const qs = new URLSearchParams({
        title: route.file,
        weight: String(w),
        budget: String(budget),
        limit: String(limit.current),
      });
      if (route.q) qs.set("q", route.q);
      return api(`/similar?${qs}`);
    },
    [route.file, route.q],
  );

  const show = useCallback((data, append) => {
    const apply = () => {
      // Scrolling down appends: cards already on screen keep their place, like a Pinterest board.
      setItems((prev) => {
        if (!append) return data.items;
        const seen = new Set(prev.map((p) => p.title));
        return [...prev, ...data.items.filter((i) => !seen.has(i.title))];
      });
      setInfo({ pending: data.pending, candidates: data.candidates, more: data.items.length >= limit.current });
    };
    if (append) {
      apply();
      return;
    }
    // First results rise in; later rounds glide cards to their new rank.
    if (shown.current) withTransition(apply);
    else apply();
    shown.current = data.items.length > 0;
  }, []);

  // Load, then keep refining while there are candidates that have not been looked at yet.
  const refresh = useCallback(
    async (firstBudget, w, append = false) => {
      const id = ++run.current;
      try {
        let data = await fetchSimilar(firstBudget, w);
        if (id !== run.current) return;
        setOrigin((o) => ({ ...o, ...data.origin }));
        show(data, append);
        for (let round = 0; !append && round < REFINE_ROUNDS && data.pending > 0; round += 1) {
          setPhase("refining");
          data = await fetchSimilar(REFINE_BUDGET, w);
          if (id !== run.current) return;
          show(data);
        }
        setPhase("ready");
        setError("");
      } catch (e) {
        if (id !== run.current) return;
        setError(e.message);
        setPhase("error");
      }
    },
    [fetchSimilar, show],
  );

  useEffect(() => {
    setPhase("loading");
    api(`/file?title=${encodeURIComponent(route.file)}`)
      .then((o) => {
        setOrigin((prev) => ({ ...prev, ...o }));
        setTrail(pushTrail(o, preview?.anchor));
      })
      .catch(() => {});
    refresh(FIRST_BUDGET, weight);
    // The things in the image come first (the dots), then what each one is (Wikipedia concepts).
    api(`/page?title=${encodeURIComponent(route.file)}`)
      .then((p) => {
        setPage({ ...p, map: bytes(p.linkmap) });
        return api(`/concepts?title=${encodeURIComponent(route.file)}`);
      })
      .then((c) => setConcepts(c.concepts))
      .catch(() => {
        setPage((p) => p || { links: [], w: 1, h: 1, map: null });
        setConcepts([]);
      });
    return () => {
      run.current += 1;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [route.file]);

  // Moving the slider only re-ranks images that are already analysed: instant.
  const lastWeight = useRef(weight);
  useEffect(() => {
    if (lastWeight.current === weight) return undefined;
    const t = setTimeout(() => {
      lastWeight.current = weight;
      refresh(0, weight);
    }, 250);
    return () => clearTimeout(t);
  }, [weight, refresh]);

  useEffect(() => {
    const onSaved = (e) => {
      setItems((prev) => prev.map((p) => (p.title === e.detail.title ? { ...p, saved: e.detail.saved } : p)));
      setOrigin((o) => (o && o.title === e.detail.title ? { ...o, saved: e.detail.saved } : o));
    };
    window.addEventListener("saved-changed", onSaved);
    return () => window.removeEventListener("saved-changed", onSaved);
  }, []);

  const onVote = async (item, value) => {
    setVoting(true);
    if (value === -1) withTransition(() => setItems((prev) => prev.filter((p) => p.title !== item.title)));
    else setItems((prev) => prev.map((p) => (p.title === item.title ? { ...p, vote: value } : p)));
    try {
      await api("/vote", { body: { title: item.title, value, origin: route.file, item } });
      setPhase("refining");
      await refresh(REFINE_BUDGET, weight);
    } finally {
      setVoting(false);
    }
  };

  const loadMore = useCallback(() => {
    limit.current += SIMILAR_PAGE;
    setPhase("refining");
    refresh(REFINE_BUDGET, weight, true);
  }, [refresh, weight]);

  // Following a link: the detail is remembered in the trail, as the way into the next image.
  const anchorOf = (link) => ({ src: origin.thumb, box: link.box, ratio: origin?.width && origin?.height ? origin.width / origin.height : 1 });
  const follow = (link, item, el) => cardHandlers.onOpen(item, el, anchorOf(link));
  const openConcept = (link, concept, el) => cardHandlers.onConcept({ ...concept, thumb: concept.image || link.items[0]?.thumb }, el, anchorOf(link));

  const analysed = info.candidates - info.pending;
  const busy = phase === "loading" || phase === "refining";
  const status =
    phase === "loading"
      ? "Guardo le immagini collegate"
      : phase === "refining"
        ? `Affino: ${analysed} di ${info.candidates} immagini guardate`
        : phase === "error"
          ? error
          : `${analysed} immagini guardate${info.pending ? `, ${info.pending} ancora da guardare` : ""}. Passa sopra un'immagine: “più” e “meno” riordinano i risultati.`;

  return (
    <main className="mx-auto max-w-[1500px] px-6 pt-12 pb-24 md:pt-20">
      <ProgressBar value={info.candidates ? analysed / info.candidates : 0.08} active={busy} />

      <Trail
        trail={trail}
        current={route.file}
        onOpen={(t) => (t.kind === "concept" ? cardHandlers.onConcept({ qid: t.title, name: t.name, thumb: t.thumb }) : cardHandlers.onOpen(t))}
      />

      <header className="mx-auto mb-14 flex max-w-[560px] flex-col gap-3.5">
        <div className="rise rise-1 flex flex-col gap-0.5">
          <h1 className="break-words">{origin?.name || " "}</h1>
          {origin?.author !== undefined && <p className="text-mute">{[origin.author, origin.license].filter(Boolean).join(" · ")}</p>}
        </div>
        <p className="rise rise-2 text-ink-2">
          Ogni cosa in questa immagine è un collegamento, come le parole di una voce di Wikipedia. Passaci sopra per scoprire cos'è,
          clicca per leggerne la voce e continuare da lì.
        </p>
        {origin && (
          <p className="rise rise-3 flex flex-wrap gap-x-5 gap-y-1">
            <button type="button" onClick={onBack} className="link">
              Indietro
            </button>
            <button type="button" onClick={() => cardHandlers.onSave(origin)} className="link">
              {origin.saved ? "Salvata" : "Salva"}
            </button>
            {origin.page_url && (
              <a href={origin.page_url} target="_blank" rel="noreferrer">
                {origin.source || "Wikimedia Commons"}
              </a>
            )}
          </p>
        )}
      </header>

      <section className="rise rise-3 mb-24">
        <PageHero
          origin={origin}
          page={page}
          concepts={concepts}
          hover={hover}
          setHover={setHover}
          onFollow={follow}
          onConcept={openConcept}
        />
      </section>

      <section className="mx-auto mb-24 flex max-w-[560px] flex-col gap-3.5">
        <p className="text-mute">In questa immagine</p>
        <Threads
          origin={origin}
          page={page}
          concepts={concepts}
          hover={hover}
          setHover={setHover}
          onFollow={follow}
          onConcept={openConcept}
        />
      </section>

      <section className="mx-auto mb-10 flex max-w-[560px] flex-col gap-3.5">
        <p className="text-mute">Simili</p>
        <div className="flex flex-col gap-2">
          <input
            type="range"
            min="0"
            max="1"
            step="0.05"
            value={weight}
            onChange={(e) => setWeight(Number(e.target.value))}
            className="look-range"
            aria-label="Aspetto o soggetto"
          />
          <div className="flex justify-between text-mute">
            <span>Soggetto</span>
            <span key={lookLabel(weight)} className="fade-in text-ink-2">
              {lookLabel(weight)}
            </span>
            <span>Aspetto</span>
          </div>
        </div>
        <p className="text-mute">{busy ? <Breathing>{status}</Breathing> : status}</p>
      </section>

      {phase === "loading" && !items.length ? (
        <SkeletonGrid n={12} />
      ) : items.length ? (
        <Masonry
          items={items}
          animate
          onOpen={cardHandlers.onOpen}
          onSave={cardHandlers.onSave}
          onVote={onVote}
          voting={voting}
        />
      ) : (
        phase !== "loading" &&
        phase !== "error" && <p className="mx-auto max-w-[560px] text-mute">Nessuna immagine simile trovata.</p>
      )}

      <InfiniteSentinel onVisible={loadMore} active={phase === "ready" && (info.pending > 0 || info.more)} />
      {phase === "refining" && items.length > 0 && (
        <p className="mx-auto mt-10 max-w-[560px]">
          <Breathing>Cerco altre immagini simili</Breathing>
        </p>
      )}
    </main>
  );
}

/* ------------------------------------------------------------------ concept ("voce") */

/**
 * A concept, like a Wikipedia article made of images: what it is (the article's summary), the
 * pictures that show it, and the concepts that appear with it. Every picture opens an image page,
 * whose details lead to more concepts.
 */
function Voce({ route, preview, cardHandlers }) {
  const [data, setData] = useState(preview?.qid === route.qid ? preview : null);
  const [items, setItems] = useState([]);
  const [next, setNext] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [trail, setTrail] = useState(readTrail);
  const busy = useRef(false);

  useEffect(() => {
    let live = true;
    api(`/concept?qid=${encodeURIComponent(route.qid)}`)
      .then((d) => {
        if (!live) return;
        setData((prev) => ({ ...prev, ...d }));
        setItems(d.items);
        setNext(d.next ?? null);
        const thumb = d.image || d.items[0]?.thumb || preview?.thumb;
        setTrail(pushTrail({ title: route.qid, name: d.name, thumb, kind: "concept" }, preview?.qid === route.qid ? preview.anchor : undefined));
      })
      .catch((e) => live && setError(e.message))
      .finally(() => live && setLoading(false));
    return () => {
      live = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [route.qid]);

  useEffect(() => {
    const onSaved = (e) => setItems((prev) => prev.map((p) => (p.title === e.detail.title ? { ...p, saved: e.detail.saved } : p)));
    window.addEventListener("saved-changed", onSaved);
    return () => window.removeEventListener("saved-changed", onSaved);
  }, []);

  const more = useCallback(async () => {
    if (busy.current || next == null) return;
    busy.current = true;
    try {
      const d = await api(`/concept?qid=${encodeURIComponent(route.qid)}&offset=${next}`);
      setItems((prev) => {
        const seen = new Set(prev.map((p) => p.title));
        return [...prev, ...d.items.filter((i) => !seen.has(i.title))];
      });
      setNext(d.next ?? null);
    } catch {
      setNext(null);
    } finally {
      busy.current = false;
    }
  }, [next, route.qid]);

  const cover = data?.image || data?.thumb || items[0]?.thumb;
  const paragraphs = (data?.extract || "").split("\n").filter(Boolean);
  return (
    <main className="mx-auto max-w-[1500px] px-6 pt-12 pb-24 md:pt-20">
      <ProgressBar value={0.6} active={loading} />
      <Trail
        trail={trail}
        current={route.qid}
        onOpen={(t) => (t.kind === "concept" ? cardHandlers.onConcept({ qid: t.title, name: t.name, thumb: t.thumb }) : cardHandlers.onOpen(t))}
      />
      <article className="mx-auto mb-24 flex max-w-[560px] flex-col gap-3.5">
        <div className="rise rise-1 flex flex-col gap-0.5">
          <p className="text-mute">Voce</p>
          <h1 className="first-letter:uppercase">{data?.name || " "}</h1>
          {data?.description && <p className="text-mute first-letter:uppercase">{data.description.split(" / ").pop()}</p>}
        </div>
        <div className="rise rise-2 my-5 overflow-hidden rounded-[2px]">
          {cover ? (
            <img data-hero src={cover} alt={data?.name || ""} className="max-h-[70vh] w-full object-cover" style={{ viewTransitionName: "hero" }} />
          ) : (
            <div className="placeholder aspect-[4/5]" style={{ viewTransitionName: "hero" }} />
          )}
        </div>
        {loading && !paragraphs.length ? (
          <p>
            <Breathing>Leggo la voce</Breathing>
          </p>
        ) : (
          paragraphs.map((t, i) => (
            <p key={i} className="rise rise-3 text-ink-2">
              {t}
            </p>
          ))
        )}
        {error && <p className="text-mute">{error}</p>}
        {data?.url && (
          <p className="rise rise-4">
            <a href={data.url} target="_blank" rel="noreferrer">
              Leggi tutto su Wikipedia{data.lang === "en" ? ", in inglese" : ""}
            </a>
          </p>
        )}
        {data?.related?.length > 0 && (
          <div className="rise rise-5 mt-10 flex flex-col gap-3.5">
            <p className="text-mute">Voci collegate</p>
            <p className="text-ink-2">
              {data.related.map((r, i) => (
                <span key={r.qid}>
                  <button type="button" onClick={() => cardHandlers.onConcept(r)} className="link">
                    {r.name}
                  </button>
                  {i < data.related.length - 1 ? ", " : "."}
                </span>
              ))}
            </p>
          </div>
        )}
      </article>

      <section>
        <p className="mx-auto mb-3.5 max-w-[560px] text-mute">Immagini</p>
        {loading && !items.length ? (
          <SkeletonGrid n={12} />
        ) : items.length ? (
          <Masonry items={items} onOpen={cardHandlers.onOpen} onSave={cardHandlers.onSave} />
        ) : (
          !loading && <p className="mx-auto max-w-[560px] text-mute">Nessuna immagine per questa voce, per ora.</p>
        )}
        <InfiniteSentinel onVisible={more} active={!loading && next != null} />
      </section>
    </main>
  );
}

/* ------------------------------------------------------------------ footer */

/** A short rule, then the local time with the breathing dot, as on the portfolio. */
function Footer() {
  const fmt = () =>
    new Intl.DateTimeFormat("it-IT", { hour: "2-digit", minute: "2-digit", timeZone: "Europe/Rome" }).format(new Date());
  const [time, setTime] = useState(fmt);
  useEffect(() => {
    const t = setInterval(() => setTime(fmt()), 15000);
    return () => clearInterval(t);
  }, []);
  return (
    <footer className="mx-auto flex max-w-[560px] flex-col gap-3.5 px-6 pt-10 pb-[100px]">
      <span className="h-px w-12 bg-rule-2" />
      <Breathing>{time} a Treviso, Italia</Breathing>
    </footer>
  );
}

/* ------------------------------------------------------------------ app */

export default function App() {
  const [route, setRoute] = useState(readRoute);
  const [preview, setPreview] = useState(null);
  const [savedCount, setSavedCount] = useState(0);
  const [toast, setToast] = useState("");
  const lastToast = useRef("");
  if (toast) lastToast.current = toast; // keep the text while the toast fades out

  useEffect(() => {
    api("/health")
      .then((h) => setSavedCount(h.saved))
      .catch(() => setToast("Il server non risponde: è acceso?"));
    const onPop = () => setRoute(readRoute());
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  useEffect(() => {
    if (!toast) return undefined;
    const t = setTimeout(() => setToast(""), 2600);
    return () => clearTimeout(t);
  }, [toast]);

  const go = useCallback((r) => {
    window.history.pushState(null, "", routeUrl(r));
    setRoute(r);
    window.scrollTo(0, 0);
  }, []);

  const onSave = useCallback(async (item) => {
    const saved = !item.saved;
    window.dispatchEvent(new CustomEvent("saved-changed", { detail: { title: item.title, saved } }));
    setSavedCount((c) => c + (saved ? 1 : -1));
    setToast(saved ? "Salvata" : "Rimossa dai salvati");
    try {
      await api("/vote", { body: { title: item.title, value: saved ? 1 : 0, item: saved ? item : null } });
    } catch {
      setToast("Non sono riuscito a salvare");
    }
  }, []);

  // The clicked thumbnail morphs into the big image of the detail page.
  const onOpen = useCallback(
    (item, imgEl, anchor) => {
      document.querySelectorAll("[data-hero]").forEach((el) => {
        el.style.viewTransitionName = "none";
      });
      if (imgEl) imgEl.style.viewTransitionName = "hero";
      const q = route.view === "search" || route.view === "file" ? route.q : "";
      api("/view", { body: { title: item.title, item } }).catch(() => {});
      withTransition(() => {
        setPreview(anchor ? { ...item, anchor } : item);
        go({ view: "file", file: item.title, q });
      });
    },
    [go, route],
  );

  // A detail (or a "voce collegata") opens a concept: its crop, when there is one, grows into the cover.
  const onConcept = useCallback(
    (concept, el, anchor) => {
      document.querySelectorAll("[data-hero]").forEach((h) => {
        h.style.viewTransitionName = "none";
      });
      if (el) el.style.viewTransitionName = "hero";
      withTransition(() => {
        setPreview({ ...concept, kind: "concept", anchor });
        go({ view: "concept", qid: concept.qid, q: "" });
      });
    },
    [go],
  );

  const cardHandlers = {
    onOpen,
    onSave,
    onConcept,
    onSearch: (q) => go({ view: "search", q }),
    onNav: (view) => go({ view, q: "" }),
  };

  return (
    <div className="min-h-screen">
      <Header
        route={route}
        savedCount={savedCount}
        onSearch={(q) => go({ view: "search", q })}
        onHome={() => go({ view: "feed", q: "" })}
        onSaved={() => go({ view: "saved", q: "" })}
      />
      {route.view === "concept" ? (
        <Voce key={route.qid} route={route} preview={preview?.qid === route.qid ? preview : null} cardHandlers={cardHandlers} />
      ) : route.view === "file" ? (
        <Detail
          key={route.file}
          route={route}
          preview={preview?.title === route.file ? preview : null}
          cardHandlers={cardHandlers}
          onBack={() => (window.history.length > 1 ? window.history.back() : go({ view: "feed", q: "" }))}
        />
      ) : (
        <Browse key={`${route.view}:${route.q}`} route={route} cardHandlers={cardHandlers} />
      )}

      <Footer />

      <div
        className={`pointer-events-none fixed bottom-6 left-1/2 z-50 -translate-x-1/2 border border-rule bg-paper px-4 py-2 text-ink-2 transition-[opacity,filter] duration-[1400ms] ease-[cubic-bezier(.16,1,.3,1)] ${
          toast ? "opacity-100 blur-0" : "opacity-0 blur-[3px]"
        }`}
        role="status"
        aria-live="polite"
      >
        {lastToast.current}
      </div>
    </div>
  );
}
