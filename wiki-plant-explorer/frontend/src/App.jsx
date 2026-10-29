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

/* ------------------------------------------------------------------ icons */

const Icon = {
  search: (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round">
      <circle cx="11" cy="11" r="7" />
      <path d="m20 20-3.5-3.5" />
    </svg>
  ),
  plus: (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round">
      <path d="M12 5v14M5 12h14" />
    </svg>
  ),
  minus: (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round">
      <path d="M5 12h14" />
    </svg>
  ),
  back: (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M15 18l-6-6 6-6" />
    </svg>
  ),
  out: (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M7 17 17 7M8 7h9v9" />
    </svg>
  ),
  spark: (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor">
      <path d="M12 2l1.9 6.1L20 10l-6.1 1.9L12 18l-1.9-6.1L4 10l6.1-1.9z" />
    </svg>
  ),
};

/* ------------------------------------------------------------------ header */

function Header({ route, savedCount, onSearch, onHome, onSaved }) {
  const [text, setText] = useState(route.view === "search" ? route.q : "");
  const [scrolled, setScrolled] = useState(false);

  useEffect(() => {
    if (route.view === "search") setText(route.q);
    if (route.view === "feed") setText("");
  }, [route]);

  useEffect(() => {
    const onScroll = () => setScrolled(window.scrollY > 8);
    onScroll();
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => window.removeEventListener("scroll", onScroll);
  }, []);

  return (
    <header
      className={`sticky top-0 z-30 bg-white/85 backdrop-blur-xl transition-shadow duration-300 ${
        scrolled ? "shadow-[0_1px_0_rgba(0,0,0,0.06),0_8px_24px_-12px_rgba(0,0,0,0.12)]" : ""
      }`}
    >
      <div className="flex h-[72px] items-center gap-3 px-4 md:gap-5 md:px-6">
        <button type="button" onClick={onHome} className="group flex shrink-0 items-center gap-2" aria-label="Home">
          <span className="grid h-9 w-9 grid-cols-2 gap-[3px] rounded-[10px] bg-ink p-[7px] transition-transform duration-300 group-hover:rotate-[-8deg]">
            <span className="rounded-[2px] bg-white" />
            <span className="mt-1 rounded-[2px] bg-white" />
            <span className="-mt-1 rounded-[2px] bg-white" />
            <span className="rounded-[2px] bg-white" />
          </span>
          <span className="hidden text-[15px] font-semibold tracking-tight sm:inline">Wiki Explorer</span>
        </button>

        <form
          className="relative min-w-0 flex-1"
          onSubmit={(e) => {
            e.preventDefault();
            if (text.trim()) onSearch(text.trim());
          }}
        >
          <span className="pointer-events-none absolute top-1/2 left-4 -translate-y-1/2 text-mute">{Icon.search}</span>
          <input
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder="Cerca tra milioni di immagini libere"
            className="h-12 w-full rounded-full bg-wash pr-4 pl-11 text-[15px] transition outline-none placeholder:text-mute focus:bg-white focus:shadow-[0_0_0_1px_rgba(0,0,0,0.08),0_8px_30px_-8px_rgba(0,0,0,0.18)]"
            enterKeyHint="search"
          />
        </form>

        <button
          type="button"
          onClick={onSaved}
          className={`shrink-0 rounded-full px-4 py-2.5 text-sm font-semibold transition ${
            route.view === "saved" ? "bg-ink text-white" : "hover:bg-wash"
          }`}
        >
          Salvati
          {savedCount > 0 && (
            <span key={savedCount} className="fade-in ml-1.5 inline-block opacity-60">
              {savedCount}
            </span>
          )}
        </button>
      </div>
    </header>
  );
}

