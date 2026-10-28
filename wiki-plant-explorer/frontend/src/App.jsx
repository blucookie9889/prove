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
  if (p.get("file")) return { view: "file", file: p.get("file"), q: p.get("q") || "", lens: p.get("lens") || "simili" };
  if (p.get("drift")) return { view: "drift", file: p.get("drift"), q: "" };
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
    if (r.lens && r.lens !== "simili") p.set("lens", r.lens);
  } else if (r.view === "drift") p.set("drift", r.file);
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

function pushTrail(item) {
  const next = [{ title: item.title, thumb: item.thumb, name: item.name, width: item.width, height: item.height }]
    .concat(readTrail().filter((t) => t.title !== item.title))
    .slice(0, 12);
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
  eye: (
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z" />
      <circle cx="12" cy="12" r="3" />
    </svg>
  ),
  drift: (
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M4 19c4 0 4-6 8-6s4 6 8 6M4 11c4 0 4-6 8-6s4 6 8 6" />
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
        {item.focus && loaded && (
          // "Dettaglio": a soft spotlight on the region of this image that matches the picked detail.
          <span
            className="fade-in pointer-events-none absolute inset-0 transition duration-300 group-hover:opacity-40"
            style={{
              background: `radial-gradient(ellipse ${Math.round(item.focus.r * 75)}% ${Math.round(item.focus.r * 75)}% at ${
                item.focus.x * 100
              }% ${item.focus.y * 100}%, transparent 55%, rgba(0,0,0,0.5) 100%)`,
            }}
          />
        )}
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
            <button
              type="button"
              disabled={!items.length}
              onClick={() => cardHandlers.onDrift(items[Math.floor(Math.random() * Math.min(items.length, 24))])}
              title="Parti da un'immagine a caso di questa pagina e lasciati portare"
              className="flex items-center gap-1.5 rounded-full px-5 py-2 text-sm font-semibold text-mute transition hover:text-ink disabled:opacity-40"
            >
              {Icon.drift} Deriva
            </button>
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

function Trail({ trail, current, onOpen }) {
  const past = trail.filter((t) => t.title !== current);
  if (!past.length) return null;
  return (
    <div className="fade-in">
      <p className="mb-2.5 text-xs font-semibold tracking-wide text-mute uppercase">Il tuo percorso</p>
      <div className="flex flex-wrap gap-2">
        {past.slice(0, 8).map((t, i) => (
          <button
            key={t.title}
            type="button"
            title={t.name}
            onClick={() => onOpen(t)}
            className="rise h-11 w-11 overflow-hidden rounded-full bg-wash ring-2 ring-white transition hover:-translate-y-0.5 hover:ring-ink"
            style={{ "--i": i }}
          >
            <img src={t.thumb} alt={t.name} className="h-full w-full object-cover" />
          </button>
        ))}
      </div>
    </div>
  );
}

const LENSES = [
  ["simili", "Simili", "Aspetto e soggetto, affinati dai tuoi + e −"],
  ["rime", "Rime", "Stessa forma, un altro mondo: DINOv2 le vede uguali, CLIP no"],
  ["materia", "Materia", "Stesse superfici, grane e colori, qualunque cosa sia"],
  ["forma", "Forma", "Stessa composizione: dove stanno masse, linee e vuoti"],
  ["dettaglio", "Dettaglio", "Tocca un punto dell'immagine: cerco dove altro compare"],
];

const LINKS = {
  forma: "stessa composizione",
  materia: "stessa materia",
  sguardo: "stesso aspetto",
  senso: "stesso soggetto",
  rima: "rima visiva",
};

function bytes(b64) {
  return Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
}

/** Paint a w x h grid (one value per DINOv2 patch) into a canvas; the browser smooths it when scaled up. */
function paintGrid(canvas, w, h, pixel) {
  if (!canvas) return;
  canvas.width = w;
  canvas.height = h;
  const ctx = canvas.getContext("2d");
  const img = ctx.createImageData(w, h);
  for (let i = 0; i < w * h; i += 1) img.data.set(pixel(i), i * 4);
  ctx.putImageData(img, 0, 0);
}

/**
 * The big image. With ``eyes`` on, DINOv2's view of it is laid over: the first three principal
 * components of its patch tokens as colours (patches of the same kind of thing share a colour).
 * With ``picking`` on, the region under the pointer lights up and a click picks a detail.
 */
function Hero({ origin, map, eyes, picking, pick, mask, onPick }) {
  const [bigLoaded, setBigLoaded] = useState(false);
  const [hoverSeg, setHoverSeg] = useState(-1);
  const colours = useRef(null);
  const shade = useRef(null);
  const ratio = origin?.width && origin?.height ? origin.width / origin.height : map ? map.w / map.h : 4 / 3;

  useEffect(() => {
    if (!map) return;
    const rgb = map.rgbBytes;
    paintGrid(colours.current, map.w, map.h, (i) => [rgb[i * 3], rgb[i * 3 + 1], rgb[i * 3 + 2], 255]);
  }, [map]);

  // Darken everything but the hovered region (or, after a click, the patches that were picked).
  useEffect(() => {
    if (!map) return;
    const seg = map.segBytes;
    const lit = mask ? (i) => mask[i] : hoverSeg >= 0 ? (i) => seg[i] === hoverSeg : null;
    paintGrid(shade.current, map.w, map.h, (i) => (lit && !lit(i) ? [0, 0, 0, 150] : [0, 0, 0, 0]));
  }, [map, hoverSeg, mask]);

  const at = (e) => {
    const r = e.currentTarget.getBoundingClientRect();
    return { x: Math.min(0.999, Math.max(0, (e.clientX - r.left) / r.width)), y: Math.min(0.999, Math.max(0, (e.clientY - r.top) / r.height)) };
  };

  return (
    <div
      className="relative mx-auto overflow-hidden rounded-3xl bg-wash shadow-[0_30px_60px_-30px_rgba(0,0,0,0.35)]"
      style={{ aspectRatio: `${ratio}`, width: `min(100%, calc(78vh * ${ratio}))` }}
    >
      {origin?.thumb ? (
        <>
          {/* The grid thumbnail is already cached: show it at once (it is what the card morphs into). */}
          <img data-hero src={origin.thumb} alt={origin.name} className="h-full w-full object-cover" style={{ viewTransitionName: "hero" }} />
          <img
            src={bigThumb(origin)}
            alt=""
            onLoad={() => setBigLoaded(true)}
            className={`absolute inset-0 h-full w-full object-cover transition-opacity duration-700 ${bigLoaded ? "opacity-100" : "opacity-0"}`}
          />
        </>
      ) : (
        <div className="shimmer h-full w-full" />
      )}
      <canvas
        ref={colours}
        className={`pointer-events-none absolute inset-0 h-full w-full transition-opacity duration-700 ${map && eyes ? "opacity-90" : "opacity-0"}`}
      />
      <canvas
        ref={shade}
        className={`pointer-events-none absolute inset-0 h-full w-full transition-opacity duration-300 ${map && picking ? "opacity-100" : "opacity-0"}`}
      />
      {picking && map && (
        <button
          type="button"
          aria-label="Scegli un dettaglio"
          className="absolute inset-0 cursor-crosshair"
          onMouseMove={(e) => {
            if (mask) return;
            const { x, y } = at(e);
            setHoverSeg(map.segBytes[Math.floor(y * map.h) * map.w + Math.floor(x * map.w)]);
          }}
          onMouseLeave={() => setHoverSeg(-1)}
          onClick={(e) => onPick(at(e))}
        />
      )}
      {picking && pick && (
        <span
          key={`${pick.x},${pick.y}`}
          className="pointer-events-none absolute h-9 w-9 -translate-x-1/2 -translate-y-1/2 rounded-full border-2 border-white shadow-[0_0_0_1px_rgba(0,0,0,0.25)]"
          style={{ left: `${pick.x * 100}%`, top: `${pick.y * 100}%` }}
        >
          <span className="absolute inset-0 animate-ping rounded-full border-2 border-white [animation-iteration-count:2]" />
        </span>
      )}
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
  const run = useRef(0);
  const shown = useRef(false);

  const limit = useRef(SIMILAR_PAGE);

  // Lenses other than "simili": each a different way of being alike, ranked over the whole index.
  const [lens, setLens] = useState(route.lens || "simili");
  const [lensItems, setLensItems] = useState([]);
  const [lensBusy, setLensBusy] = useState(false);
  const [lensMore, setLensMore] = useState(false);
  const [map, setMap] = useState(null);
  const [eyes, setEyes] = useState(false);
  const [pick, setPick] = useState(null);
  const [mask, setMask] = useState(null);
  const [progress, setProgress] = useState(null);
  const lensLimit = useRef(SIMILAR_PAGE);
  const lensRun = useRef(0);

  const chooseLens = (next) => {
    setLens(next);
    window.history.replaceState(null, "", routeUrl({ ...route, lens: next }));
  };

  // DINOv2's patch map of the origin: for its "eyes" and for picking a detail.
  useEffect(() => {
    if (map || (!eyes && lens !== "dettaglio")) return;
    api(`/dino?title=${encodeURIComponent(route.file)}`)
      .then((m) => setMap({ ...m, rgbBytes: bytes(m.rgb), segBytes: bytes(m.segments) }))
      .catch(() => {});
  }, [eyes, lens, map, route.file]);

  useEffect(() => {
    if (lens === "simili") return;
    api("/health")
      .then((h) => setProgress({ dense: h.dense, indexed: h.indexed }))
      .catch(() => {});
  }, [lens]);

  const fetchLens = useCallback(
    async (append) => {
      if (lens === "simili" || (lens === "dettaglio" && !pick)) return;
      const id = ++lensRun.current;
      setLensBusy(true);
      try {
        const qs = new URLSearchParams({ title: route.file, limit: String(lensLimit.current) });
        let data;
        if (lens === "dettaglio") {
          qs.set("x", String(pick.x));
          qs.set("y", String(pick.y));
          data = await api(`/detail?${qs}`);
          if (id === lensRun.current) setMask(bytes(data.mask));
        } else {
          qs.set("kind", lens);
          data = await api(`/lens?${qs}`);
        }
        if (id !== lensRun.current) return;
        const apply = () =>
          setLensItems((prev) => {
            if (!append) return data.items;
            const seen = new Set(prev.map((p) => p.title));
            return [...prev, ...data.items.filter((i) => !seen.has(i.title))];
          });
        if (append) apply();
        else withTransition(apply);
        setLensMore(data.items.length >= lensLimit.current);
        setError("");
      } catch (e) {
        if (id === lensRun.current) setError(e.message);
      } finally {
        if (id === lensRun.current) setLensBusy(false);
      }
    },
    [lens, pick, route.file],
  );

  useEffect(() => {
    lensLimit.current = SIMILAR_PAGE;
    setLensItems([]);
    setLensMore(false);
    if (lens !== "dettaglio") {
      setPick(null);
      setMask(null);
    }
    fetchLens(false);
  }, [fetchLens, lens]);

  const lensLoadMore = useCallback(() => {
    lensLimit.current += SIMILAR_PAGE;
    fetchLens(true);
  }, [fetchLens]);

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
        setTrail(pushTrail(o));
      })
      .catch(() => {});
    refresh(FIRST_BUDGET, weight);
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

  const analysed = info.candidates - info.pending;
  const similar = lens === "simili";
  const busy = similar ? phase === "loading" || phase === "refining" : lensBusy;
  const lensInfo = LENSES.find(([k]) => k === lens) || LENSES[0];
  const learning =
    progress && progress.dense < progress.indexed
      ? `DINOv2 sta studiando l'indice: ${progress.dense.toLocaleString("it-IT")} di ${progress.indexed.toLocaleString("it-IT")} immagini`
      : progress
        ? `${progress.indexed.toLocaleString("it-IT")} immagini nell'indice`
        : "";
  const status = !similar
    ? lens === "dettaglio" && !pick
      ? "Tocca l'immagine qui sopra"
      : lensBusy
        ? "Cerco nell'indice…"
        : error || learning
    : phase === "loading"
      ? "Guardo le immagini collegate…"
      : phase === "refining"
        ? `Affino · ${analysed} di ${info.candidates} immagini analizzate`
        : phase === "error"
          ? error
          : `${analysed} immagini analizzate${info.pending ? ` · ${info.pending} ancora da guardare` : ""}`;
  const shownItems = similar ? items : lensItems;

  return (
    <main className="relative px-4 pb-16 md:px-6">
      <ProgressBar value={similar ? (info.candidates ? analysed / info.candidates : 0.08) : 0.6} active={busy} />

      {/* Ambient glow made of the image's own colours */}
      {origin?.thumb && (
        <div className="pointer-events-none absolute inset-x-0 -top-[72px] -z-10 h-[90vh] overflow-hidden [mask-image:linear-gradient(to_bottom,black,transparent)]">
          <img src={origin.thumb} alt="" className="fade-in h-full w-full scale-125 object-cover opacity-30 blur-[90px] saturate-150" />
        </div>
      )}

      <section className="mx-auto mb-12 grid max-w-6xl gap-6 md:mt-2 md:grid-cols-[minmax(0,1.5fr)_minmax(280px,1fr)] md:gap-10">
        <div className="relative">
          <button
            type="button"
            onClick={onBack}
            className="absolute top-3 left-3 z-10 grid h-10 w-10 place-items-center rounded-full bg-white/95 shadow-sm transition hover:scale-105 hover:bg-white"
            aria-label="Indietro"
          >
            {Icon.back}
          </button>
          <button
            type="button"
            onClick={() => setEyes((v) => !v)}
            className={`absolute top-3 right-3 z-10 flex items-center gap-1.5 rounded-full px-3.5 py-2 text-[13px] font-semibold shadow-sm transition hover:scale-105 ${
              eyes ? "bg-ink text-white" : "bg-white/95 text-ink"
            }`}
            title="Colora l'immagine come la vede DINOv2: parti dello stesso tipo, stesso colore"
          >
            {Icon.eye} Occhi di DINO
          </button>
          <Hero
            origin={origin}
            map={map}
            eyes={eyes}
            picking={lens === "dettaglio"}
            pick={pick}
            mask={mask}
            onPick={(p) => {
              setMask(null);
              setPick(p);
            }}
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
              <button
                type="button"
                onClick={() => cardHandlers.onDrift(origin)}
                className="flex items-center gap-1.5 rounded-full bg-wash px-5 py-3 text-sm font-semibold transition hover:bg-line"
                title="Un viaggio da qui: ogni passo somiglia al precedente in un modo diverso"
              >
                {Icon.drift} Deriva da qui
              </button>
              {origin.page_url && (
                <a
                  href={origin.page_url}
                  target="_blank"
                  rel="noreferrer"
                  className="flex items-center gap-1.5 rounded-full bg-wash px-5 py-3 text-sm font-semibold transition hover:bg-line"
                >
                  {origin.source || "Wikimedia Commons"} {Icon.out}
                </a>
              )}
            </div>
          )}

          {similar ? (
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
          ) : (
            <div key={lens} className="fade-in rounded-2xl border border-line bg-white/70 p-5 backdrop-blur">
              <p className="text-sm font-semibold">{lensInfo[1]}</p>
              <p className="mt-1.5 text-sm leading-relaxed text-mute">{lensInfo[2]}</p>
              {lens === "dettaglio" && (
                <p className="mt-3 text-xs leading-relaxed text-mute">
                  DINOv2 divide l'immagine in regioni: passa sopra per vederle, clicca per sceglierne una. Nei risultati la parte che
                  corrisponde resta in luce.
                </p>
              )}
            </div>
          )}

          <Trail trail={trail} current={route.file} onOpen={(t) => cardHandlers.onOpen(t)} />
        </aside>
      </section>

      <div className="mx-auto mb-6 flex max-w-6xl flex-col gap-3 md:flex-row md:items-center md:justify-between">
        <div className="-mx-4 flex gap-1 overflow-x-auto px-4 md:mx-0 md:px-0">
          {LENSES.map(([key, label, hint]) => (
            <button
              key={key}
              type="button"
              title={hint}
              onClick={() => chooseLens(key)}
              className={`shrink-0 rounded-full px-4 py-2 text-[15px] font-semibold tracking-tight transition ${
                lens === key ? "bg-ink text-white" : "text-mute hover:bg-wash hover:text-ink"
              }`}
            >
              {label}
            </button>
          ))}
        </div>
        <span className="flex items-center gap-2 text-xs text-mute md:text-right">
          {busy && <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-ink" />}
          {status}
        </span>
      </div>

      {(similar ? phase === "loading" : lensBusy) && !shownItems.length ? (
        <SkeletonGrid n={12} />
      ) : shownItems.length ? (
        <Masonry
          items={shownItems}
          animate
          onOpen={cardHandlers.onOpen}
          onSave={cardHandlers.onSave}
          onVote={similar ? onVote : undefined}
          voting={voting}
        />
      ) : lens === "dettaglio" && !pick ? (
        <p className="py-16 text-center text-mute">Scegli un punto dell'immagine: una foglia, un occhio, una finestra, una crepa.</p>
      ) : (
        !busy &&
        phase !== "error" && <p className="py-16 text-center text-mute">Nessuna immagine trovata.</p>
      )}

      {similar ? (
        <InfiniteSentinel onVisible={loadMore} active={phase === "ready" && (info.pending > 0 || info.more)} />
      ) : (
        <InfiniteSentinel onVisible={lensLoadMore} active={!lensBusy && lensMore} />
      )}
      {busy && shownItems.length > 0 && (
        <p className="mt-8 flex items-center justify-center gap-2 text-sm text-mute">
          <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-ink" />
          Cerco altre immagini…
        </p>
      )}
    </main>
  );
}

