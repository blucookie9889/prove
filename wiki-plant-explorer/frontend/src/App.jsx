import { useCallback, useEffect, useMemo, useRef, useState } from "react";

const PAGE_SIZE = 60;
const SIMILAR_LIMIT = 40;
const WEIGHT_DEBOUNCE_MS = 250;

async function getJSON(url, signal) {
  const res = await fetch(url, { signal });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json();
}

function describeWeight(w) {
  if (w <= 0.15) return "Concept / taxonomy";
  if (w >= 0.85) return "Shape / style";
  if (w < 0.45) return "Mostly concept";
  if (w > 0.55) return "Mostly shape";
  return "Balanced";
}

/* ------------------------------------------------------------------ header */

function Header({ mode, loading, error, total, onToggleSidebar }) {
  const status = error
    ? { label: "Backend unreachable", dot: "bg-red-500" }
    : loading
      ? { label: "Searching…", dot: "bg-pollen-400 animate-pulse" }
      : mode === "similar"
        ? { label: "Similarity mode", dot: "bg-moss-400" }
        : { label: "Browsing feed", dot: "bg-sky-400" };

  return (
    <header className="sticky top-0 z-30 flex items-center gap-3 border-b border-canopy-700 bg-canopy-950/85 px-4 py-3 backdrop-blur md:px-6">
      <button
        type="button"
        onClick={onToggleSidebar}
        className="rounded-lg border border-canopy-700 p-2 text-stone-300 hover:bg-canopy-800 lg:hidden"
        aria-label="Toggle controls"
      >
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
          <path d="M4 6h16M4 12h16M4 18h16" />
        </svg>
      </button>
      <div className="flex min-w-0 items-center gap-2">
        <span className="text-2xl" aria-hidden>
          🌿
        </span>
        <div className="min-w-0">
          <h1 className="truncate text-lg font-semibold tracking-tight md:text-xl">Wiki Plant Explorer</h1>
          <p className="hidden truncate text-xs text-stone-400 sm:block">
            Dual-vector search · DINOv2 × OpenCLIP · Wikidata
          </p>
        </div>
      </div>
      <div className="ml-auto flex items-center gap-3">
        {total > 0 && (
          <span className="hidden rounded-full bg-canopy-800 px-3 py-1 text-xs text-stone-300 md:inline">
            {total} species indexed
          </span>
        )}
        <span
          className="flex items-center gap-2 rounded-full border border-canopy-700 bg-canopy-900 px-3 py-1.5 text-xs font-medium"
          role="status"
          aria-live="polite"
        >
          <span className={`h-2.5 w-2.5 rounded-full ${status.dot}`} />
          {status.label}
        </span>
      </div>
    </header>
  );
}

/* ------------------------------------------------------------------ sidebar */

function HybridSlider({ weight, onChange }) {
  const pct = Math.round(weight * 100);
  return (
    <section className="space-y-3">
      <div className="flex items-baseline justify-between">
        <h2 className="text-xs font-semibold uppercase tracking-wider text-stone-400">Hybrid weight</h2>
        <span className="font-mono text-sm text-stone-200">{weight.toFixed(2)}</span>
      </div>
      <input
        type="range"
        min="0"
        max="1"
        step="0.05"
        value={weight}
        onChange={(e) => onChange(parseFloat(e.target.value))}
        className="hybrid-range"
        style={{ "--pct": `${pct}%` }}
        aria-label="Hybrid weight between semantic concept and visual shape"
        aria-valuetext={describeWeight(weight)}
      />
      <div className="flex justify-between text-xs">
        <div>
          <p className="font-semibold text-pollen-400">Concept</p>
          <p className="text-stone-500">OpenCLIP · {100 - pct}%</p>
        </div>
        <div className="text-right">
          <p className="font-semibold text-moss-400">Shape</p>
          <p className="text-stone-500">DINOv2 · {pct}%</p>
        </div>
      </div>
      <p className="rounded-lg bg-canopy-800 px-3 py-2 text-center text-sm text-stone-300">
        {describeWeight(weight)}
      </p>
      <div className="flex gap-2">
        {[
          [0, "Concept"],
          [0.5, "Balanced"],
          [1, "Shape"],
        ].map(([v, label]) => (
          <button
            key={label}
            type="button"
            onClick={() => onChange(v)}
            className={`flex-1 rounded-lg border px-2 py-2 text-xs font-medium transition ${
              weight === v
                ? "border-moss-500 bg-moss-500/15 text-moss-400"
                : "border-canopy-700 text-stone-300 hover:bg-canopy-800"
            }`}
          >
            {label}
          </button>
        ))}
      </div>
    </section>
  );
}

