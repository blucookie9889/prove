"""The curator: a vision-language model looks at an image and opens doors to knowledge from what is in it.

Each door starts from something visible (with its box in the image) and leads to a concept worth
knowing, of one kind: what is inside it (the mechanism), why it happens, the same principle far
away, the human story, another scale. The curator also gets the concepts already met on this
journey, so it never leads back. Everything it says is then grounded by the caller: a door is kept
only if its Wikipedia article exists, and the text shown is Wikipedia's.

Three engines, same prompt and schema (``CURATOR`` in .env picks one; default ``local``):
  - ``LocalCurator``: an open vision-language model on this Mac through Ollama (``OLLAMA_MODEL`` /
    ``OLLAMA_URL``). Free, and nothing leaves the computer. The default and the only one used unless
    another is chosen explicitly.
  - ``GeminiCurator`` (``CURATOR=gemini``): Google Gemini (default ``gemini-3.8-flash``) with an AI Studio
    key (``GEMINI_API_KEY``). Free only on a project without billing: on a paid or prepaid project every
    picture costs. Sent with ``store: false``. It stops asking for a while after "payment required" or
    "quota exhausted".
  - ``Curator`` (``CURATOR=claude``): Claude through the Anthropic API (paid, needs ANTHROPIC_API_KEY).
The image is sent in memory as base64; nothing is stored.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import re
import time
from typing import AsyncIterator, Optional

import anthropic
import httpx
from PIL import Image

log = logging.getLogger("curator")

MODEL = "claude-opus-5-5"
DOORS = 5
SIDE = 1024  # long side of the image sent to the model

LEAPS = ["dentro", "perché", "altrove", "storia", "scala"]

GUIDE = """You are the curator of a visual encyclopedia for one curious, demanding person. They look at a \
picture; you see what in it is genuinely worth knowing, and open doors from it.

Each door starts from one specific thing that is visible in the picture (give its box) and leads to one \
concept that has its own Wikipedia article. A door is one of five kinds:
- dentro: the mechanism inside the thing (how it works, what it is made of);
- perché: why it is the way it is (the cause, the physics, the evolution);
- altrove: the same principle somewhere far away, in another field (the leap that surprises);
- storia: the human story attached to it (who discovered, used, feared or made it);
- scala: the same thing at a very different scale (atoms, cells, cities, galaxies).

What to point at:
- Only things you can clearly see in this picture, big enough to be framed by a box, and that you can \
  name with confidence. The box must tightly contain that thing and nothing else of note: the person \
  will see exactly that crop.
- Use the picture's file name as a strong clue to what it shows (species, place, object, event).
- Never point at: text or inscriptions you cannot read, watermarks, borders, blur, noise or other \
  defects of the photo, empty background, or the photograph itself. Never use generic photography \
  concepts (photography, light, shadow, perspective, composition, colour) unless they are truly the \
  subject.
- When the picture is plain, open fewer doors (three is fine) rather than inventing details.

What makes a good door:
- The link between the detail and the concept is real and specific: the hook states it.
- It is true. Prefer phenomena, mechanisms, laws, materials, techniques, histories over plain labels: \
  "superhydrophobicity" beats "water"; "phyllotaxis" beats "leaf"; "Rayleigh scattering" beats "sky".
- It is specific. Never a broad textbook chapter (photosynthesis, steel, civil engineering, urban \
  planning, road safety, polymers, erosion): name the precise phenomenon, law, technique or story that \
  this very detail shows ("Kautsky effect", "Martensite", "Euler buckling", "Cerdà's Eixample grid").
- Its hook is one surprising, precise, verifiable fact that makes the person want to go through. No \
  marketing, no exclamation marks, no questions, no "did you know".
- The five doors are different from each other: different details of the picture, different kinds \
  (at least one altrove), different fields of knowledge.
- Never repeat or lightly vary a concept the person has already met (listed below). Go somewhere new.
- If the picture shows something famous (a species, a building, an artwork), you may use it, but the \
  best doors usually come from details and from what they mean.

