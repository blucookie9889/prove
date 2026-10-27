import { useCallback, useEffect, useRef, useState } from "react";
import { flushSync } from "react-dom";

const SUGGESTIONS = ["foglie", "texture", "architettura gotica", "minerali", "conchiglie", "nuvole", "farfalle", "mosaici"];
// First answer embeds only a few new images so it comes back fast; follow-up rounds refine it
// (each round also explores the categories of the best matches so far).
const FIRST_BUDGET = 24;
const REFINE_BUDGET = 64;
const REFINE_ROUNDS = 3;
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
  if (p.get("view") === "saved") return { view: "saved", q: "" };
  if (p.get("q")) return { view: "search", q: p.get("q") };
  return { view: "feed", q: "" };
}

function routeUrl(r) {
  const p = new URLSearchParams();
  if (r.view === "file") {
    p.set("file", r.file);
    if (r.q) p.set("q", r.q);
  } else if (r.view === "saved") p.set("view", "saved");
  else if (r.view === "search") p.set("q", r.q);
  const s = p.toString();
  return s ? `?${s}` : window.location.pathname;
}

function bigThumb(item) {
  if (!item) return "";
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
            placeholder="Cerca su tutto Wikimedia Commons"
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
  const [error, setError] = useState("");
  const busy = useRef(false);

  const load = useCallback(
    async (cursor, signal) => {
      busy.current = true;
      try {
        let data;
        if (route.view === "saved") data = await api("/saved", { signal });
        else if (route.view === "search")
          data = await api(`/search?q=${encodeURIComponent(route.q)}&offset=${cursor || 0}`, { signal });
        else data = await api(`/feed${cursor ? `?cont=${encodeURIComponent(cursor)}` : ""}`, { signal });
        setItems((prev) => {
          const seen = new Set(cursor ? prev.map((p) => p.title) : []);
          return [...(cursor ? prev : []), ...data.items.filter((i) => !seen.has(i.title))];
        });
        setNext(data.next ?? null);
        setEnglish(data.english || "");
        setError("");
      } catch (e) {
        if (e.name !== "AbortError") setError("Wikimedia non risponde, riprova tra poco.");
      } finally {
        busy.current = false;
        setLoading(false);
      }
    },
    [route],
  );

  useEffect(() => {
    const ctl = new AbortController();
    setLoading(true);
    setItems([]);
    load(null, ctl.signal);
    return () => ctl.abort();
  }, [load]);

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
      {route.view === "feed" && (
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
        <p className="fade-in mb-6 text-center text-sm text-mute">
          {english && english.toLowerCase() !== route.q.toLowerCase()
            ? `Risultati per “${route.q}” · cercato come “${english}”`
            : `Risultati per “${route.q}”`}
        </p>
      )}
      {route.view === "saved" && (
        <h1 className="fade-in mt-4 mb-8 text-center text-3xl font-semibold tracking-tight">Salvati</h1>
      )}

      {error && <p className="py-20 text-center text-mute">{error}</p>}
      {loading ? (
        <SkeletonGrid />
      ) : items.length ? (
        <Masonry items={items} onOpen={cardHandlers.onOpen} onSave={cardHandlers.onSave} />
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

function Detail({ route, preview, cardHandlers, onBack }) {
  const [origin, setOrigin] = useState(preview);
  const [items, setItems] = useState([]);
  const [weight, setWeight] = useState(0.75);
  const [phase, setPhase] = useState("loading"); // loading | refining | ready | error
  const [info, setInfo] = useState({ pending: 0, candidates: 0 });
  const [voting, setVoting] = useState(false);
  const [error, setError] = useState("");
  const [bigLoaded, setBigLoaded] = useState(false);
  const [trail, setTrail] = useState(readTrail);
  const run = useRef(0);
  const shown = useRef(false);

  const fetchSimilar = useCallback(
    async (budget, w) => {
      const qs = new URLSearchParams({ title: route.file, weight: String(w), budget: String(budget) });
      if (route.q) qs.set("q", route.q);
      return api(`/similar?${qs}`);
    },
    [route.file, route.q],
  );

  const show = useCallback((data) => {
    const apply = () => {
      setItems(data.items);
      setInfo({ pending: data.pending, candidates: data.candidates });
    };
    // First results rise in; later rounds glide cards to their new rank.
    if (shown.current) withTransition(apply);
    else apply();
    shown.current = data.items.length > 0;
  }, []);

  // Load, then keep refining while there are candidates that have not been looked at yet.
  const refresh = useCallback(
    async (firstBudget, w) => {
      const id = ++run.current;
      try {
        let data = await fetchSimilar(firstBudget, w);
        if (id !== run.current) return;
        setOrigin((o) => ({ ...o, ...data.origin }));
        show(data);
        for (let round = 0; round < REFINE_ROUNDS && data.pending > 0; round += 1) {
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
          <div className="relative overflow-hidden rounded-3xl bg-wash shadow-[0_30px_60px_-30px_rgba(0,0,0,0.35)]">
            {origin?.thumb ? (
              <>
                {/* The grid thumbnail is already cached: show it at once (it is what the card morphs into). */}
                <img
                  data-hero
                  src={origin.thumb}
                  alt={origin.name}
                  className="mx-auto max-h-[78vh] w-full object-contain"
                  style={{
                    viewTransitionName: "hero",
                    aspectRatio: origin.width && origin.height ? `${origin.width} / ${origin.height}` : undefined,
                  }}
                />
                <img
                  src={bigThumb(origin)}
                  alt=""
                  onLoad={() => setBigLoaded(true)}
                  className={`absolute inset-0 h-full w-full object-contain transition-opacity duration-700 ${bigLoaded ? "opacity-100" : "opacity-0"}`}
                />
              </>
            ) : (
              <div className="shimmer aspect-[4/3]" />
            )}
          </div>
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
                  Apri su Commons {Icon.out}
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

          <Trail trail={trail} current={route.file} onOpen={(t) => cardHandlers.onOpen(t)} />
        </aside>
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

      {phase === "ready" && info.pending > 0 && (
        <div className="mt-8 text-center">
          <button
            type="button"
            onClick={() => {
              setPhase("refining");
              refresh(REFINE_BUDGET, weight);
            }}
            className="rounded-full bg-wash px-6 py-3 text-sm font-semibold transition hover:-translate-y-0.5 hover:bg-ink hover:text-white"
          >
            Esplora altre {Math.min(info.pending, REFINE_BUDGET)} immagini
          </button>
        </div>
      )}
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
      withTransition(() => {
        setPreview(item);
        go({ view: "file", file: item.title, q });
      });
    },
    [go, route],
  );

  const cardHandlers = { onOpen, onSave, onSearch: (q) => go({ view: "search", q }) };

  return (
    <div className="min-h-screen">
      <Header
        route={route}
        savedCount={savedCount}
        onSearch={(q) => go({ view: "search", q })}
        onHome={() => go({ view: "feed", q: "" })}
        onSaved={() => go({ view: "saved", q: "" })}
      />
      {route.view === "file" ? (
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