function SelectedPlant({ plant, onClear }) {
  if (!plant) {
    return (
      <section className="rounded-xl border border-dashed border-canopy-700 p-4 text-sm text-stone-400">
        Tap any plant to find its nearest neighbours by shape and by concept.
      </section>
    );
  }
  return (
    <section className="overflow-hidden rounded-xl border border-moss-500/50 bg-canopy-900">
      <img src={plant.image_url} alt={plant.title} className="aspect-[4/3] w-full object-cover" />
      <div className="space-y-2 p-4">
        <h2 className="text-base font-semibold leading-tight">{plant.title}</h2>
        <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-sm">
          <dt className="text-stone-500">Taxon</dt>
          <dd className="italic text-stone-200">{plant.taxon}</dd>
          <dt className="text-stone-500">Family</dt>
          <dd className="text-stone-200">{plant.family}</dd>
          <dt className="text-stone-500">Wikidata</dt>
          <dd>
            <a
              href={plant.wiki_url}
              target="_blank"
              rel="noreferrer"
              className="text-moss-400 underline-offset-2 hover:underline"
            >
              {plant.wiki_id} ↗
            </a>
          </dd>
        </dl>
        <button
          type="button"
          onClick={onClear}
          className="mt-2 w-full rounded-lg border border-canopy-700 py-2 text-sm text-stone-300 hover:bg-canopy-800"
        >
          Clear selection · back to feed
        </button>
      </div>
    </section>
  );
}

function Sidebar({ open, query, onQuery, selected, onClear, weight, onWeight, onClose }) {
  return (
    <>
      {open && (
        <div className="fixed inset-0 z-30 bg-black/50 lg:hidden" onClick={onClose} aria-hidden />
      )}
      <aside
        className={`fixed inset-y-0 left-0 z-40 w-80 shrink-0 overflow-y-auto border-r border-canopy-700 bg-canopy-950 p-5 transition-transform lg:sticky lg:top-[65px] lg:z-0 lg:h-[calc(100vh-65px)] lg:translate-x-0 ${
          open ? "translate-x-0" : "-translate-x-full"
        }`}
      >
        <div className="space-y-6">
          <section className="space-y-2">
            <label htmlFor="search" className="text-xs font-semibold uppercase tracking-wider text-stone-400">
              Filter
            </label>
            <div className="relative">
              <input
                id="search"
                type="search"
                value={query}
                onChange={(e) => onQuery(e.target.value)}
                placeholder="Name, taxon or family…"
                className="w-full rounded-lg border border-canopy-700 bg-canopy-900 py-2.5 pl-9 pr-3 text-sm placeholder:text-stone-500 focus:border-moss-500 focus:outline-none"
              />
              <svg
                className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-stone-500"
                width="16"
                height="16"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="2"
              >
                <circle cx="11" cy="11" r="7" />
                <path d="m20 20-3.5-3.5" />
              </svg>
            </div>
          </section>

          <SelectedPlant plant={selected} onClear={onClear} />

          <HybridSlider weight={weight} onChange={onWeight} />
        </div>
      </aside>
    </>
  );
}

/* ------------------------------------------------------------------ grid */