/* ------------------------------------------------------------------ cards & grid */

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
        className={`relative block w-full overflow-hidden rounded-2xl bg-wash transition duration-300 ${
          item.vote === 1 ? "ring-2 ring-ink ring-offset-2" : ""
        }`}
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
          className={`h-full w-full object-cover transition duration-700 ease-out group-hover:scale-[1.04] ${
            loaded ? "opacity-100 blur-0" : "opacity-0 blur-sm"
          }`}
        />
        <span className="absolute inset-0 bg-gradient-to-t from-black/55 via-black/0 to-black/0 opacity-0 transition duration-300 group-hover:opacity-100" />
        <span
          className={`absolute bottom-3 left-3 line-clamp-2 text-left text-[13px] leading-snug font-medium text-white opacity-0 transition duration-300 group-hover:translate-y-0 group-hover:opacity-100 ${
            onVote ? "right-24" : "right-3"
          } translate-y-1`}
        >
          {item.name}
          {item.source && item.source !== "Wikimedia Commons" && (
            <span className="mt-0.5 block text-[11px] font-normal text-white/75">{item.source}</span>
          )}
        </span>
      </button>

      {item.match != null && (
        <span className="absolute top-2.5 left-2.5 rounded-full bg-white/90 px-2.5 py-1 text-[11px] font-semibold tabular-nums opacity-0 shadow-sm backdrop-blur transition group-hover:opacity-100">
          {Math.round(item.match * 100)}%
        </span>
      )}

      <button
        type="button"
        onClick={() => onSave(item)}
        className={`absolute top-2.5 right-2.5 rounded-full px-3.5 py-2 text-[13px] font-semibold transition duration-200 active:scale-95 ${
          item.saved
            ? "bg-ink text-white opacity-100"
            : "bg-white/95 text-ink opacity-0 shadow-sm group-hover:opacity-100 focus:opacity-100"
        }`}
      >
        {item.saved ? "Salvata" : "Salva"}
      </button>

      {onVote && (
        <div className="absolute right-2.5 bottom-2.5 flex gap-1.5 opacity-0 transition duration-200 group-hover:opacity-100 focus-within:opacity-100">
          <button
            type="button"
            disabled={voting}
            onClick={() => onVote(item, item.vote === 1 ? 0 : 1)}
            title="Più così: affina i risultati verso questa immagine"
            className={`grid h-9 w-9 place-items-center rounded-full shadow-sm transition active:scale-90 ${
              item.vote === 1 ? "bg-ink text-white" : "bg-white/95 text-ink hover:bg-white"
            }`}
          >
            {Icon.plus}
          </button>
          <button
            type="button"
            disabled={voting}
            onClick={() => onVote(item, -1)}
            title="Meno così: allontana i risultati da questa immagine"
            className="grid h-9 w-9 place-items-center rounded-full bg-white/95 text-ink shadow-sm transition hover:bg-white active:scale-90"
          >
            {Icon.minus}
          </button>
        </div>
      )}
    </figure>
  );
}

function Masonry({ items, animate = false, ...handlers }) {
  return (
    <div className="masonry">
      {items.map((it, i) => (
        <Card key={it.title} item={it} index={i} animated={animate && i < ANIMATED_CARDS} {...handlers} />
      ))}
    </div>
  );
}

