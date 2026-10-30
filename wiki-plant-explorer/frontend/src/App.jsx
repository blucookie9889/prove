import { useCallback, useEffect, useRef, useState } from "react";
import { flushSync } from "react-dom";

const SUGGESTIONS = ["foglie", "texture", "architettura gotica", "minerali", "conchiglie", "nuvole", "farfalle", "mosaici"];
// Search pages are shown at once, then checked with CLIP (up to this many new images per page).
const SEARCH_BUDGET = 72;
// Cards that get their own view-transition name, so they glide to their new place on re-rank.
const ANIMATED_CARDS = 40;

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
  const start = p.get("flow") || p.get("file"); // ?file= is the old image page: it opens the flow now
  if (start) return { view: "flow", file: start, q: "" };
  if (p.get("view") === "saved") return { view: "saved", q: "" };
  if (p.get("view") === "featured") return { view: "featured", q: "" };
  if (p.get("q")) return { view: "search", q: p.get("q") };
  return { view: "feed", q: "" };
}

function routeUrl(r) {
  const p = new URLSearchParams();
  if (r.view === "flow") p.set("flow", r.file);
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

/* ------------------------------------------------------------------ flow */

/**
 * The veil over a picture that leaves one box clear: four paper-coloured rectangles around it.
 * ``box`` is x0, y0, x1, y1 as fractions of the picture.
 */
function Veil({ box }) {
  const [x0, y0, x1, y1] = box || [0, 0, 1, 1];
  const parts = [
    { left: 0, top: 0, width: "100%", height: `${y0 * 100}%` },
    { left: 0, top: `${y1 * 100}%`, width: "100%", height: `${(1 - y1) * 100}%` },
    { left: 0, top: `${y0 * 100}%`, width: `${x0 * 100}%`, height: `${(y1 - y0) * 100}%` },
    { left: `${x1 * 100}%`, top: `${y0 * 100}%`, width: `${(1 - x1) * 100}%`, height: `${(y1 - y0) * 100}%` },
  ];
  return (
    <span className={`pointer-events-none absolute inset-0 transition-opacity duration-[1200ms] ease-[cubic-bezier(.16,1,.3,1)] ${box ? "opacity-100" : "opacity-0"}`}>
      {parts.map((st, i) => (
        <span key={i} className="absolute bg-paper/70" style={st} />
      ))}
    </span>
  );
}

/** One picture of the stream, its doors marked on it: hover one to see where it starts. */
function FlowPicture({ item, doors, hover, setHover, onFollow }) {
  const [bigLoaded, setBigLoaded] = useState(false);
  const ratio = item?.width && item?.height ? item.width / item.height : 4 / 3;
  const big = bigThumb(item);
  return (
    <div className="relative mx-auto" style={{ aspectRatio: `${ratio}`, width: `min(100%, calc(80vh * ${ratio}))` }}>
      <div className="placeholder absolute inset-0 overflow-hidden rounded-[2px]">
        {item?.thumb && (
          <>
            <img src={item.thumb} alt={item.name} className="h-full w-full object-cover" />
            <img
              src={big}
              alt=""
              onLoad={() => setBigLoaded(true)}
              className={`absolute inset-0 h-full w-full object-cover transition-opacity duration-[1800ms] ease-[cubic-bezier(.16,1,.3,1)] ${bigLoaded ? "opacity-100" : "opacity-0"}`}
            />
          </>
        )}
        <Veil box={hover >= 0 && doors?.[hover] ? doors[hover].box : null} />
        {(doors || []).map((d, i) => (
          <button
            key={i}
            type="button"
            aria-label={d.title}
            onMouseEnter={() => setHover(i)}
            onMouseLeave={() => setHover(-1)}
            onFocus={() => setHover(i)}
            onBlur={() => setHover(-1)}
            onClick={() => onFollow(i)}
            className="fade-in absolute grid h-8 w-8 -translate-x-1/2 -translate-y-1/2 place-items-center"
            style={{ left: `${d.dot[0] * 100}%`, top: `${d.dot[1] * 100}%`, animationDelay: `${0.3 + i * 0.25}s` }}
          >
            <span
              className={`block h-1.5 w-1.5 rounded-full bg-paper outline outline-1 outline-ink/25 transition-transform duration-[1200ms] ease-[cubic-bezier(.16,1,.3,1)] ${
                hover === i ? "scale-[1.8]" : "breathe"
              }`}
              style={{ animationDelay: `${i * 0.7}s` }}
            />
          </button>
        ))}
      </div>
    </div>
  );
}

/**
 * One stimulus: the door that led here (what it is, the hook, Wikipedia's explanation), the
 * picture, and the doors out of it as list rows.
 */
function Stimulus({ entry, first, onFollow, cardHandlers }) {
  const [hover, setHover] = useState(-1);
  const { item, via, data } = entry;
  const doors = data?.doors;
  return (
    <section id={entry.key} className="mb-40 scroll-mt-24">
      <div className="mx-auto mb-12 flex max-w-[560px] flex-col gap-3.5">
        {via ? (
          <>
            <p className="rise rise-1 text-mute">
              {via.leap}
              {via.anchor ? ` · ${via.anchor}` : ""}
            </p>
            <h2 className="rise rise-1 first-letter:uppercase">{via.title}</h2>
            <p className="rise rise-2">{via.hook}</p>
          </>
        ) : (
          <>
            <h1 className="rise rise-1">{item?.name || " "}</h1>
            {item?.author !== undefined && <p className="rise rise-1 text-mute">{[item.author, item.license].filter(Boolean).join(" · ")}</p>}
            {data?.seeing && <p className="rise rise-2 text-ink-2">{data.seeing}</p>}
          </>
        )}
      </div>

      <div className={first ? "rise rise-2" : "rise rise-3"}>
        <FlowPicture item={item} doors={doors} hover={hover} setHover={setHover} onFollow={(i) => onFollow(i)} />
      </div>

      <div className="mx-auto mt-12 flex max-w-[560px] flex-col gap-3.5">
        {via?.extract && (
          <>
            <p className="text-ink-2">{via.extract}</p>
            <p className="flex flex-wrap gap-x-5 text-mute">
              {via.url && (
                <a href={via.url} target="_blank" rel="noreferrer">
                  Wikipedia{via.lang === "en" ? ", in inglese" : ""}
                </a>
              )}
              <button type="button" onClick={() => cardHandlers.onSave(item)} className="link">
                {item.saved ? "Salvata" : "Salva"}
              </button>
              <span className="truncate">{item.name}</span>
            </p>
          </>
        )}
        {!via && item && (
          <p className="flex flex-wrap gap-x-5 text-mute">
            <button type="button" onClick={() => cardHandlers.onSave(item)} className="link">
              {item.saved ? "Salvata" : "Salva"}
            </button>
            {item.page_url && (
              <a href={item.page_url} target="_blank" rel="noreferrer">
                {item.source || "Wikimedia Commons"}
              </a>
            )}
          </p>
        )}

        <div className="mt-10 flex flex-col">
          <p className="mb-3.5 text-mute">Da qui</p>
          {!data ? (
            <p className="border-t border-rule py-[11px]">
              <Breathing>Guardo cosa c'è di interessante</Breathing>
            </p>
          ) : !doors.length ? (
            <p className="border-t border-rule py-[11px] text-mute">Nessuna porta da questa immagine.</p>
          ) : (
            <div className="recede flex flex-col">
              {doors.map((d, i) => (
                <button
                  key={i}
                  type="button"
                  onClick={() => onFollow(i)}
                  onMouseEnter={() => setHover(i)}
                  onMouseLeave={() => setHover(-1)}
                  className="rise group grid grid-cols-[108px_1fr_48px] items-start gap-x-0 border-t border-rule py-[11px] text-left"
                  style={{ "--i": i * 3 }}
                >
                  <span className="text-mute">{d.leap}</span>
                  <span className="calm min-w-0 pr-4 transition-transform group-hover:translate-x-[3px]">
                    <span className="block first-letter:uppercase">{d.title}</span>
                    <span className="block text-ink-2">{d.hook}</span>
                  </span>
                  <img src={d.item.thumb} alt="" loading="lazy" className="aspect-[4/5] w-12 rounded-[2px] bg-wash object-cover" />
                </button>
              ))}
              <div className="border-t border-rule" />
            </div>
          )}
          {data && !data.curator && (
            <p className="mt-3.5 text-mute">Porte dai concetti di Wikimedia. Con una chiave Anthropic in .env le sceglie il curatore.</p>
          )}
        </div>
      </div>
    </section>
  );
}

/**
 * The stream: a picture, the doors out of it, and on and on. Scrolling takes the door farthest
 * from where the person has been; clicking a door (in the list or on the picture) takes that one.
 * There is no trail to keep: only what comes next.
 */
function Flow({ route, preview, cardHandlers }) {
  const [stream, setStream] = useState(() => [
    { key: `s0-${route.file}`, title: route.file, item: preview?.title === route.file ? preview : null, via: null, data: null },
  ]);
  const asked = useRef(new Set());
  const count = useRef(1);

  useEffect(() => {
    const onSaved = (e) =>
      setStream((prev) => prev.map((p) => (p.item?.title === e.detail.title ? { ...p, item: { ...p.item, saved: e.detail.saved } } : p)));
    window.addEventListener("saved-changed", onSaved);
    return () => window.removeEventListener("saved-changed", onSaved);
  }, []);

  // Ask for the doors of every picture that has none yet (quickly: the person is waiting); prepare the
  // one scrolling would take next ahead, with time to think.
  useEffect(() => {
    stream.forEach((e) => {
      if (e.data || asked.current.has(e.key)) return;
      asked.current.add(e.key);
      api("/flow", { body: { title: e.title, via: e.via?.title || null } })
        .then((d) => {
          setStream((prev) => prev.map((p) => (p.key === e.key ? { ...p, item: { ...p.item, ...d.item }, data: d } : p)));
          const nd = d.doors[d.next];
          if (nd) api("/flow", { body: { title: nd.item.title, via: nd.title, warm: true } }).catch(() => {});
        })
        .catch(() => setStream((prev) => prev.map((p) => (p.key === e.key ? { ...p, data: { doors: [], next: -1 } } : p))));
    });
  }, [stream]);

  const step = (door) => {
    count.current += 1;
    return { key: `s${count.current}-${door.item.title}`, title: door.item.title, item: door.item, via: door, data: null };
  };

  const follow = (index, doorIndex) => {
    const door = stream[index].data?.doors?.[doorIndex];
    if (!door) return;
    const next = step(door);
    setStream((prev) => [...prev.slice(0, index + 1), next]);
    window.history.replaceState(null, "", routeUrl({ view: "flow", file: next.title }));
    requestAnimationFrame(() => document.getElementById(next.key)?.scrollIntoView({ behavior: "smooth" }));
  };

  const last = stream[stream.length - 1];
  const more = useCallback(() => {
    setStream((prev) => {
      const tail = prev[prev.length - 1];
      const door = tail.data?.doors?.[tail.data.next];
      return door ? [...prev, step(door)] : prev;
    });
  }, []);

  return (
    <main className="mx-auto max-w-[1500px] px-6 pt-16 pb-12 md:pt-24">
      {stream.map((e, i) => (
        <Stimulus key={e.key} entry={e} first={i === 0} onFollow={(d) => follow(i, d)} cardHandlers={cardHandlers} />
      ))}
      <InfiniteSentinel onVisible={more} active={Boolean(last.data && last.data.next >= 0)} />
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

  // Opening a picture starts a flow from it.
  const onOpen = useCallback(
    (item) => {
      api("/view", { body: { title: item.title, item } }).catch(() => {});
      withTransition(() => {
        setPreview(item);
        go({ view: "flow", file: item.title, q: "" });
      });
    },
    [go],
  );

  const cardHandlers = {
    onOpen,
    onSave,
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
      {route.view === "flow" ? (
        <Flow key={route.file} route={route} preview={preview?.title === route.file ? preview : null} cardHandlers={cardHandlers} />
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