function PlantCard({ plant, isOrigin, onSelect }) {
  const [loaded, setLoaded] = useState(false);
  const hasScore = typeof plant.score === "number";
  return (
    <button
      type="button"
      onClick={() => onSelect(plant)}
      className={`group relative block w-full overflow-hidden rounded-xl bg-canopy-900 text-left transition duration-200 hover:-translate-y-0.5 hover:shadow-xl hover:shadow-black/40 focus:outline-none focus-visible:ring-2 focus-visible:ring-moss-400 ${
        isOrigin ? "ring-4 ring-moss-400" : "ring-1 ring-canopy-700"
      }`}
    >
      <img
        src={plant.image_url}
        alt={plant.title}
        loading="lazy"
        onLoad={() => setLoaded(true)}
        className={`w-full object-cover transition-opacity duration-300 ${loaded ? "opacity-100" : "min-h-40 opacity-0"}`}
      />
      {!loaded && <div className="absolute inset-0 animate-pulse bg-canopy-800" />}

      {isOrigin && (
        <span className="absolute left-2 top-2 rounded-full bg-moss-400 px-2.5 py-1 text-xs font-bold text-canopy-950 shadow">
          ORIGIN
        </span>
      )}
      {hasScore && (
        <span
          className="absolute right-2 top-2 rounded-full bg-canopy-950/85 px-2.5 py-1 font-mono text-xs font-semibold text-moss-400 shadow backdrop-blur"
          title={`visual ${plant.visual_score} · semantic ${plant.semantic_score}`}
        >
          {Math.round(plant.score * 100)}%
        </span>
      )}

      <div className="absolute inset-x-0 bottom-0 bg-gradient-to-t from-black/90 via-black/60 to-transparent p-3 pt-8">
        <p className="truncate text-sm font-semibold">{plant.title}</p>
        <p className="truncate text-xs text-stone-300">
          <span className="italic">{plant.taxon}</span> · {plant.family}
        </p>
      </div>
    </button>
  );
}

/* ------------------------------------------------------------------ app */