"""

# How the cloud models answer: JSON with the schema below.
SYSTEM = GUIDE + """Be brief: anchor 2-5 words (anchor_en: the same detail in plain English, as a photo caption would name \
it: "engraved name plate", "moss between bricks"), hook at most 18 words, image_query at most 6 words, seeing one short sentence.

The person reads Italian. Write anchor, title and hook in Italian. Give the article title exactly as it \
is on Italian Wikipedia when that article exists (else an empty string) and exactly as on English \
Wikipedia. image_query is an English search for a single striking photograph of the concept itself \
(concrete and visual: "water droplets on lotus leaf macro", "Fibonacci spiral sunflower head"), not of \
this picture."""

# How the local model answers: plain lines, a third of the JSON's length (writing is where its time
# goes), with examples of the doors wanted. No hook: the one shown is Wikipedia's.
LINES = GUIDE + """Answer in plain lines and nothing else, no JSON, no lists, no comments.
Line 1: what the picture shows, one short sentence in Italian.
Then one line per door, seven fields separated by " | ", in this order:
kind | box as x0 y0 x1 y1, integers 0-1000 | the detail, 2-5 Italian words | the same detail, 2-5 plain \
English words | the link: why this detail leads to that concept, 5-12 Italian words | the concept's \
article title exactly as on English Wikipedia | an English search, at most 6 words, for one striking photograph of the concept itself