/* ------------------------------------------------------------------ drift ("deriva") */

const DRIFT_STEPS = 8;

/**
 * A walk through the index: each image is alike to the one before it in one way (composition,
 * material, look, subject, or a visual rhyme), and the way changes at every step. The server draws
 * among the best few candidates, so no two walks are the same.
 */
function Drift({ route, cardHandlers }) {
  const [start, setStart] = useState(null);
  const [steps, setSteps] = useState([]);
  const [busy, setBusy] = useState(false);
  const [ended, setEnded] = useState(false);
  const [error, setError] = useState("");
  const [round, setRound] = useState(0);
  const walk = useRef(null);
  const loading = useRef(false);
  const current = useRef(round);
  current.current = round;
  const moreRef = useRef(null);

  const more = useCallback(async () => {
    if (loading.current) return;
    loading.current = true;
    setBusy(true);
    const mine = round;
    try {
      const qs = new URLSearchParams({ title: route.file, steps: String(DRIFT_STEPS) });
      if (walk.current) qs.set("walk", walk.current);
      const data = await api(`/drift?${qs}`);
      if (mine !== current.current) return; // "Ricomincia" was pressed meanwhile
      walk.current = data.walk;
      setSteps((prev) => [...prev, ...data.steps]);
      setEnded(data.steps.length === 0);
      setError("");
    } catch (e) {
      setError(e.message);
    } finally {
      loading.current = false;
      setBusy(false);
      // A restart pressed while this step was loading: start the new walk now.
      if (mine !== current.current) moreRef.current();
    }
  }, [route.file, round]);
  moreRef.current = more;

  useEffect(() => {
    api(`/file?title=${encodeURIComponent(route.file)}`)
      .then(setStart)
      .catch(() => {});
  }, [route.file]);

  useEffect(() => {
    walk.current = null;
    setSteps([]);
    setEnded(false);
    more();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [route.file, round]);

  useEffect(() => {
    const onSaved = (e) => setSteps((prev) => prev.map((p) => (p.title === e.detail.title ? { ...p, saved: e.detail.saved } : p)));
    window.addEventListener("saved-changed", onSaved);
    return () => window.removeEventListener("saved-changed", onSaved);
  }, []);

  return (
    <main className="px-4 pb-24 md:px-6">
      <ProgressBar value={0.6} active={busy} />
      <div className="fade-in mx-auto mt-6 mb-12 flex max-w-xl flex-col items-center text-center">
        <p className="mb-3 text-xs font-semibold tracking-[0.2em] text-mute uppercase">Deriva</p>
        {start?.thumb && (
          <button type="button" onClick={() => cardHandlers.onOpen(start)} className="mb-5 h-24 w-24 overflow-hidden rounded-full bg-wash shadow-lg ring-4 ring-white transition hover:scale-105">
            <img src={start.thumb} alt={start.name} className="h-full w-full object-cover" />
          </button>
        )}
        <h1 className="text-2xl font-semibold tracking-tight">{start?.name || " "}</h1>
        <p className="mt-3 text-sm leading-relaxed text-mute">
          Ogni immagine somiglia alla precedente in un modo diverso: per composizione, materia, aspetto, soggetto o per rima
          visiva. Nessuna deriva è uguale a un'altra.
        </p>
        <button
          type="button"
          onClick={() => setRound((r) => r + 1)}
          className="mt-5 flex items-center gap-1.5 rounded-full bg-wash px-5 py-2.5 text-sm font-semibold transition hover:bg-line"
        >
          {Icon.drift} Ricomincia da qui
        </button>
      </div>

      <ol className="mx-auto flex max-w-2xl flex-col items-center">
        {steps.map((s, i) => (
          <li key={`${s.title}-${i}`} className="rise flex w-full flex-col items-center" style={{ "--i": i % DRIFT_STEPS }}>
            <div className="flex flex-col items-center py-3">
              <span className="h-8 w-px bg-line" />
              <span className="my-1.5 rounded-full border border-line bg-white px-3 py-1 text-xs font-medium text-mute">
                {LINKS[s.link] || s.link}
                {s.match != null && <span className="ml-1.5 tabular-nums opacity-60">{Math.round(s.match * 100)}%</span>}
              </span>
              <span className="h-8 w-px bg-line" />
            </div>
            <div className={`w-full max-w-[560px] ${i % 2 ? "md:translate-x-16" : "md:-translate-x-16"}`}>
              <Card item={s} index={0} onOpen={cardHandlers.onOpen} onSave={cardHandlers.onSave} />
            </div>
          </li>
        ))}
      </ol>

      {error && <p className="py-10 text-center text-mute">{error}</p>}
      {ended && steps.length > 0 && (
        <p className="py-10 text-center text-sm text-mute">La deriva si è fermata qui: l'indice non ha altro di abbastanza vicino.</p>
      )}
      {busy && (
        <p className="mt-10 flex items-center justify-center gap-2 text-sm text-mute">
          <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-ink" />
          Scelgo il prossimo passo…
        </p>
      )}
      <InfiniteSentinel onVisible={more} active={!busy && !ended && !error && steps.length > 0} />
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
    (item, imgEl) => {
      document.querySelectorAll("[data-hero]").forEach((el) => {
        el.style.viewTransitionName = "none";
      });
      if (imgEl) imgEl.style.viewTransitionName = "hero";
      const q = route.view === "search" || route.view === "file" ? route.q : "";
      api("/view", { body: { title: item.title, item } }).catch(() => {});
      withTransition(() => {
        setPreview(item);
        go({ view: "file", file: item.title, q });
      });
    },
    [go, route],
  );

  const cardHandlers = {
    onOpen,
    onSave,
    onSearch: (q) => go({ view: "search", q }),
    onNav: (view) => go({ view, q: "" }),
    onDrift: (item) => withTransition(() => go({ view: "drift", file: item.title, q: "" })),
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
      {route.view === "drift" ? (
        <Drift key={route.file} route={route} cardHandlers={cardHandlers} />
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
