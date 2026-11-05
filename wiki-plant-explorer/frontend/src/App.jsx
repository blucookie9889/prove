import { Fragment, useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { flushSync } from "react-dom";

const SUGGESTIONS = ["foglie", "texture", "architettura gotica", "minerali", "conchiglie", "nuvole", "farfalle", "mosaici"];
// Search pages are shown at once, then checked with CLIP (up to this many new images per page).
const SEARCH_BUDGET = 72;
// Cards that get their own view-transition name, so they glide to their new place on re-rank.
const ANIMATED_CARDS = 40;

async function api(path, { signal, body, method } = {}) {
  const res = await fetch(`/api${path}`, {
    signal,
    method: method || (body ? "POST" : "GET"),
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
  if (p.get("journey")) return { view: "journey", id: p.get("journey"), q: "" };
  if (p.get("view") === "diary") return { view: "diary", q: "" };
  if (p.get("view") === "saved") return { view: "saved", q: "" };
  if (p.get("view") === "featured") return { view: "featured", q: "" };
  if (p.get("q")) return { view: "search", q: p.get("q") };
  return { view: "feed", q: "" };
}

function routeUrl(r) {
  const p = new URLSearchParams();
  if (r.view === "flow") p.set("flow", r.file);
  else if (r.view === "journey") p.set("journey", r.id);
  else if (r.view === "saved" || r.view === "featured" || r.view === "diary") p.set("view", r.view);
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

/** One line over the page: the search as a grey pill, the name in the middle, the places as pills (the one you are in, lime). */
function Header({ route, savedCount, journeyCount, onSearch, onHome, onSaved, onDiary }) {
  const [text, setText] = useState(route.view === "search" ? route.q : "");

  useEffect(() => {
    if (route.view === "search") setText(route.q);
    if (route.view === "feed") setText("");
  }, [route]);

  const diaryOn = route.view === "diary" || route.view === "journey";
  return (
    <header className="sticky top-0 z-30 bg-paper/85 backdrop-blur-[14px]">
      <div className="mx-auto flex max-w-[1500px] flex-wrap items-center gap-x-3 gap-y-2 px-3 py-3 sm:grid sm:grid-cols-[1fr_auto_1fr] md:px-4">
        <form
          className="order-3 w-full min-w-0 sm:order-none sm:max-w-[340px]"
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
            className="calm h-7 w-full rounded-[8px] bg-panel px-2.5 text-ink transition-colors outline-none placeholder:text-mute focus:bg-rule-2"
            enterKeyHint="search"
          />
        </form>
        <button type="button" onClick={onHome} className="order-1 shrink-0 text-[15px] tracking-[0.01em] text-ink uppercase sm:order-none">
          Wiki Explorer
        </button>
        <nav className="order-2 ml-auto flex gap-1.5 sm:order-none">
          <button type="button" onClick={onDiary} className={`pill ${diaryOn ? "is-on" : "is-quiet"}`}>
            Diario{journeyCount > 0 && <span className="tech">{journeyCount}</span>}
          </button>
          <button type="button" onClick={onSaved} className={`pill ${route.view === "saved" ? "is-on" : "is-quiet"}`}>
            Salvati{savedCount > 0 && <span className="tech">{savedCount}</span>}
          </button>
        </nav>
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
        className={`placeholder relative block w-full overflow-hidden rounded-[8px] ${item.vote === 1 ? "outline outline-1 outline-offset-2 outline-ink" : ""}`}
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
        <span className="min-w-0 flex-1 truncate">{prettyName(item.name)}</span>
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
        <div key={i} className="placeholder fade-in rounded-[8px]" style={{ aspectRatio: ratios[i % ratios.length] }} />
      ))}
    </div>
  );
}

function InfiniteSentinel({ onVisible, active, margin = "1200px" }) {
  const ref = useRef(null);
  useEffect(() => {
    if (!active || !ref.current) return undefined;
    const io = new IntersectionObserver((e) => e[0].isIntersecting && onVisible(), { rootMargin: margin });
    io.observe(ref.current);
    return () => io.disconnect();
  }, [active, onVisible, margin]);
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
            <p className="rise rise-2 flex flex-wrap gap-1.5">
              {[
                ["feed", "Per te"],
                ["featured", "In evidenza"],
              ].map(([view, label]) => (
                <button
                  key={view}
                  type="button"
                  onClick={() => cardHandlers.onNav(view)}
                  className={`pill ${route.view === view ? "is-on" : "is-quiet"}`}
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

/** POST that answers one JSON event per line; ``onEvent`` gets each as it arrives. */
async function apiStream(path, body, onEvent, signal) {
  const res = await fetch(`/api${path}`, {
    method: "POST",
    signal,
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok || !res.body) throw new Error("Qualcosa è andato storto, riprova.");
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    const lines = buf.split("\n");
    buf = lines.pop();
    lines.filter(Boolean).forEach((l) => onEvent(JSON.parse(l)));
  }
}

const reduceMotion = () => window.matchMedia("(prefers-reduced-motion: reduce)").matches;
const wide = () => window.matchMedia("(min-width: 1024px)").matches;

/**
 * The detail a door comes from, lifted off the picture: the picture dims and loses its colour, the
 * detail stays as it is, rises a little as under a loupe, and carries the door's name on a tag.
 * No line across the page: the number and the receding rows already say which door it is.
 * One lift for all the details, so moving between doors it glides from one to the next; leaving,
 * it settles back where it was.
 */
function Lift({ door, src }) {
  const last = useRef(null);
  if (door?.box) last.current = door;
  if (!last.current || !src) return null;
  const [x0, y0, x1, y1] = last.current.box;
  return (
    <span className={`lift ${door?.box ? "is-on" : ""}`} style={{ "--x0": x0, "--y0": y0, "--x1": x1, "--y1": y1 }} aria-hidden="true">
      <span className="lift-detail">
        <img src={src} alt="" />
      </span>
    </span>
  );
}

const two = (n) => String(n).padStart(2, "0");
const at = (v) => v.toFixed(2).replace(/^0/, "");

/**
 * The doors drawn on their picture as on a technical plate: each detail framed by a hairline with
 * corner ticks, and at its corner a tiny label (number, leap, where it sits on the picture). The
 * label is the door's handle: hovering it lifts the detail, clicking it goes through.
 */
function Marks({ doors, hover, setHover, onFollow, dotRefs }) {
  return (
    <div className={`marks ${hover >= 0 ? "has-on" : ""}`}>
      {doors.map((d, i) => {
        const [x0, y0, x1, y1] = d.box || [d.dot[0], d.dot[1], d.dot[0], d.dot[1]];
        const on = hover === i;
        const below = y0 < 0.08; // no room above the box: the label goes under its top edge
        const right = x0 > 0.62;
        return (
          <Fragment key={d.id}>
            {d.box && (
              <span
                className={`mark-box ${on ? "is-on" : ""}`}
                style={{ left: `${x0 * 100}%`, top: `${y0 * 100}%`, width: `${(x1 - x0) * 100}%`, height: `${(y1 - y0) * 100}%`, animationDelay: `${i * 0.12}s` }}
              />
            )}
            <button
              ref={(el) => (dotRefs.current[i] = el)}
              type="button"
              disabled={!d.ready}
              aria-label={d.title}
              onMouseEnter={() => setHover(i)}
              onMouseLeave={() => setHover(-1)}
              onFocus={() => setHover(i)}
              onBlur={() => setHover(-1)}
              onClick={() => onFollow(i)}
              className={`mark-label tech ${on ? "is-on" : ""} disabled:cursor-default`}
              style={{
                [right ? "right" : "left"]: `${(right ? 1 - x1 : x0) * 100}%`,
                top: `calc(${y0 * 100}% ${below ? "+ 4px" : "- 18px"})`,
                animationDelay: `${0.2 + i * 0.12}s`,
              }}
            >
              <span>{two(i + 1)}</span>
              <span className="min-w-0 truncate">{d.leap}</span>
              <span className="where hidden sm:inline">
                x{at((x0 + x1) / 2)} y{at((y0 + y1) / 2)}
              </span>
            </button>
          </Fragment>
        );
      })}
    </div>
  );
}

/**
 * The picture of a spread with its doors marked on it. As large as the screen allows, never cropped.
 * Under it, what the curator sees in it (written as it looks) and the credits.
 */
function FlowPicture({ item, seeing, doors, hover, setHover, onFollow, dotRefs, name, showName }) {
  const [bigLoaded, setBigLoaded] = useState(false);
  const ratio = item?.width && item?.height ? item.width / item.height : 4 / 3;
  const big = bigThumb(item);
  return (
    <figure className="flex min-w-0 flex-col items-center">
      <div
        className="relative w-[min(100%,calc(68svh*var(--r)))] lg:w-[min(100%,calc((100svh-210px)*var(--r)))]"
        style={{ "--r": ratio, aspectRatio: `${ratio}`, viewTransitionName: name }}
      >
        <div className="placeholder picture-base absolute inset-0 overflow-hidden rounded-[8px]" data-dim={hover >= 0 && Boolean(doors?.[hover]?.box)}>
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
        </div>
        <Lift door={hover >= 0 ? doors?.[hover] : null} src={bigLoaded ? big : item?.thumb} />
        <Marks doors={doors || []} hover={hover} setHover={setHover} onFollow={onFollow} dotRefs={dotRefs} />
      </div>
      <figcaption className="mt-3 flex w-[min(100%,calc(68svh*var(--r)))] flex-col gap-1 lg:w-[min(100%,calc((100svh-210px)*var(--r)))]" style={{ "--r": ratio }}>
        <p className="min-h-[1lh] text-ink-2">{seeing || ""}</p>
        <p className="truncate text-mute">{[showName ? prettyName(item?.name) : null, prettyAuthor(item?.author), item?.license].filter(Boolean).join(" · ")}</p>
      </figcaption>
    </figure>
  );
}

/**
 * A file name as a title: without camera codes, dates, shot numbers and IDs
 * ("DSC08246 Memorial Stones … 2025-09" -> "Memorial Stones …",
 * "Beech Forest (AU), Great Otway National Park -- 2019 -- 1294" -> "Beech Forest (AU), Great Otway National Park").
 */
function prettyName(name) {
  if (!name) return "";
  const clean = name
    .replace(/\s+--\s.*$/, "")
    .replace(/\b(?:DSCF?|IMG|DCIM|PXL|P|GOPR)[_-]?\d{3,}\b/gi, "")
    .replace(/\(\d{5,}\)/g, "")
    .replace(/^\d{2}[-_.]\d{2}(?:[-_.]\d{2})?[-_\s]+/, "")
    .replace(/[,\s-]+\b(?:19|20)\d\d(?:[-_.]\d\d){0,2}\b/g, "")
    .replace(/[\s_-]+\d{2,}\s*$/, "")
    .replace(/\s{2,}/g, " ")
    .replace(/^[\s,.:;_-]+|[\s,.:;_-]+$/g, "");
  return clean || name;
}

/** Commons' author as a name: "No machine-readable author provided. Loganberry assumed (based on
 * copyright claims)." -> "Loganberry". */
function prettyAuthor(author) {
  if (!author) return "";
  const assumed = author.match(/^No machine-readable author provided\.\s*(.+?)\s+assumed\b/i);
  return (assumed ? assumed[1] : author).replace(/\s*\((?:talk|discussione)\)\s*/gi, " ").trim();
}

/** ``extract`` without its opening when that is the ``hook`` already shown. */
function restOf(extract, hook) {
  if (!extract) return "";
  return hook && extract.startsWith(hook) ? extract.slice(hook.length).trim() : extract;
}

/** Wikipedia's explanation, a few lines, the rest a click away. */
function Extract({ text }) {
  const [open, setOpen] = useState(false);
  const ref = useRef(null);
  const [long, setLong] = useState(false);
  useLayoutEffect(() => {
    if (ref.current) setLong(ref.current.scrollHeight > ref.current.clientHeight + 2);
  }, [text]);
  return (
    <div className="rise rise-3 text-ink-2">
      <p ref={ref} className={open ? "" : "line-clamp-4"}>
        {text}
      </p>
      {(long || open) && (
        <button type="button" onClick={() => setOpen((o) => !o)} className="link mt-1 text-mute">
          {open ? "meno" : "continua"}
        </button>
      )}
    </div>
  );
}

/** One door of the list: what it leads to and from which detail; the picture it opens on the left. */
function DoorRow({ door, n, isNext, hover, onHover, onFollow, thumbRef }) {
  const dim = hover >= 0 && hover !== n;
  return (
    // The row opens its own height as it arrives, so the rows under it glide down instead of jumping
    <div className="open-in">
    <div className="rise min-h-0 border-t border-rule py-1">
    <button
      type="button"
      data-door={n}
      disabled={!door.ready}
      onClick={(e) => onFollow(n, e.currentTarget.querySelector("img"))}
      onMouseEnter={() => onHover(n)}
      onMouseLeave={() => onHover(-1)}
      onFocus={() => onHover(n)}
      onBlur={() => onHover(-1)}
      className={`group -mx-2 grid w-[calc(100%+16px)] grid-cols-[52px_1fr] gap-4 rounded-[8px] p-2 text-left transition-[opacity,background-color] duration-500 ease-[cubic-bezier(.16,1,.3,1)] disabled:cursor-default ${
        hover === n ? "bg-lime" : dim ? "opacity-[.45]" : ""
      }`}
    >
      <span ref={thumbRef} className="placeholder relative block aspect-[4/5] w-[52px] overflow-hidden rounded-[6px]">
        {door.ready ? (
          <img src={door.item.thumb} alt="" className="fade-in h-full w-full object-cover" />
        ) : (
          <span className="breathe absolute inset-0 bg-rule-2" />
        )}
      </span>
      <span className="calm min-w-0 transition-transform group-enabled:group-hover:translate-x-[3px]">
        <span className={`flex items-baseline gap-2 ${hover === n ? "text-ink/60" : "text-mute"}`}>
          <span className="tech">{two(n + 1)}</span>
          <span className="min-w-0 truncate">
            {door.leap}
            {door.anchor ? ` · ${door.anchor}` : ""}
          </span>
          {isNext && (
            <span className="ml-auto shrink-0" title="Scorrendo si apre questa">
              ↓
            </span>
          )}
        </span>
        <span className={`block transition-colors duration-[1200ms] first-letter:uppercase ${door.ready ? "text-ink" : "text-mute"}`}>{door.title}</span>
        <span className="block text-ink-2">{door.hook}</span>
      </span>
    </button>
    </div>
    </div>
  );
}

/** Rows standing in for doors not found yet. */
function DoorSkeleton({ n }) {
  return Array.from({ length: n }, (_, i) => (
    <div key={i} className="fade-in grid grid-cols-[52px_1fr] gap-4 border-t border-rule py-3" style={{ animationDelay: `${i * 0.2}s` }}>
      <span className="placeholder block aspect-[4/5] w-[52px] rounded-[6px]" />
      <span className="flex flex-col gap-2 pt-1.5">
        <span className="breathe block h-2 w-24 rounded-full bg-rule-2" style={{ animationDelay: `${i * 0.5}s` }} />
        <span className="breathe block h-2 w-40 rounded-full bg-rule-2" style={{ animationDelay: `${i * 0.5 + 0.2}s` }} />
      </span>
    </div>
  ));
}

/**
 * One stimulus as a spread: the picture on the left, on the right the concept that led here
 * (its hook, Wikipedia's explanation) and the doors out of it. A door and its detail answer each
 * other: hovering one brings the detail into focus and lets the other doors recede.
 */
function Stimulus({ entry, first, active, dive, onFollow, onSave, sectionRef }) {
  const [hover, setHover] = useState(-1);
  const host = useRef(null);
  const dotRefs = useRef([]);
  const thumbRefs = useRef([]);
  const { item, via, data } = entry;
  const doors = data?.doors || [];
  const done = Boolean(data?.done);
  const follow = (i, from) => onFollow(i, from || thumbRefs.current[i]?.querySelector("img"));
  useEffect(() => {
    sectionRef(host.current);
    return () => sectionRef(null);
  }, [sectionRef]);
  return (
    <section
      id={entry.key}
      ref={host}
      className="relative grid scroll-mt-16 grid-cols-1 content-start gap-x-16 gap-y-10 py-10 lg:min-h-[calc(100svh-64px)] lg:grid-cols-[minmax(0,1fr)_minmax(320px,400px)] lg:py-8"
    >
      <div className={`lg:sticky lg:top-[88px] lg:self-start ${first ? "fade-in" : "rise rise-1"}`}>
        <FlowPicture
          item={item}
          seeing={data?.seeing}
          doors={doors}
          hover={hover}
          setHover={setHover}
          onFollow={(i) => follow(i, dotRefs.current[i])}
          dotRefs={dotRefs}
          name={dive ? "dive" : first ? vtName(entry.title) : undefined}
          showName={Boolean(via)}
        />
      </div>

      <aside className="flex min-w-0 flex-col gap-3.5 self-start">
        {via ? (
          <>
            <div className="rise rise-1 rounded-[8px] bg-lime px-3 py-2">
              <h2 className="uppercase">{via.title}</h2>
              <p className="text-[10px] leading-[12px]">
                {via.leap}
                {via.anchor ? ` · ${via.anchor}` : ""}
              </p>
            </div>
            <p className="rise rise-2 rounded-[8px] bg-panel px-3 py-2.5 text-[15px] leading-[1.35]">{via.hook}</p>
            {/* The local curator's hook is the extract's first sentence: only the rest follows it */}
            {restOf(via.extract, via.hook) && <Extract text={restOf(via.extract, via.hook)} />}
            <p className="rise rise-3 flex flex-wrap gap-1.5">
              {via.url && (
                <a href={via.url} target="_blank" rel="noreferrer" className="pill is-quiet">
                  Wikipedia{via.lang === "en" ? ", in inglese" : ""}
                </a>
              )}
              {item && (
                <button type="button" onClick={() => onSave(item)} className={`pill ${item.saved ? "is-on" : "is-quiet"}`}>
                  {item.saved ? "Salvata" : "Salva"}
                </button>
              )}
            </p>
          </>
        ) : (
          <>
            <h1 className="rise rise-1 rounded-[8px] bg-panel px-3 py-2 uppercase">{prettyName(item?.name) || " "}</h1>
            {/* Its line is kept while the picture is being found, so nothing under it moves */}
            {!item && <p className="min-h-[1lh]" />}
            {item && (
              <p className="rise rise-2 flex flex-wrap gap-1.5">
                <button type="button" onClick={() => onSave(item)} className={`pill ${item.saved ? "is-on" : "is-quiet"}`}>
                  {item.saved ? "Salvata" : "Salva"}
                </button>
                {item.page_url && (
                  <a href={item.page_url} target="_blank" rel="noreferrer" className="pill is-quiet">
                    {item.source || "Wikimedia Commons"}
                  </a>
                )}
              </p>
            )}
          </>
        )}

        <div className="mt-6 flex flex-col">
          <p className="mb-1.5 flex items-baseline justify-between gap-4 text-mute">
            <span className="uppercase">Da qui</span>
            {/* Each state fades in as the one before it goes (keyed), never a word swapped in place */}
            {!done ? (
              <Breathing key={data?.seeing ? "doors" : "look"}>
                <span className="fade-in">{data?.seeing ? "Cerco le porte" : "Guardo cosa c'è di interessante"}</span>
              </Breathing>
            ) : (
              doors.length > 0 && active && <span key="keys" className="fade-in hidden lg:inline">1–{doors.length} per aprire</span>
            )}
          </p>
          <div className="flex flex-col">
            {doors.map((d, i) => (
              <DoorRow
                key={d.id}
                door={d}
                n={i}
                isNext={done && data.next === i}
                hover={hover}
                onHover={setHover}
                onFollow={follow}
                thumbRef={(el) => (thumbRefs.current[i] = el)}
              />
            ))}
            {!done && <DoorSkeleton n={Math.max(0, 3 - doors.length)} />}
            {done && !doors.length && <p className="border-t border-rule py-3 text-mute">{data.error || "Nessuna porta da questa immagine."}</p>}
            <div className="border-t border-rule" />
          </div>
        </div>
      </aside>

    </section>
  );
}

/**
 * The stream: a picture, the doors out of it, and on and on. Each picture's doors arrive as the
 * curator finds them. Scrolling takes the door farthest from where the person has been; clicking a
 * door (in the list or on the picture, or its number on the keyboard) dives into it.
 * There is no trail to keep: only what comes next.
 */
function Flow({ route, preview, cardHandlers, onKept }) {
  const [stream, setStream] = useState(() => [
    { key: `s0-${route.file}`, title: route.file, item: preview?.title === route.file ? preview : null, via: null, data: null },
  ]);
  const [dive, setDive] = useState(null);
  const [active, setActive] = useState(0);
  const started = useRef(Date.now() / 1000);
  // Ending the journey: its recap replaces the stream, which stays as it was to go back to
  const [ending, setEnding] = useState(null);
  const asked = useRef(new Map()); // entry key -> AbortController of its stream
  const sections = useRef(new Map());
  const count = useRef(1);

  // The attention each picture got: seconds it was the spread in view, while the page was visible
  // and the journey not yet ended.
  const dwell = useRef(new Map());
  const inView = useRef(null);
  inView.current = ending ? null : stream[active]?.key;
  useEffect(() => {
    const tick = setInterval(() => {
      const k = inView.current;
      if (k && document.visibilityState === "visible") dwell.current.set(k, (dwell.current.get(k) || 0) + 1);
    }, 1000);
    return () => clearInterval(tick);
  }, []);

  useEffect(() => {
    const onSaved = (e) =>
      setStream((prev) => prev.map((p) => (p.item?.title === e.detail.title ? { ...p, item: { ...p.item, saved: e.detail.saved } } : p)));
    window.addEventListener("saved-changed", onSaved);
    return () => window.removeEventListener("saved-changed", onSaved);
  }, []);

  useEffect(
    () => () => {
      asked.current.forEach((c) => c.abort());
      asked.current.clear();
    },
    [],
  );

  // Stream the doors of every picture that has none yet (quickly: the person is waiting); once they
  // are all there, prepare the one scrolling would take next, with time to think.
  useEffect(() => {
    const keys = new Set(stream.map((e) => e.key));
    asked.current.forEach((c, k) => {
      if (!keys.has(k)) {
        c.abort();
        asked.current.delete(k);
      }
    });
    stream.forEach((e) => {
      if (e.data?.done || asked.current.has(e.key)) return;
      const ctrl = new AbortController();
      asked.current.set(e.key, ctrl);
      const patch = (fn) => setStream((prev) => prev.map((p) => (p.key === e.key ? fn(p) : p)));
      const withData = (p, fn) => ({ ...p, data: fn(p.data || { seeing: "", doors: [], next: -1, done: false }) });
      apiStream(
        "/flow/stream",
        { title: e.title, via: e.via?.title || null },
        (ev) => {
          if (ev.type === "item") patch((p) => ({ ...p, item: { ...p.item, ...ev.item } }));
          else if (ev.type === "seeing") patch((p) => withData(p, (d) => ({ ...d, seeing: ev.text })));
          else if (ev.type === "door")
            patch((p) =>
              withData(p, (d) => {
                const at = d.doors.findIndex((x) => x.id === ev.door.id);
                const doors = at < 0 ? [...d.doors, ev.door] : d.doors.map((x, i) => (i === at ? ev.door : x));
                return { ...d, doors };
              }),
            );
          else if (ev.type === "drop") patch((p) => withData(p, (d) => ({ ...d, doors: d.doors.filter((x) => x.id !== ev.id) })));
          else if (ev.type === "error") patch((p) => withData(p, (d) => ({ ...d, done: true, error: ev.detail })));
          else if (ev.type === "done") {
            const r = ev.result;
            patch((p) => ({ ...p, item: { ...p.item, ...r.item }, data: { ...r, done: true } }));
            const nd = r.doors[r.next];
            if (nd) api("/flow", { body: { title: nd.item.title, via: nd.title, warm: true } }).catch(() => {});
          }
        },
        ctrl.signal,
      ).catch((err) => {
        if (err.name !== "AbortError") patch((p) => withData(p, (d) => ({ ...d, done: true, error: "Il server non risponde, riprova." })));
      });
    });
  }, [stream]);

  // The spread in view is the one the keyboard speaks to.
  useEffect(() => {
    const io = new IntersectionObserver(
      (entries) => {
        entries.forEach((en) => {
          if (en.isIntersecting) setActive(Number(en.target.dataset.index));
        });
      },
      { rootMargin: "-45% 0px -45% 0px" },
    );
    const frame = requestAnimationFrame(() =>
      sections.current.forEach((el, i) => {
        el.dataset.index = i;
        io.observe(el);
      }),
    );
    return () => {
      cancelAnimationFrame(frame);
      io.disconnect();
    };
  }, [stream.length]);

  const step = (door) => {
    count.current += 1;
    return { key: `s${count.current}-${door.item.title}`, title: door.item.title, item: door.item, via: door, data: null };
  };

  // Following a door: its little picture grows into the next spread (a View Transition named "dive").
  const current = useRef(stream);
  current.current = stream;
  const follow = useCallback((index, doorIndex, fromEl) => {
    const door = current.current[index]?.data?.doors?.[doorIndex];
    if (!door?.ready) return;
    const next = step(door);
    const show = (prev) => [...prev.slice(0, index + 1), next];
    window.history.replaceState(null, "", routeUrl({ view: "flow", file: next.title }));
    if (!document.startViewTransition || reduceMotion() || !fromEl) {
      setStream(show);
      requestAnimationFrame(() => document.getElementById(next.key)?.scrollIntoView({ behavior: reduceMotion() ? "auto" : "smooth" }));
      return;
    }
    fromEl.style.viewTransitionName = "dive";
    const t = document.startViewTransition(() => {
      fromEl.style.viewTransitionName = "";
      flushSync(() => {
        setDive(next.key);
        setStream(show);
      });
      document.getElementById(next.key)?.scrollIntoView({ block: "start" });
    });
    t.finished.finally(() => setDive(null));
  }, []);

  // Keys: 1-9 open that door of the spread in view, → the one scrolling would take.
  useEffect(() => {
    const onKey = (e) => {
      if (e.metaKey || e.ctrlKey || e.altKey || /input|textarea|select/i.test(e.target.tagName)) return;
      const entry = stream[active];
      if (!entry?.data) return;
      if (/^[1-9]$/.test(e.key)) {
        const i = Number(e.key) - 1;
        if (entry.data.doors[i]?.ready) {
          e.preventDefault();
          follow(active, i, sections.current.get(active)?.querySelector(`[data-door="${i}"] img`));
        }
      } else if (e.key === "ArrowRight" && entry.data.done && entry.data.next >= 0) {
        e.preventDefault();
        follow(active, entry.data.next, null);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [stream, active, follow]);

  const refSetters = useRef(new Map());
  const sectionRef = (i) => {
    if (!refSetters.current.has(i))
      refSetters.current.set(i, (el) => (el ? sections.current.set(i, el) : sections.current.delete(i)));
    return refSetters.current.get(i);
  };

  const last = stream[stream.length - 1];
  const more = useCallback(() => {
    setStream((prev) => {
      const tail = prev[prev.length - 1];
      const door = tail.data?.done && tail.data.doors?.[tail.data.next];
      return door ? [...prev, step(door)] : prev;
    });
  }, []);

  const journey = () => ({
    started: started.current,
    ended: Date.now() / 1000,
    steps: stream
      .filter((e) => e.item?.thumb)
      .map((e) => ({
        title: e.title,
        item: e.item,
        via: e.via && { title: e.via.title, leap: e.via.leap, anchor: e.via.anchor, hook: e.via.hook, url: e.via.url, box: e.via.box },
        seeing: e.data?.seeing || "",
        doors: e.data?.doors?.length || 0,
        seconds: dwell.current.get(e.key) || 0,
        note: "",
      })),
  });

  const end = () => {
    const j = journey();
    withTransition(() => setEnding({ ...j, title: "", summary: "", stats: null, writing: true, keptId: null }));
    window.scrollTo(0, 0);
    api("/journey/recap", { body: j })
      .then((r) => setEnding((e) => e && { ...e, ...r, writing: false }))
      .catch(() => setEnding((e) => e && { ...e, writing: false }));
  };

  const keep = async () => {
    try {
      const { id } = await api("/journeys", { body: ending });
      setEnding((e) => ({ ...e, keptId: id }));
      onKept();
    } catch {
      window.dispatchEvent(new CustomEvent("toast", { detail: "Non sono riuscito a salvarlo" }));
    }
  };

  const setNote = (i, note) => setEnding((e) => ({ ...e, steps: e.steps.map((st, k) => (k === i ? { ...st, note } : st)) }));
  const saveNote = (i) => ending.keptId && saveStepNote(ending.keptId, i, ending.steps[i].note);

  const back = () => {
    withTransition(() => setEnding(null));
    requestAnimationFrame(() => document.getElementById(stream[Math.max(0, active)]?.key)?.scrollIntoView({ block: "start" }));
  };

  if (ending)
    return (
      <Recap
        journey={ending}
        writing={ending.writing}
        onTitle={ending.keptId ? null : (title) => setEnding((e) => ({ ...e, title }))}
        onOpenStep={(step) => cardHandlers.onOpen(step.item)}
        onNote={setNote}
        onNoteSave={saveNote}
        actions={
          ending.keptId ? (
            <>
              <span className="pill is-on cursor-default">Nel diario</span>
              <button type="button" className="pill is-quiet" onClick={() => cardHandlers.onNav("diary")}>
                Apri il diario
              </button>
              <button type="button" className="pill is-quiet" onClick={() => cardHandlers.onNav("feed")}>
                Nuovo percorso
              </button>
            </>
          ) : (
            <>
              <button type="button" className="pill is-go" disabled={ending.writing} onClick={keep}>
                Salva nel diario
              </button>
              <button type="button" className="pill is-quiet" onClick={back}>
                Torna al percorso
              </button>
              <button type="button" className="pill is-quiet" onClick={() => cardHandlers.onNav("feed")}>
                Nuovo percorso
              </button>
            </>
          )
        }
      />
    );

  return (
    <main className="mx-auto max-w-[1500px] px-6 pb-12">
      {stream.map((e, i) => (
        <Stimulus
          key={e.key}
          entry={e}
          first={i === 0}
          active={i === active}
          dive={dive === e.key}
          onFollow={(d, from) => follow(i, d, from)}
          onSave={cardHandlers.onSave}
          sectionRef={sectionRef(i)}
        />
      ))}
      <InfiniteSentinel onVisible={more} active={Boolean(last.data?.done && last.data.next >= 0)} margin="160px" />
      <Trail
        steps={stream.filter((e) => e.item?.thumb)}
        onStep={(key) => document.getElementById(key)?.scrollIntoView({ behavior: reduceMotion() ? "auto" : "smooth", block: "start" })}
        onEnd={end}
      />
    </main>
  );
}

/* ------------------------------------------------------------------ journeys */

const TRAIL_SHOWN = 6; // the last pictures in the trail; the recap and the diary have them all
const DAY = new Intl.DateTimeFormat("it-IT", { day: "numeric", month: "long", year: "numeric" });
const MONTH = new Intl.DateTimeFormat("it-IT", { month: "long", year: "numeric" });
const WEEKDAY = new Intl.DateTimeFormat("it-IT", { weekday: "short" });

/** A step's picture keeps its name from the trail to the recap, so ending the journey flies it there. */
const stepName = (i) => `step-${i}`;

/**
 * The journey so far, floating in glass at the foot of the flow: its last pictures (each scrolls back
 * to its spread), how many steps, and the way to end it. It appears with the first door followed.
 */
function Trail({ steps, onStep, onEnd }) {
  if (steps.length < 2) return null;
  const from = Math.max(0, steps.length - TRAIL_SHOWN);
  return (
    <div className="trail glass fixed bottom-5 left-1/2 z-40 flex -translate-x-1/2 items-center gap-3 rounded-[10px] py-1.5 pr-1.5 pl-1.5">
      <span className="flex items-center gap-1">
        {from > 0 && <span className="pr-1 text-mute tabular-nums">+{from}</span>}
        {steps.slice(from).map((e, j) => (
          <button
            key={e.key}
            type="button"
            onClick={() => onStep(e.key)}
            title={e.via?.title || prettyName(e.item?.name)}
            className="trail-step block h-7 w-7 overflow-hidden rounded-[5px]"
            style={{ viewTransitionName: stepName(from + j) }}
          >
            <img src={e.item.thumb} alt="" className="h-full w-full object-cover" />
          </button>
        ))}
      </span>
      <span className="tech whitespace-nowrap text-mute">{steps.length} TAPPE</span>
      <button type="button" onClick={onEnd} className="pill is-go">
        Concludi
      </button>
    </div>
  );
}

function statsOf(stats) {
  if (!stats) return [];
  const far = (stats.leaps?.altrove || 0) + (stats.leaps?.scala || 0);
  return [
    [stats.pictures, stats.pictures === 1 ? "immagine" : "immagini"],
    [stats.concepts, stats.concepts === 1 ? "concetto" : "concetti"],
    far ? [far, far === 1 ? "salto lontano" : "salti lontani"] : null,
    [stats.minutes, stats.minutes === 1 ? "minuto" : "minuti"],
    stats.notes ? [stats.notes, stats.notes === 1 ? "nota" : "note"] : null,
  ].filter(Boolean);
}

const lasted = (s) => (s < 60 ? `${Math.max(1, Math.round(s))} s` : `${Math.round(s / 60)} min`);

function saveStepNote(id, i, note) {
  return api(`/journeys/${id}/steps/${i}/note`, { method: "PUT", body: { note } }).catch(() =>
    window.dispatchEvent(new CustomEvent("toast", { detail: "Non sono riuscito a salvare la nota" })),
  );
}

/**
 * The detail a door started from, cut out of the picture before at the box's own proportions: what
 * caught the eye, kept beside where it led.
 */
function Detail({ item, box, h = 22 }) {
  if (!item?.thumb || !box) return null;
  const [x0, y0, x1, y1] = box;
  const ratio = item.width && item.height ? ((x1 - x0) * item.width) / ((y1 - y0) * item.height) : 1;
  const w = Math.round(Math.min(Math.max(h * ratio, h * 0.6), h * 2.4));
  return (
    <span className="relative inline-block shrink-0 overflow-hidden rounded-[4px] shadow-[inset_0_0_0_0.5px_rgb(29_28_26/0.12)]" style={{ width: w, height: h }}>
      <img
        src={item.thumb}
        alt=""
        className="absolute max-w-none"
        style={{
          width: `${100 / (x1 - x0)}%`,
          height: `${100 / (y1 - y0)}%`,
          left: `${(-x0 / (x1 - x0)) * 100}%`,
          top: `${(-y0 / (y1 - y0)) * 100}%`,
        }}
      />
    </span>
  );
}

/**
 * A journey told back: the day, a title (the curator's, which can be changed before keeping it),
 * a few sentences on what ties the steps together, its numbers, then the trail itself as a
 * collection, step by step along a thread: each picture with the door that led to it, the detail
 * that caught the eye, how long it held it, and a line of the person's own.
 */
function Recap({ journey, writing, onTitle, onOpenStep, onNote, onNoteSave, actions }) {
  const { steps, summary, stats, ended } = journey;
  const lingered = stats?.lingered ?? null;
  return (
    <main className="mx-auto max-w-[720px] px-6 pt-12 pb-12">
      <p className="fade-in tech text-mute uppercase">{DAY.format(new Date((ended || Date.now() / 1000) * 1000))}</p>
      <div className="mt-2 min-h-[1.2em] text-[28px] leading-[1.2] tracking-[-0.015em]">
        {writing ? (
          <span className="breathe mt-3 block h-3 w-64 rounded-full bg-rule-2" />
        ) : onTitle ? (
          <input
            value={journey.title}
            onChange={(e) => onTitle(e.target.value)}
            aria-label="Titolo del percorso"
            className="fade-in w-full bg-transparent text-ink outline-none first-letter:uppercase"
          />
        ) : (
          <h1 className="fade-in first-letter:uppercase">{journey.title}</h1>
        )}
      </div>
      <div className="mt-3 min-h-[4.8em] max-w-[580px]">
        {writing ? (
          <Breathing>Il curatore ripercorre le tappe</Breathing>
        ) : (
          summary && <p className="fade-in text-ink-2">{summary}</p>
        )}
      </div>
      <p className="mt-5 flex flex-wrap gap-x-5 gap-y-1 text-mute">
        {statsOf(stats).map(([n, word]) => (
          <span key={word} className="fade-in">
            <span className="text-ink tabular-nums">{n}</span> {word}
          </span>
        ))}
      </p>
      <p className="mt-6 flex flex-wrap gap-1.5">{actions}</p>

      <ol className="mt-14">
        {steps.map((st, i) => (
          <li key={`${i}-${st.title}`} className="rise group relative grid grid-cols-[72px_1fr] gap-5 pb-9" style={{ animationDelay: `${0.15 + i * 0.07}s` }}>
            {/* The thread from one picture to the next */}
            {i < steps.length - 1 && <span className="absolute top-[98px] bottom-2 left-[36px] w-px bg-rule-2" />}
            <button
              type="button"
              onClick={() => onOpenStep(st)}
              title="Riparti da qui"
              className="placeholder block aspect-[4/5] w-[72px] overflow-hidden rounded-[8px]"
              style={{ viewTransitionName: stepName(i) }}
            >
              <img src={st.item.thumb} alt="" className="h-full w-full object-cover" />
            </button>
            <div className="min-w-0 pt-0.5">
              <p className="flex items-center gap-2 text-mute">
                <span className="tech">{two(i + 1)}</span>
                {st.via?.box && <Detail item={steps[i - 1]?.item} box={st.via.box} />}
                <span className="min-w-0 truncate">
                  {st.via ? `${st.via.leap}${st.via.anchor ? ` · ${st.via.anchor}` : ""}` : "partenza"}
                </span>
                {st.seconds >= 1 && (
                  <span className={`ml-auto shrink-0 tabular-nums ${i === lingered ? "text-ink" : ""}`}>
                    {i === lingered ? `il più a lungo · ${lasted(st.seconds)}` : lasted(st.seconds)}
                  </span>
                )}
              </p>
              <p className="text-ink first-letter:uppercase">{st.via ? st.via.title : prettyName(st.item.name)}</p>
              {st.via?.hook && <p className="text-ink-2">{st.via.hook}</p>}
              {onNote ? (
                <input
                  value={st.note || ""}
                  onChange={(e) => onNote(i, e.target.value)}
                  onBlur={() => onNoteSave?.(i)}
                  onKeyDown={(e) => e.key === "Enter" && e.currentTarget.blur()}
                  maxLength={280}
                  placeholder="Una riga tua"
                  aria-label={`Nota sulla tappa ${i + 1}`}
                  className="calm mt-1.5 w-full border-b border-transparent bg-transparent pb-0.5 text-ink outline-none transition-colors placeholder:text-mute/70 hover:border-rule focus:border-rule-2"
                />
              ) : (
                st.note && <p className="mt-1.5 text-ink">“{st.note}”</p>
              )}
              <button
                type="button"
                onClick={() => onOpenStep(st)}
                className="calm mt-1 text-mute transition-[opacity,color] hover:text-ink focus:opacity-100 lg:opacity-0 lg:group-hover:opacity-100"
              >
                Riparti da qui →
              </button>
            </div>
          </li>
        ))}
      </ol>
    </main>
  );
}

const DIARY_LOOK = "diary-look"; // the person's choice of list or grid, kept in this browser

function readLook() {
  try {
    return localStorage.getItem(DIARY_LOOK) === "grid" ? "grid" : "list";
  } catch {
    return "list";
  }
}

/** The diary: the journeys kept, newest first, by month; as a list (day, title, summary) or a grid of covers. */
function Diary({ onOpen }) {
  const [items, setItems] = useState(null);
  const [look, setLook] = useState(readLook);
  useEffect(() => {
    api("/journeys")
      .then((r) => setItems(r.items))
      .catch(() => setItems([]));
  }, []);
  const choose = (l) => {
    withTransition(() => setLook(l));
    try {
      localStorage.setItem(DIARY_LOOK, l);
    } catch {
      /* private window: the choice lasts this visit */
    }
  };
  const months = [];
  (items || []).forEach((j) => {
    const m = MONTH.format(new Date(j.ended * 1000));
    if (months[months.length - 1]?.m !== m) months.push({ m, list: [] });
    months[months.length - 1].list.push(j);
  });
  return (
    <main className="mx-auto max-w-[1100px] px-4 pt-8 pb-12 md:px-6">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="rise rise-1 max-w-[420px] rounded-[8px] bg-panel px-3 py-2.5 text-[15px] leading-[1.35]">
          I percorsi che hai concluso e tenuto: ogni tappa con il dettaglio che ti ha fermato, quanto ci sei rimasto e una riga tua.
        </div>
        <span className="rise rise-2 inline-flex rounded-[8px] bg-panel p-0.5">
          {[
            ["list", "Lista"],
            ["grid", "Griglia"],
          ].map(([l, label]) => (
            <button
              key={l}
              type="button"
              onClick={() => choose(l)}
              className={`calm h-6 rounded-[6px] px-5 transition-colors ${look === l ? "bg-paper text-ink" : "text-mute hover:text-ink"}`}
            >
              {label}
            </button>
          ))}
        </span>
      </div>
      {items && !items.length && <p className="fade-in mt-12 text-mute">Nessun percorso ancora. Quando ne concludi uno, lo trovi qui.</p>}
      {months.map(({ m, list }) => (
        <section key={m} className="mt-8">
          <h2 className="fade-in flex items-baseline justify-between rounded-[8px] bg-panel px-3 py-2 uppercase">
            {m}
            <span className="tech text-mute">
              {list.length} {list.length === 1 ? "PERCORSO" : "PERCORSI"}
            </span>
          </h2>
          {look === "grid" ? (
            <div className="mt-2 grid grid-cols-2 gap-2 md:grid-cols-3 lg:grid-cols-4">
              {list.map((j, i) => (
                <button key={j.id} type="button" onClick={() => onOpen(j.id)} className="rise group text-left" style={{ animationDelay: `${i * 0.06}s` }}>
                  <span className="placeholder grid aspect-square grid-cols-2 grid-rows-2 gap-px overflow-hidden rounded-[8px]">
                    {Array.from({ length: 4 }, (_, k) =>
                      j.thumbs[k] ? (
                        <img key={k} src={j.thumbs[k]} alt="" className="calm h-full w-full object-cover transition-transform group-hover:scale-[1.03]" />
                      ) : (
                        <span key={k} className="bg-panel" />
                      ),
                    )}
                  </span>
                  <span className="mt-1.5 block truncate uppercase">{j.title}</span>
                  <span className="tech block text-mute">
                    {new Date(j.ended * 1000).getDate()} · {statsOf(j.stats).map(([n, w]) => `${n} ${w}`).join(" · ")}
                  </span>
                </button>
              ))}
            </div>
          ) : (
            list.map((j, i) => {
              const d = new Date(j.ended * 1000);
              return (
                <button
                  key={j.id}
                  type="button"
                  onClick={() => onOpen(j.id)}
                  className="rise group grid w-full grid-cols-[44px_1fr] items-start gap-5 border-b border-rule px-3 py-4 text-left transition-colors duration-500 hover:bg-panel md:grid-cols-[44px_1fr_auto] md:rounded-[8px] md:border-transparent"
                  style={{ animationDelay: `${i * 0.06}s` }}
                >
                  <span className="flex flex-col items-center leading-none">
                    <span className="text-[24px] text-ink tabular-nums">{d.getDate()}</span>
                    <span className="tech mt-1 text-mute uppercase">{WEEKDAY.format(d)}</span>
                  </span>
                  <span className="min-w-0">
                    <span className="block text-ink uppercase">{j.title}</span>
                    {j.summary && <span className="line-clamp-2 text-ink-2">{j.summary}</span>}
                    {j.note && <span className="mt-1 block truncate text-ink">“{j.note}”</span>}
                    <span className="tech mt-1.5 block text-mute">{statsOf(j.stats).map(([n, w]) => `${n} ${w}`).join(" · ")}</span>
                  </span>
                  <span className="col-start-2 flex gap-1 md:col-start-3">
                    {j.thumbs.map((t, k) => (
                      <img key={k} src={t} alt="" className="h-12 w-10 rounded-[5px] object-cover" />
                    ))}
                  </span>
                </button>
              );
            })
          )}
        </section>
      ))}
    </main>
  );
}

/** A journey from the diary, as a collection to reopen: start again from any of its steps. */
function JourneyPage({ id, cardHandlers, onForgot }) {
  const [j, setJ] = useState(null);
  const [error, setError] = useState("");
  const [sure, setSure] = useState(false);
  useEffect(() => {
    api(`/journeys/${id}`)
      .then(setJ)
      .catch((e) => setError(e.message));
  }, [id]);
  if (error) return <p className="mx-auto max-w-[720px] px-6 pt-12 text-mute">{error}</p>;
  if (!j) return <main className="mx-auto min-h-[60vh] max-w-[720px] px-6 pt-12" />;
  const lastStep = j.steps[j.steps.length - 1];
  return (
    <Recap
      journey={j}
      writing={false}
      onTitle={null}
      onOpenStep={(st) => cardHandlers.onOpen(st.item)}
      onNote={(i, note) => setJ((x) => ({ ...x, steps: x.steps.map((st, k) => (k === i ? { ...st, note } : st)) }))}
      onNoteSave={(i) => saveStepNote(id, i, j.steps[i].note)}
      actions={
        <>
          <button type="button" className="pill is-go" onClick={() => cardHandlers.onOpen(lastStep.item)}>
            Riprendi dall'ultima tappa
          </button>
          <button type="button" className="pill is-quiet" onClick={() => cardHandlers.onNav("diary")}>
            Diario
          </button>
          <button
            type="button"
            className="pill is-quiet"
            onClick={async () => {
              if (!sure) return setSure(true);
              await api(`/journeys/${id}`, { method: "DELETE" }).catch(() => {});
              onForgot();
              cardHandlers.onNav("diary");
            }}
            onBlur={() => setSure(false)}
          >
            {sure ? "Sicuro? Toglilo dal diario" : "Togli dal diario"}
          </button>
        </>
      }
    />
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
    <footer className="flex justify-center px-6 pt-10 pb-[100px] text-[11px]">
      <Breathing>{time} a Treviso, Italia</Breathing>
    </footer>
  );
}

/* ------------------------------------------------------------------ app */

export default function App() {
  const [route, setRoute] = useState(readRoute);
  const [preview, setPreview] = useState(null);
  const [savedCount, setSavedCount] = useState(0);
  const [journeyCount, setJourneyCount] = useState(0);
  const [toast, setToast] = useState("");
  const lastToast = useRef("");
  if (toast) lastToast.current = toast; // keep the text while the toast fades out

  useEffect(() => {
    api("/health")
      .then((h) => {
        setSavedCount(h.saved);
        setJourneyCount(h.journeys || 0);
      })
      .catch(() => setToast("Il server non risponde: è acceso?"));
    const onPop = () => setRoute(readRoute());
    const onToast = (e) => setToast(e.detail);
    window.addEventListener("popstate", onPop);
    window.addEventListener("toast", onToast);
    return () => {
      window.removeEventListener("popstate", onPop);
      window.removeEventListener("toast", onToast);
    };
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
        journeyCount={journeyCount}
        onSearch={(q) => go({ view: "search", q })}
        onHome={() => go({ view: "feed", q: "" })}
        onSaved={() => go({ view: "saved", q: "" })}
        onDiary={() => go({ view: "diary", q: "" })}
      />
      {route.view === "flow" ? (
        <Flow
          key={route.file}
          route={route}
          preview={preview?.title === route.file ? preview : null}
          cardHandlers={cardHandlers}
          onKept={() => {
            setJourneyCount((c) => c + 1);
            setToast("Nel diario");
          }}
        />
      ) : route.view === "diary" ? (
        <Diary onOpen={(id) => go({ view: "journey", id, q: "" })} />
      ) : route.view === "journey" ? (
        <JourneyPage key={route.id} id={route.id} cardHandlers={cardHandlers} onForgot={() => setJourneyCount((c) => Math.max(0, c - 1))} />
      ) : (
        <Browse key={`${route.view}:${route.q}`} route={route} cardHandlers={cardHandlers} />
      )}

      <Footer />

      <div
        className={`pointer-events-none glass fixed ${route.view === "flow" ? "bottom-[76px]" : "bottom-6"} left-1/2 z-50 -translate-x-1/2 rounded-[8px] px-3 py-1.5 text-ink transition-[opacity,filter] duration-[1400ms] ease-[cubic-bezier(.16,1,.3,1)] ${
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