function SkeletonGrid({ n = 18 }) {
  const heights = [260, 340, 200, 300, 380, 240, 320, 220, 280];
  return (
    <div className="masonry">
      {Array.from({ length: n }, (_, i) => (
        <div key={i} className="shimmer rounded-2xl" style={{ height: heights[i % heights.length] }} />
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

function ProgressBar({ value, active }) {
  return (
    <div className="pointer-events-none fixed inset-x-0 top-0 z-40 h-[3px]">
      <div
        className="h-full bg-ink transition-all duration-700 ease-out"
        style={{ width: `${Math.max(4, value * 100)}%`, opacity: active ? 1 : 0 }}
      />
    </div>
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

  return (
    <main className="px-4 pb-16 md:px-6">
      {(route.view === "feed" || route.view === "featured") && (
        <div className="fade-in mb-5 flex flex-col items-center gap-2">
          <div className="flex gap-1 rounded-full bg-wash p-1">
            {[
              ["feed", "Per te"],
              ["featured", "In evidenza"],
            ].map(([view, label]) => (
              <button
                key={view}
                type="button"
                onClick={() => cardHandlers.onNav(view)}
                className={`rounded-full px-5 py-2 text-sm font-semibold transition ${
                  route.view === view ? "bg-white text-ink shadow-sm" : "text-mute hover:text-ink"
                }`}
              >
                {label}
              </button>
            ))}
          </div>
          <p className="text-xs text-mute">
            {route.view === "feed" && !personal
              ? "Apri e salva qualche immagine: qui nascerà il tuo feed personale."
              : indexed
                ? `${indexed.toLocaleString("it-IT")} immagini nel tuo indice · cresce mentre navighi`
                : "\u00a0"}
          </p>
        </div>
      )}
      {(route.view === "feed" || route.view === "featured") && (
        <div className="-mx-4 mb-6 flex gap-2 overflow-x-auto px-4 pb-1 md:mx-0 md:flex-wrap md:justify-center md:px-0">
          {SUGGESTIONS.map((s, i) => (
            <button
              key={s}
              type="button"
              onClick={() => cardHandlers.onSearch(s)}
              className="rise shrink-0 rounded-full bg-wash px-4 py-2 text-sm font-medium transition hover:-translate-y-0.5 hover:bg-ink hover:text-white"
              style={{ "--i": i }}
            >
              {s}
            </button>
          ))}
        </div>
      )}
      {route.view === "search" && !loading && (
        <div className="fade-in mb-6 flex flex-col items-center gap-1 text-center text-sm text-mute">
          <p>
            {english && english.toLowerCase() !== route.q.toLowerCase()
              ? `Risultati per “${route.q}” · cercato come “${english}”`
              : `Risultati per “${route.q}”`}
          </p>
          <p className="h-5 text-xs">
            {checking ? (
              <span className="inline-flex items-center gap-1.5">
                <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-ink" />
                Controllo che ogni immagine mostri davvero “{english || route.q}”…
              </span>
            ) : hidden ? (
              `${hidden} immagini nascoste: fuori tema o poco belle`
            ) : null}
          </p>
        </div>
      )}
      {route.view === "search" && <ProgressBar value={checking ? 0.6 : 1} active={checking} />}
      {route.view === "saved" && (
        <h1 className="fade-in mt-4 mb-8 text-center text-3xl font-semibold tracking-tight">Salvati</h1>
      )}

      {error && <p className="py-20 text-center text-mute">{error}</p>}
      {loading ? (
        <SkeletonGrid />
      ) : items.length ? (
        <Masonry items={items} animate={route.view === "search"} onOpen={cardHandlers.onOpen} onSave={cardHandlers.onSave} />
      ) : (
        !error && (
          <p className="py-24 text-center text-mute">
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

/** The way here, like a browser history of articles: each image, and the detail that led to the next. */
function Trail({ trail, current, onOpen }) {
  const path = trail.slice(0, 10).reverse();
  if (path.length < 2) return null;
  return (
    <nav className="fade-in mx-auto mb-5 flex max-w-6xl items-center gap-1.5 overflow-x-auto pb-1" aria-label="Il tuo percorso">
      {path.map((t, i) => (
        <span key={t.title} className="flex shrink-0 items-center gap-1.5">
          {i > 0 && (
            <span className="flex items-center gap-1.5 text-mute">
              <span className="h-px w-3 bg-line" />
              {t.anchor && (
                <span
                  title="Il dettaglio che ti ha portato qui"
                  className="h-7 rounded-md bg-wash ring-1 ring-line"
                  style={{ ...cropStyle(t.anchor.src, t.anchor.box), aspectRatio: `${boxAspect(t.anchor.box, t.anchor.ratio)}` }}
                />
              )}
              <span className="h-px w-3 bg-line" />
            </span>
          )}
          <button
            type="button"
            title={t.name}
            disabled={t.title === current}
            onClick={() => onOpen(t)}
            className={`h-9 w-9 overflow-hidden rounded-full bg-wash transition ${
              t.title === current ? "ring-2 ring-ink ring-offset-2" : "opacity-70 hover:-translate-y-0.5 hover:opacity-100"
            }`}
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
 * The image as a page: the things in it (found by DINOv2) are its links. A dot marks each one;
 * hovering lights it up and previews what it is (the Wikipedia concept, once named) and where it
 * leads; clicking dives into it: that part of the image grows into the concept's page (or, without
 * a concept, into the most similar image).
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

  // Darken everything but the hovered thing.
  useEffect(() => {
    const canvas = shade.current;
    if (!canvas || !map) return;
    canvas.width = page.w;
    canvas.height = page.h;
    const ctx = canvas.getContext("2d");
    const img = ctx.createImageData(page.w, page.h);
    for (let i = 0; i < map.length; i += 1) if (map[i] !== hover) img.data[i * 4 + 3] = 140;
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
      className="relative mx-auto rounded-3xl bg-wash shadow-[0_30px_60px_-30px_rgba(0,0,0,0.35)]"
      style={{ aspectRatio: `${ratio}`, width: `min(100%, calc(76vh * ${ratio}))` }}
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
      <div className={`absolute inset-0 overflow-hidden rounded-3xl ${hover >= 0 ? "cursor-pointer" : ""}`}>
        {origin?.thumb ? (
          <>
            {/* The grid thumbnail is already cached: show it at once (it is what the card morphs into). */}
            <img data-hero src={origin.thumb} alt={origin.name} className="h-full w-full object-cover" style={{ viewTransitionName: "hero" }} />
            <img
              src={big}
              alt=""
              onLoad={() => setBigLoaded(true)}
              className={`absolute inset-0 h-full w-full object-cover transition-opacity duration-700 ${bigLoaded ? "opacity-100" : "opacity-0"}`}
            />
          </>
        ) : (
          <div className="shimmer h-full w-full" />
        )}
        <canvas
          ref={shade}
          className={`pointer-events-none absolute inset-0 h-full w-full transition-opacity duration-300 ${hover >= 0 ? "opacity-100" : "opacity-0"}`}
        />
        {diving >= 0 && (
          // The part being followed, drawn over itself: the View Transition grows it into the next image.
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
            className="rise pointer-events-none absolute -translate-x-1/2 -translate-y-1/2"
            style={{ left: `${l.dot[0] * 100}%`, top: `${l.dot[1] * 100}%`, "--i": i * 3 }}
          >
            <span
              className={`block h-3.5 w-3.5 rounded-full border-2 border-white shadow-[0_0_0_1px_rgba(0,0,0,0.25),0_2px_8px_rgba(0,0,0,0.35)] transition duration-300 ${
                hover === i ? "scale-150 bg-white" : "bg-white/30 backdrop-blur"
              }`}
            />
          </span>
        ))}
      </div>

      {shown && (
        // Where this link leads, like Wikipedia's hover preview.
        <div
          key={hover}
          className="fade-in pointer-events-none absolute z-20 w-72 rounded-2xl bg-white p-2 shadow-[0_20px_50px_-12px_rgba(0,0,0,0.45)]"
          style={{
            left: shown.dot[0] > 0.55 ? undefined : `calc(${shown.dot[0] * 100}% + 18px)`,
            right: shown.dot[0] > 0.55 ? `calc(${(1 - shown.dot[0]) * 100}% + 18px)` : undefined,
            top: shown.dot[1] > 0.55 ? undefined : `calc(${shown.dot[1] * 100}% - 12px)`,
            bottom: shown.dot[1] > 0.55 ? `calc(${(1 - shown.dot[1]) * 100}% - 12px)` : undefined,
          }}
        >
          {idea && (
            <div className="px-2 pt-1.5 pb-2.5">
              <p className="text-[15px] leading-snug font-semibold first-letter:uppercase">{idea.name}</p>
              {idea.extract && <p className="mt-1 line-clamp-4 text-[13px] leading-snug text-mute">{idea.extract}</p>}
            </div>
          )}
          <div className="grid grid-cols-3 gap-1.5">
            {shown.items.slice(0, idea ? 3 : 4).map((it, k) => (
              <img
                key={it.title}
                src={it.thumb}
                alt=""
                className={`w-full rounded-xl bg-wash object-cover ${!idea && k === 0 ? "col-span-3 aspect-[16/10]" : "aspect-square"}`}
              />
            ))}
          </div>
          <p className="px-1.5 pt-2 pb-0.5 text-xs text-mute">{idea ? "Clicca per leggere la voce" : "Clicca per entrare"}</p>
        </div>
      )}
    </div>
  );
}

/** "In questa immagine": every thread leaving this image, the part on the left, where it goes on the right. */
function Threads({ origin, page, concepts, hover, setHover, onFollow, onConcept }) {
  const ratio = origin?.width && origin?.height ? origin.width / origin.height : 1;
  if (!page) {
    return (
      <div className="flex flex-col gap-4">
        {[0, 1, 2].map((i) => (
          <div key={i} className="flex gap-3">
            <div className="shimmer h-28 w-28 shrink-0 rounded-2xl" />
            <div className="shimmer h-28 flex-1 rounded-2xl" />
          </div>
        ))}
      </div>
    );
  }
  if (!page.links.length) return <p className="text-sm text-mute">DINOv2 non ha trovato parti da seguire in questa immagine.</p>;
  const src = bigThumb(origin);
  return (
    <div className="flex flex-col gap-4">
      {page.links.map((l, i) => (
        <div
          key={i}
          className={`rise flex gap-3 rounded-3xl p-1.5 transition ${hover === i ? "bg-wash" : ""}`}
          style={{ "--i": i }}
          onMouseEnter={() => setHover(i)}
          onMouseLeave={() => setHover(-1)}
        >
          <button
            type="button"
            onClick={() => (concepts?.[i] ? onConcept(l, concepts[i], null) : onFollow(l, l.items[0], null))}
            title="Segui questo dettaglio"
            className="h-28 max-w-48 shrink-0 rounded-2xl bg-wash bg-no-repeat ring-1 ring-black/5 transition hover:scale-[1.03] md:h-36"
            style={{ ...cropStyle(src, l.box), aspectRatio: `${Math.min(1.6, Math.max(0.6, boxAspect(l.box, ratio)))}` }}
          />
          <div className="flex min-w-0 flex-1 flex-col gap-2">
          {concepts === null ? (
            <span className="shimmer h-5 w-40 rounded-full" />
          ) : concepts?.[i] ? (
            <button type="button" onClick={() => onConcept(l, concepts[i], null)} className="group/c min-w-0 text-left">
              <span className="text-[15px] font-semibold first-letter:uppercase group-hover/c:underline">{concepts[i].name}</span>
              {concepts[i].extract && (
                <span className="mt-0.5 line-clamp-2 text-sm leading-snug text-mute">{concepts[i].extract}</span>
              )}
            </button>
          ) : null}
          <div className="flex min-w-0 flex-1 gap-2 overflow-x-auto">
            {l.items.map((it) => (
              <button
                key={it.title}
                type="button"
                title={it.name}
                onClick={(e) => onFollow(l, it, e.currentTarget.querySelector("img"))}
                className="h-24 shrink-0 overflow-hidden rounded-2xl bg-wash transition hover:opacity-90 md:h-28"
                style={{ aspectRatio: it.width && it.height ? `${it.width} / ${it.height}` : "1" }}
              >
                <img src={it.thumb} alt={it.name} loading="lazy" className="h-full w-full object-cover" />
              </button>
            ))}
          </div>
          </div>
        </div>
      ))}
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
      ? "Guardo le immagini collegate…"
      : phase === "refining"
        ? `Affino · ${analysed} di ${info.candidates} immagini analizzate`
        : phase === "error"
          ? error
          : `${analysed} immagini analizzate${info.pending ? ` · ${info.pending} ancora da guardare` : ""}`;

  return (
    <main className="relative px-4 pb-16 md:px-6">
      <ProgressBar value={info.candidates ? analysed / info.candidates : 0.08} active={busy} />

      {/* Ambient glow made of the image's own colours */}
      {origin?.thumb && (
        <div className="pointer-events-none absolute inset-x-0 -top-[72px] -z-10 h-[90vh] overflow-hidden [mask-image:linear-gradient(to_bottom,black,transparent)]">
          <img src={origin.thumb} alt="" className="fade-in h-full w-full scale-125 object-cover opacity-30 blur-[90px] saturate-150" />
        </div>
      )}

      <Trail
        trail={trail}
        current={route.file}
        onOpen={(t) => (t.kind === "concept" ? cardHandlers.onConcept({ qid: t.title, name: t.name, thumb: t.thumb }) : cardHandlers.onOpen(t))}
      />

      <section className="mx-auto mb-14 grid max-w-6xl gap-6 md:mt-2 md:grid-cols-[minmax(0,1.5fr)_minmax(280px,1fr)] md:gap-10">
        <div className="relative">
          <button
            type="button"
            onClick={onBack}
            className="absolute top-3 left-3 z-10 grid h-10 w-10 place-items-center rounded-full bg-white/95 shadow-sm transition hover:scale-105 hover:bg-white"
            aria-label="Indietro"
          >
            {Icon.back}
          </button>
          <PageHero
            origin={origin}
            page={page}
            concepts={concepts}
            hover={hover}
            setHover={setHover}
            onFollow={follow}
            onConcept={openConcept}
          />
        </div>

        <aside className="flex flex-col gap-6 md:pt-2">
          <div className="fade-in">
            <h1 className="text-2xl leading-tight font-semibold tracking-tight break-words md:text-[28px]">
              {origin?.name || " "}
            </h1>
            {origin?.author !== undefined && (
              <p className="mt-2 text-sm text-mute">{[origin.author, origin.license].filter(Boolean).join(" · ")}</p>
            )}
          </div>

          {origin && (
            <div className="flex flex-wrap gap-2">
              <button
                type="button"
                onClick={() => cardHandlers.onSave(origin)}
                className="rounded-full bg-ink px-5 py-3 text-sm font-semibold text-white transition hover:bg-black active:scale-95"
              >
                {origin.saved ? "Salvata" : "Salva"}
              </button>
              {origin.page_url && (
                <a
                  href={origin.page_url}
                  target="_blank"
                  rel="noreferrer"
                  className="flex items-center gap-1.5 rounded-full bg-wash px-5 py-3 text-sm font-semibold transition hover:bg-line"
                >
                  Apri su {origin.source || "Wikimedia Commons"} {Icon.out}
                </a>
              )}
            </div>
          )}

          <div className="rounded-2xl border border-line bg-white/70 p-5 backdrop-blur">
            <div className="mb-3 flex items-baseline justify-between">
              <span className="text-sm font-semibold">Simili per</span>
              <span key={lookLabel(weight)} className="fade-in text-sm text-mute">
                {lookLabel(weight)}
              </span>
            </div>
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
            <div className="mt-2 flex justify-between text-xs text-mute">
              <span>Soggetto</span>
              <span>Aspetto</span>
            </div>
            <p className="mt-4 text-xs leading-relaxed text-mute">
              Passa sopra un'immagine e usa <b className="text-ink">+</b> per “più così” o <b className="text-ink">−</b> per
              “meno così”: i risultati si riordinano e l'app impara i tuoi gusti.
            </p>
          </div>

          <p className="text-sm leading-relaxed text-mute">
            Ogni cosa in questa immagine è un collegamento, come le parole di una voce di Wikipedia: passaci sopra per
            scoprire cos'è, clicca per leggerne la voce e continuare da lì.
          </p>
        </aside>
      </section>

      <section className="mx-auto mb-14 max-w-6xl">
        <h2 className="mb-4 text-xl font-semibold tracking-tight">In questa immagine</h2>
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

      <div className="mx-auto mb-5 flex max-w-6xl items-center justify-between gap-4">
        <h2 className="flex items-center gap-2 text-xl font-semibold tracking-tight">
          Simili
          {busy && <span className="text-mute">{Icon.spark}</span>}
        </h2>
        <span className="flex items-center gap-2 text-right text-xs text-mute">
          {busy && <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-ink" />}
          {status}
        </span>
      </div>

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
        phase !== "error" && <p className="py-16 text-center text-mute">Nessuna immagine simile trovata.</p>
      )}

      <InfiniteSentinel onVisible={loadMore} active={phase === "ready" && (info.pending > 0 || info.more)} />
      {phase === "refining" && items.length > 0 && (
        <p className="mt-8 flex items-center justify-center gap-2 text-sm text-mute">
          <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-ink" />
          Cerco altre immagini simili…
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
    <main className="px-4 pb-20 md:px-6">
      <ProgressBar value={0.6} active={loading} />
      <Trail
        trail={trail}
        current={route.qid}
        onOpen={(t) => (t.kind === "concept" ? cardHandlers.onConcept({ qid: t.title, name: t.name, thumb: t.thumb }) : cardHandlers.onOpen(t))}
      />
      <article className="mx-auto mb-14 grid max-w-6xl gap-8 md:mt-2 md:grid-cols-[minmax(0,1fr)_minmax(0,1.1fr)] md:gap-12">
        <div className="overflow-hidden rounded-3xl bg-wash shadow-[0_30px_60px_-30px_rgba(0,0,0,0.35)] md:sticky md:top-24 md:self-start">
          {cover ? (
            <img data-hero src={cover} alt={data?.name || ""} className="max-h-[70vh] w-full object-cover" style={{ viewTransitionName: "hero" }} />
          ) : (
            <div className="shimmer aspect-[4/3]" style={{ viewTransitionName: "hero" }} />
          )}
        </div>
        <div className="fade-in flex flex-col gap-5 md:pt-4">
          <p className="text-xs font-semibold tracking-[0.18em] text-mute uppercase">Voce</p>
          <h1 className="text-4xl leading-[1.05] font-semibold tracking-tight first-letter:uppercase md:text-5xl">{data?.name || " "}</h1>
          {data?.description && <p className="text-[15px] text-mute first-letter:uppercase">{data.description.split(" / ").pop()}</p>}
          {loading && !paragraphs.length ? (
            <div className="flex flex-col gap-2">
              {[0, 1, 2, 3].map((i) => (
                <span key={i} className="shimmer h-4 rounded-full" style={{ width: `${95 - i * 12}%` }} />
              ))}
            </div>
          ) : (
            paragraphs.map((t, i) => (
              <p key={i} className="text-[17px] leading-relaxed text-ink/85">
                {t}
              </p>
            ))
          )}
          {error && <p className="text-mute">{error}</p>}
          {data?.url && (
            <a
              href={data.url}
              target="_blank"
              rel="noreferrer"
              className="flex w-fit items-center gap-1.5 rounded-full bg-wash px-5 py-3 text-sm font-semibold transition hover:bg-line"
            >
              Leggi tutto su Wikipedia{data.lang === "en" ? " (in inglese)" : ""} {Icon.out}
            </a>
          )}
          {data?.related?.length > 0 && (
            <div className="mt-2">
              <p className="mb-2.5 text-xs font-semibold tracking-[0.18em] text-mute uppercase">Voci collegate</p>
              <div className="flex flex-wrap gap-2">
                {data.related.map((r, i) => (
                  <button
                    key={r.qid}
                    type="button"
                    onClick={() => cardHandlers.onConcept(r)}
                    className="rise rounded-full bg-wash px-4 py-2 text-sm font-medium transition first-letter:uppercase hover:-translate-y-0.5 hover:bg-ink hover:text-white"
                    style={{ "--i": i }}
                  >
                    {r.name}
                  </button>
                ))}
              </div>
            </div>
          )}
        </div>
      </article>

      <section className="mx-auto max-w-[1800px]">
        <h2 className="mb-4 text-xl font-semibold tracking-tight">Immagini</h2>
        {loading && !items.length ? (
          <SkeletonGrid n={12} />
        ) : items.length ? (
          <Masonry items={items} onOpen={cardHandlers.onOpen} onSave={cardHandlers.onSave} />
        ) : (
          !loading && <p className="py-16 text-center text-mute">Nessuna immagine per questa voce, per ora.</p>
        )}
        <InfiniteSentinel onVisible={more} active={!loading && next != null} />
      </section>
    </main>
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

      <div
        className={`pointer-events-none fixed bottom-6 left-1/2 z-50 -translate-x-1/2 rounded-full bg-ink px-5 py-3 text-sm font-medium text-white shadow-lg transition duration-300 ${
          toast ? "translate-y-0 opacity-100" : "translate-y-4 opacity-0"
        }`}
        role="status"
        aria-live="polite"
      >
        {lastToast.current}
      </div>
    </div>
  );
}