Examples of the doors wanted (from other pictures):
perché | 120 80 380 300 | gocce sulla foglia | water drops on a leaf | la cera a microscopiche punte fa rotolare l'acqua | Lotus effect | water droplets lotus leaf macro
altrove | 410 280 590 590 | catarifrangente nell'asfalto | road cat's eye | gli occhi dei gatti riflettono la luce allo stesso modo | Tapetum lucidum | cat eyes glowing in the dark
altrove | 600 460 760 660 | nervature che si dividono | branching leaf veins | rami e vasi si dividono con la stessa regola del raggio | Murray's law | river delta branching aerial
dentro | 300 200 700 650 | spirale della conchiglia | spiral shell | cresce senza cambiare forma, ogni giro più largo | Logarithmic spiral | nautilus shell cross section
Never broad topics like Fotosintesi, Acciaio, Ingegneria civile, Urbanistica, Ombra, Foglia: the precise phenomenon."""

SCHEMA = {
    "type": "object",
    "properties": {
        "seeing": {"type": "string", "description": "What the picture shows, one precise sentence in Italian."},
        "doors": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "anchor": {"type": "string", "description": "The visible detail in 2-5 Italian words (words, not coordinates)."},
                    "anchor_en": {"type": "string", "description": "The same visible detail in 2-5 plain English words."},
                    "x0": {"type": "number"},
                    "y0": {"type": "number"},
                    "x1": {"type": "number"},
                    "y1": {"type": "number"},
                    "leap": {"type": "string", "enum": LEAPS},
                    "title": {"type": "string"},
                    "wikipedia_it": {"type": "string"},
                    "wikipedia_en": {"type": "string"},
                    "hook": {"type": "string"},
                    "image_query": {"type": "string"},
                },
                "required": ["anchor", "anchor_en", "x0", "y0", "x1", "y1", "leap", "title", "wikipedia_it", "wikipedia_en",
                             "hook", "image_query"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["seeing", "doors"],
    "additionalProperties": False,
}


def context_text(name: str, via: Optional[str], seen: list[str], doors: int = DOORS) -> str:
    lines = [f"Picture: {name}."]
    if via:
        lines.append(f"The person arrived at this picture through the concept: {via}.")
    if seen:
        lines.append("Concepts already met (do not lead back to them or near variants): " + "; ".join(seen[-60:]) + ".")
    lines.append(f"Open up to {doors} doors, each from a different detail. Each box (x0, y0, x1, y1) tightly frames that detail, "
                 "as integers from 0 to 1000 relative to the picture's width and height.")
    return "\n".join(lines)


def normalise_door(d: dict) -> dict:
    """Box as fractions: some models answer in thousandths (Qwen-VL, Gemini) or pixels of the 1024 px image.
    Missing fields (a model without structured output may skip some) become empty."""
    for k in SCHEMA["properties"]["doors"]["items"]["properties"]:
        if not isinstance(d.get(k), (str, int, float)):
            d[k] = ""
    if d["leap"] not in LEAPS:
        d["leap"] = "altrove"

    def num(v) -> float:
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0

    box = [num(d[k]) for k in ("x0", "y0", "x1", "y1")]
    top = max(box)
    scale = 1.0 if top <= 1.5 else 1000.0 if top <= 1000 else float(SIDE)
    x0, y0, x1, y1 = (v / scale for v in box)
    d.update(x0=min(x0, x1), y0=min(y0, y1), x1=max(x0, x1), y1=max(y0, y1))
    return d


def normalise(data: dict) -> dict:
    for d in data.get("doors", []):
        normalise_door(d)
    return data


class AnswerStream:
    """Reads the curator's JSON while it is being written: the "seeing" sentence as it grows, and each
    door as soon as its object closes, so the caller can show and ground them before the end."""

    SEEING = re.compile(r'"seeing"\s*:\s*"')
    DOORS = re.compile(r'"doors"\s*:\s*\[')

    def __init__(self):
        self.text = ""
        self.seeing = ""
        self.pos = -1  # scan position inside the doors array
        self.depth = 0
        self.start = -1
        self.in_string = False
        self.escape = False

    def feed(self, chunk: str) -> list[tuple[str, object]]:
        self.text += chunk
        events: list[tuple[str, object]] = []
        m = self.SEEING.search(self.text)
        if m and self.pos < 0:
            raw = re.match(r'(?:[^"\\]|\\.)*', self.text[m.end():]).group(0)
            try:
                seeing = json.loads('"' + raw.rstrip("\\") + '"')
            except ValueError:
                seeing = self.seeing
            if seeing != self.seeing:
                self.seeing = seeing
                events.append(("seeing", seeing))
        if self.pos < 0:
            m = self.DOORS.search(self.text)
            if not m:
                return events
            self.pos = m.end()
        text = self.text
        while self.pos < len(text):
            ch = text[self.pos]
            if self.in_string:
                if self.escape:
                    self.escape = False
                elif ch == "\\":
                    self.escape = True
                elif ch == '"':
                    self.in_string = False
            elif ch == '"':
                self.in_string = True
            elif ch == "{":
                if self.depth == 0:
                    self.start = self.pos
                self.depth += 1
            elif ch == "}":
                self.depth -= 1
                if self.depth == 0 and self.start >= 0:
                    try:
                        events.append(("door", normalise_door(json.loads(text[self.start:self.pos + 1]))))
                    except ValueError:
                        pass
                    self.start = -1
            self.pos += 1
        return events


class Engine:
    async def doors(self, image: Image.Image, name: str, via: Optional[str], seen: list[str],
                    deep: bool = True) -> Optional[dict]:
        raise NotImplementedError

    async def stream(self, image: Image.Image, name: str, via: Optional[str], seen: list[str],
                     deep: bool = True) -> AsyncIterator[tuple[str, object]]:
        """("seeing", text) and ("door", door) as they become known. Engines that cannot stream
        give everything at once at the end."""
        data = await self.doors(image, name, via, seen, deep=deep)
        if not data:
            return
        yield "seeing", data.get("seeing") or ""
        for d in data.get("doors", []):
            yield "door", d


def jpeg_b64(image: Image.Image, side: int = SIDE) -> str:
    im = image.convert("RGB")
    im.thumbnail((side, side))
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=85)
    return base64.standard_b64encode(buf.getvalue()).decode()


class Curator(Engine):
    def __init__(self, api_key: str, model: str = MODEL):
        self.client = anthropic.AsyncAnthropic(api_key=api_key)
        self.model = model

    async def doors(self, image: Image.Image, name: str, via: Optional[str], seen: list[str],
                    deep: bool = True) -> Optional[dict]:
        """{"seeing", "doors": [...]} for ``image``, or None when the model declines or fails. ``deep``: the
        picture is prepared ahead, so the model may think longer (only Gemini tells the two apart)."""
        try:
            response = await self.client.beta.messages.create(
                model=self.model,
                max_tokens=16000,
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",  # a declined request is re-run on Anthropic's recommended fallback model
                output_config={"effort": "medium", "format": {"type": "json_schema", "schema": SCHEMA}},
                system=SYSTEM,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": jpeg_b64(image)}},
                        {"type": "text", "text": context_text(name, via, seen)},
                    ],
                }],
            )
        except anthropic.AuthenticationError:
            log.error("curator: the Anthropic API key was refused")
            return None
        except anthropic.RateLimitError:
            log.warning("curator: rate limited")
            return None
        except anthropic.APIStatusError as exc:
            log.warning("curator: API error %s: %s", exc.status_code, exc.message)
            return None
        except anthropic.APIConnectionError as exc:
            log.warning("curator: connection error: %s", exc)
            return None
        if response.stop_reason == "refusal":
            log.warning("curator declined %s", name)
            return None
        text = next((b.text for b in response.content if b.type == "text"), "")
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            log.warning("curator: unreadable answer for %s (stop: %s)", name, response.stop_reason)
            return None
        log.info("curator %s: %d in / %d out tokens", name, response.usage.input_tokens, response.usage.output_tokens)
        return normalise(data)


class LocalCurator(Engine):
    """The same curator on an open vision-language model served by Ollama on this Mac: free."""

    KEEP = "2h"  # keep the model in memory between pictures: loading it takes 5-15 s
    SIDE = 896  # smaller than for the cloud models: fewer image tokens to read, same details

    def __init__(self, url: str, model: str):
        self.url = url.rstrip("/")
        self.model = model
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(300, connect=5))

    async def available(self) -> bool:
        try:
            tags = (await self.http.get(f"{self.url}/api/tags", timeout=3)).json()
        except (httpx.HTTPError, ValueError):
            return False
        return any(m.get("name") == self.model or m.get("model") == self.model for m in tags.get("models", []))

    async def warm_up(self) -> None:
        """Loads the model now, so the first picture does not wait for it."""
        try:
            await self.http.post(f"{self.url}/api/generate", json={"model": self.model, "keep_alive": self.KEEP})
        except httpx.HTTPError as exc:
            log.warning("local curator: could not load %s: %s", self.model, exc)

    CANDIDATES = 8  # doors asked for when the picture is prepared ahead, of which ``pick`` keeps DOORS

    async def stream(self, image: Image.Image, name: str, via: Optional[str], seen: list[str],
                     deep: bool = True) -> AsyncIterator[tuple[str, object]]:
        """While someone waits: DOORS doors, streamed. Prepared ahead (``deep``): CANDIDATES doors, then
        a second, text-only look keeps the most surprising and precise (about 5 s more, unseen)."""
        if not deep:
            async for event in self.lines(image, name, via, seen, DOORS):
                yield event
            return
        seeing, doors = "", []
        async for kind, value in self.lines(image, name, via, seen, self.CANDIDATES):
            if kind == "seeing":
                seeing = value
            else:
                doors.append(value)
        if seeing:
            yield "seeing", seeing
        for d in await self.pick(seeing, doors):
            yield "door", d

    async def pick(self, seeing: str, doors: list[dict]) -> list[dict]:
        """The DOORS best of ``doors``, best first, chosen by the same model from their words alone."""
        if len(doors) <= DOORS:
            return doors
        listed = "\n".join(f"{i + 1}. {d['anchor']} -> {d['title']} ({d.get('link', '')})" for i, d in enumerate(doors))
        body = {
            "model": self.model, "stream": False, "think": False, "keep_alive": self.KEEP,
            "options": {"temperature": 0.2, "num_predict": 40},
            "messages": [{"role": "user", "content":
                f"A picture shows: {seeing}\nCandidate doors from its details to Wikipedia concepts:\n{listed}\n\n"
                f"Pick the {DOORS} a curious, demanding person would most want to open: true, specific and "
                "surprising, from different details and fields. Reject broad textbook topics, places, wrong or "
                "far-fetched links, and near duplicates. Answer only with their numbers, best first, comma-separated."}],
        }
        try:
            resp = await self.http.post(f"{self.url}/api/chat", json=body)
            resp.raise_for_status()
            nums = [int(n) - 1 for n in re.findall(r"\d+", resp.json()["message"]["content"])]
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            log.warning("local curator: pick failed: %s", exc)
            return doors[:DOORS]
        chosen = [doors[i] for i in dict.fromkeys(nums) if 0 <= i < len(doors)][:DOORS]
        log.info("local curator: kept %s of %d", [d["title"] for d in chosen], len(doors))
        return chosen or doors[:DOORS]

    async def lines(self, image: Image.Image, name: str, via: Optional[str], seen: list[str],
                    doors: int) -> AsyncIterator[tuple[str, object]]:
        body = {
            "model": self.model,
            "stream": True,
            "think": False,  # tried on: a few sharper doors, minutes instead of seconds
            "keep_alive": self.KEEP,
            "options": {"temperature": 0.6, "num_ctx": 8192},
            "messages": [
                {"role": "system", "content": LINES},
                {"role": "user", "content": context_text(name, via, seen, doors), "images": [jpeg_b64(image, self.SIDE)]},
            ],
        }
        answer = LineStream()
        last: dict = {}
        try:
            async with self.http.stream("POST", f"{self.url}/api/chat", json=body) as resp:
                if resp.status_code != 200:
                    text = (await resp.aread()).decode(errors="replace")
                    log.warning("local curator: HTTP %s for %s: %s", resp.status_code, name, text[:300])
                    return
                async for line in resp.aiter_lines():
                    try:
                        last = json.loads(line)
                    except ValueError:
                        continue
                    for event in answer.feed((last.get("message") or {}).get("content", "")):
                        yield event
                    if last.get("done"):
                        for event in answer.feed("\n"):
                            yield event
        except httpx.HTTPError as exc:
            log.warning("local curator failed for %s: %s", name, exc)
            return
        log.info("local curator %s: %s in / %s out tokens, %.1fs (load %.1fs)", name, last.get("prompt_eval_count"),
                 last.get("eval_count"), (last.get("total_duration") or 0) / 1e9, (last.get("load_duration") or 0) / 1e9)


class LineStream:
    """Reads the local curator's plain lines while they are written: the first line is what it sees
    (sent as it grows), each later line with six or seven " | " fields is a door."""

    def __init__(self):
        self.buf = ""
        self.seeing: Optional[str] = None  # None until its line is complete

    def feed(self, chunk: str) -> list[tuple[str, object]]:
        self.buf += chunk
        events: list[tuple[str, object]] = []
        *lines, self.buf = self.buf.split("\n")
        for line in lines:
            line = line.strip().strip("`*-•").strip()
            if not line:
                continue
            if self.seeing is None and "|" not in line:
                self.seeing = line
                events.append(("seeing", line))
            elif (door := self.door(line)) is not None:
                events.append(("door", door))
        if self.seeing is None and self.buf.strip() and "|" not in self.buf:
            events.append(("seeing", self.buf.strip().strip("`*").strip()))
        return events

    @staticmethod
    def door(line: str) -> Optional[dict]:
        f = [x.strip() for x in line.split("|")]
        if len(f) < 6:  # the photo search is sometimes left out: the title stands in for it
            return None
        f += [""] * (7 - len(f))
        nums = re.findall(r"-?\d+(?:\.\d+)?", f[1])
        if len(nums) != 4:
            return None
        # f[4] is the link the model writes before choosing the concept: it makes the choice better
        # (and the second look, ``pick``), and is not shown (a local model's facts are not trusted)
        title = f[5].strip(" .\"'")
        # An English title: one meaning per title, and the Italian article is found from it
        d = {"leap": f[0].lower(), "anchor": f[2], "anchor_en": f[3], "link": f[4], "wikipedia_it": "", "wikipedia_en": title,
             "title": title, "hook": "", "image_query": f[6] or title,
             **dict(zip(("x0", "y0", "x1", "y1"), map(float, nums)))}
        return normalise_door(d)


class Fallback(Engine):
    """One engine, and another when the first answers nothing (no credit, quota, network)."""

    def __init__(self, first: Engine, then: Optional[Engine]):
        self.first, self.then = first, then
        self.model = f"{first.model}, else {then.model}" if then else first.model

    async def stream(self, image: Image.Image, name: str, via: Optional[str], seen: list[str],
                     deep: bool = True) -> AsyncIterator[tuple[str, object]]:
        answered = False
        async for event in self.first.stream(image, name, via, seen, deep):
            answered = True
            yield event
        if not answered and self.then:
            log.info("curator: %s answered nothing for %s, asking %s", self.first.model, name, self.then.model)
            async for event in self.then.stream(image, name, via, seen, deep):
                yield event


class GeminiCurator(Engine):
    """The same curator on Google Gemini through the Interactions API: free with an AI Studio key."""

    URL = "https://generativelanguage.googleapis.com/v1beta/interactions"

    def __init__(self, api_key: str, model: str, thinking: str = "medium"):
        self.model = model
        self.thinking = thinking  # medium: 30-70 s and the richest doors; low: ~9 s, more obvious doors
        self.http = httpx.AsyncClient(timeout=120, headers={"x-goog-api-key": api_key})
        self.off_until = 0.0  # after "payment required" / "quota exhausted": stop asking for a while

    async def stream(self, image: Image.Image, name: str, via: Optional[str], seen: list[str],
                     deep: bool = True) -> AsyncIterator[tuple[str, object]]:
        body = {
            "model": self.model,
            "store": False,
            "stream": True,
            "system_instruction": SYSTEM,
            "input": [
                {"type": "image", "data": jpeg_b64(image), "mime_type": "image/jpeg"},
                {"type": "text", "text": context_text(name, via, seen)},
            ],
            "response_format": {"type": "text", "mime_type": "application/json", "schema": SCHEMA},
            "generation_config": {"thinking_level": self.thinking if deep else "low"},  # low while someone waits
        }
        if time.time() < self.off_until:
            return
        answer = AnswerStream()
        usage: dict = {}
        try:
            async with self.http.stream("POST", self.URL, json=body) as resp:
                if resp.status_code != 200:
                    text = (await resp.aread()).decode(errors="replace")
                    level = log.error if resp.status_code in (400, 401, 402, 403) else log.warning
                    level("gemini curator: HTTP %s for %s: %s", resp.status_code, name, text[:300])
                    if resp.status_code in (402, 429):
                        self.off_until = time.time() + 15 * 60
                    return
                # Server-sent events: "data: {...}" lines; the text arrives in step.delta events.
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    try:
                        ev = json.loads(line[5:])
                    except ValueError:
                        continue  # "[DONE]"
                    kind = ev.get("event_type")
                    if kind == "step.delta" and (ev.get("delta") or {}).get("type") == "text":
                        for event in answer.feed(ev["delta"].get("text", "")):
                            yield event
                    elif kind == "interaction.completed":
                        usage = (ev.get("interaction") or {}).get("usage") or {}
                    elif kind == "error":
                        log.warning("gemini curator: error for %s: %s", name, str(ev)[:300])
        except httpx.HTTPError as exc:
            log.warning("gemini curator: connection error: %s", exc)
            return
        log.info("gemini curator %s: %s in / %s out / %s thinking tokens%s", name, usage.get("total_input_tokens"),
                 usage.get("total_output_tokens"), usage.get("total_thought_tokens"), "" if deep else " (quick)")