export default function App() {
  const [feed, setFeed] = useState([]);
  const [nextOffset, setNextOffset] = useState(0);
  const [total, setTotal] = useState(0);
  const [feedLoading, setFeedLoading] = useState(false);

  const [selected, setSelected] = useState(null);
  const [results, setResults] = useState([]);
  const [similarLoading, setSimilarLoading] = useState(false);

  const [weight, setWeight] = useState(0.5);
  const [query, setQuery] = useState("");
  const [error, setError] = useState(null);
  const [sidebarOpen, setSidebarOpen] = useState(false);

  const mainRef = useRef(null);
  const sentinelRef = useRef(null);
  const feedInFlight = useRef(false);

  const loadFeed = useCallback(async () => {
    // Ref guard: state updates are async, so two quick calls (StrictMode, observer) could both pass.
    if (feedInFlight.current || nextOffset === null) return;
    feedInFlight.current = true;
    setFeedLoading(true);
    try {
      const data = await getJSON(`/api/feed?limit=${PAGE_SIZE}&offset=${nextOffset}`);
      setFeed((prev) => [...prev, ...data.items]);
      setNextOffset(data.next_offset ?? null);
      setTotal(data.total);
      setError(null);
    } catch (e) {
      setError(e.message);
    } finally {
      feedInFlight.current = false;
      setFeedLoading(false);
    }
  }, [nextOffset]);

  // Initial page.
  useEffect(() => {
    loadFeed();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Infinite scroll in feed mode.
  useEffect(() => {
    if (selected || !sentinelRef.current) return;
    const io = new IntersectionObserver((entries) => entries[0].isIntersecting && loadFeed(), {
      rootMargin: "600px",
    });
    io.observe(sentinelRef.current);
    return () => io.disconnect();
  }, [selected, loadFeed]);

  // Similarity search whenever the origin or (debounced) weight changes.
  useEffect(() => {
    if (!selected) return;
    const ctrl = new AbortController();
    setSimilarLoading(true);
    const t = setTimeout(async () => {
      try {
        const data = await getJSON(
          `/api/similar?point_id=${selected.id}&weight=${weight}&limit=${SIMILAR_LIMIT}`,
          ctrl.signal,
        );
        setResults(data.items);
        setError(null);
      } catch (e) {
        if (e.name !== "AbortError") setError(e.message);
      } finally {
        if (!ctrl.signal.aborted) setSimilarLoading(false);
      }
    }, WEIGHT_DEBOUNCE_MS);
    return () => {
      clearTimeout(t);
      ctrl.abort();
    };
  }, [selected, weight]);

  const handleSelect = useCallback((plant) => {
    // Strip score fields so the origin card doesn't show a stale badge.
    const { score, visual_score, semantic_score, ...base } = plant;
    setSelected(base);
    setSidebarOpen(false);
    mainRef.current?.scrollTo({ top: 0, behavior: "smooth" });
    window.scrollTo({ top: 0, behavior: "smooth" });
  }, []);

  const clearSelection = useCallback(() => {
    setSelected(null);
    setResults([]);
  }, []);

  const items = useMemo(() => {
    const base = selected ? [selected, ...results] : feed;
    const q = query.trim().toLowerCase();
    if (!q) return base;
    return base.filter(
      (p) =>
        (selected && p.id === selected.id) ||
        [p.title, p.taxon, p.family].some((v) => v?.toLowerCase().includes(q)),
    );
  }, [selected, results, feed, query]);

  const mode = selected ? "similar" : "feed";

  return (
    <div className="flex min-h-full flex-col">
      <Header
        mode={mode}
        loading={similarLoading || (feedLoading && feed.length === 0)}
        error={error}
        total={total}
        onToggleSidebar={() => setSidebarOpen((o) => !o)}
      />
      <div className="flex flex-1">
        <Sidebar
          open={sidebarOpen}
          query={query}
          onQuery={setQuery}
          selected={selected}
          onClear={clearSelection}
          weight={weight}
          onWeight={setWeight}
          onClose={() => setSidebarOpen(false)}
        />

        <main ref={mainRef} className="min-w-0 flex-1 p-4 md:p-6">
          <div className="mb-4 flex flex-wrap items-baseline justify-between gap-2">
            <h2 className="text-sm text-stone-400">
              {selected ? (
                <>
                  Nearest to <span className="font-semibold text-stone-100">{selected.title}</span> ·{" "}
                  {describeWeight(weight).toLowerCase()}
                </>
              ) : (
                <>
                  Showing {items.length}
                  {query ? " matching" : ""} of {total || "…"} species
                </>
              )}
            </h2>
            {query && (
              <button type="button" onClick={() => setQuery("")} className="text-xs text-moss-400 hover:underline">
                Clear filter
              </button>
            )}
          </div>

          {error && (
            <div className="mb-4 rounded-xl border border-red-500/40 bg-red-500/10 p-4 text-sm text-red-200">
              Could not reach the API ({error}). Is <code>uvicorn backend.main:app</code> running on port 8000, and has{" "}
              <code>backend/ingest.py</code> been run?
            </div>
          )}

          <div className={`masonry transition-opacity ${similarLoading ? "opacity-60" : "opacity-100"}`}>
            {items.map((plant) => (
              <PlantCard
                key={`${mode}-${plant.id}`}
                plant={plant}
                isOrigin={selected?.id === plant.id}
                onSelect={handleSelect}
              />
            ))}
          </div>

          {!selected && nextOffset !== null && (
            <div ref={sentinelRef} className="py-8 text-center text-sm text-stone-500">
              {feedLoading ? "Loading more plants…" : ""}
            </div>
          )}
          {items.length === 0 && !feedLoading && !similarLoading && !error && (
            <p className="py-16 text-center text-stone-500">No plants match “{query}”.</p>
          )}
        </main>
      </div>
    </div>
  );
}
