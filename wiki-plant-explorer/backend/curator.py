"""The curator: a vision-language model looks at an image and opens doors to knowledge from what is in it.

Each door starts from something visible (with its box in the image) and leads to a concept worth
knowing, of one kind: what is inside it (the mechanism), why it happens, the same principle far
away, the human story, another scale. The curator also gets the concepts already met on this
journey, so it never leads back. Everything it says is then grounded by the caller: a door is kept
only if its Wikipedia article exists, and the text shown is Wikipedia's.

Three engines, same prompt and schema:
  - ``GeminiCurator``: Google Gemini (default ``gemini-3.8-flash``) with a free Google AI Studio key
    (``GEMINI_API_KEY``, optional ``GEMINI_MODEL``, ``GEMINI_THINKING``). Free tier: Google may use the requests to improve
    its products. Sent with ``store: false``, so the interaction is not kept on Google's side.
    Preferred when the key is set.
  - ``LocalCurator``: a free open model on this Mac through Ollama (default ``qwen3-vl:8b-instruct``,
    ``OLLAMA_MODEL`` / ``OLLAMA_URL`` in .env). Nothing leaves the computer.
  - ``Curator``: Claude through the Anthropic API (paid, needs ANTHROPIC_API_KEY).
The image is sent in memory as base64; nothing is stored.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import re
from typing import AsyncIterator, Optional

import anthropic
import httpx
from PIL import Image

log = logging.getLogger("curator")

MODEL = "claude-opus-5-5"
DOORS = 5
SIDE = 1024  # long side of the image sent to the model

LEAPS = ["dentro", "perché", "altrove", "storia", "scala"]

SYSTEM = """You are the curator of a visual encyclopedia for one curious, demanding person. They look at a \
picture; you see what in it is genuinely worth knowing, and open doors from it.

Each door starts from one specific thing that is visible in the picture (give its box) and leads to one \
concept that has its own Wikipedia article. A door is one of five kinds:
- dentro: the mechanism inside the thing (how it works, what it is made of);
- perché: why it is the way it is (the cause, the physics, the evolution);
- altrove: the same principle somewhere far away, in another field (the leap that surprises);
- storia: the human story attached to it (who discovered, used, feared or made it);
- scala: the same thing at a very different scale (atoms, cells, cities, galaxies).

What makes a good door:
- It is true. Prefer phenomena, mechanisms, laws, materials, techniques, histories over plain labels: \
  "superhydrophobicity" beats "water"; "phyllotaxis" beats "leaf"; "Rayleigh scattering" beats "sky".
- Its hook is one surprising, precise, verifiable fact that makes the person want to go through. No \
  marketing, no exclamation marks, no questions, no "did you know".
- The five doors are different from each other: different details of the picture, different kinds \
  (at least one altrove), different fields of knowledge.
- Never repeat or lightly vary a concept the person has already met (listed below). Go somewhere new.
- If the picture shows something famous (a species, a building, an artwork), you may use it, but the \
  best doors usually come from details and from what they mean.

Be brief: anchor 2-5 words, hook at most 18 words, image_query at most 6 words, seeing one short sentence.

The person reads Italian. Write anchor, title and hook in Italian. Give the article title exactly as it \
is on Italian Wikipedia when that article exists (else an empty string) and exactly as on English \
Wikipedia. image_query is an English search for a single striking photograph of the concept itself \
(concrete and visual: "water droplets on lotus leaf macro", "Fibonacci spiral sunflower head"), not of \
this picture."""

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
                "required": ["anchor", "x0", "y0", "x1", "y1", "leap", "title", "wikipedia_it", "wikipedia_en",
                             "hook", "image_query"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["seeing", "doors"],
    "additionalProperties": False,
}


def context_text(name: str, via: Optional[str], seen: list[str]) -> str:
    lines = [f"Picture: {name}."]
    if via:
        lines.append(f"The person arrived at this picture through the concept: {via}.")
    if seen:
        lines.append("Concepts already met (do not lead back to them or near variants): " + "; ".join(seen[-60:]) + ".")
    lines.append(f"Open {DOORS} doors, each from a different detail. Each box (x0, y0, x1, y1) tightly frames that detail, "
                 "as integers from 0 to 1000 relative to the picture's width and height.")
    return "\n".join(lines)


def normalise_door(d: dict) -> dict:
    """Box as fractions: some models answer in thousandths (Qwen-VL, Gemini) or pixels of the 1024 px image."""
    box = [float(d.get(k) or 0) for k in ("x0", "y0", "x1", "y1")]
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


def jpeg_b64(image: Image.Image) -> str:
    im = image.convert("RGB")
    im.thumbnail((SIDE, SIDE))
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

    def __init__(self, url: str, model: str):
        self.url = url.rstrip("/")
        self.model = model
        self.http = httpx.AsyncClient(timeout=300)

    async def available(self) -> bool:
        try:
            tags = (await self.http.get(f"{self.url}/api/tags", timeout=3)).json()
        except (httpx.HTTPError, ValueError):
            return False
        return any(m.get("name") == self.model or m.get("model") == self.model for m in tags.get("models", []))

    async def doors(self, image: Image.Image, name: str, via: Optional[str], seen: list[str],
                    deep: bool = True) -> Optional[dict]:
        body = {
            "model": self.model,
            "stream": False,
            "think": False,  # the schema already structures the answer; thinking would double the wait
            "format": SCHEMA,
            "options": {"temperature": 0.7, "num_ctx": 8192},
            "messages": [
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": context_text(name, via, seen), "images": [jpeg_b64(image)]},
            ],
        }
        try:
            resp = await self.http.post(f"{self.url}/api/chat", json=body)
            resp.raise_for_status()
            reply = resp.json()
            data = json.loads(reply["message"]["content"])
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            log.warning("local curator failed for %s: %s", name, exc)
            return None
        log.info("local curator %s: %s tokens out in %.1fs", name, reply.get("eval_count"), (reply.get("total_duration") or 0) / 1e9)
        return normalise(data)


class GeminiCurator(Engine):
    """The same curator on Google Gemini through the Interactions API: free with an AI Studio key."""

    URL = "https://generativelanguage.googleapis.com/v1beta/interactions"

    def __init__(self, api_key: str, model: str, thinking: str = "medium"):
        self.model = model
        self.thinking = thinking  # medium: 30-70 s and the richest doors; low: ~9 s, more obvious doors
        self.http = httpx.AsyncClient(timeout=120, headers={"x-goog-api-key": api_key})

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
        answer = AnswerStream()
        usage: dict = {}
        try:
            async with self.http.stream("POST", self.URL, json=body) as resp:
                if resp.status_code != 200:
                    text = (await resp.aread()).decode(errors="replace")
                    level = log.error if resp.status_code in (400, 401, 403) else log.warning
                    level("gemini curator: HTTP %s for %s: %s", resp.status_code, name, text[:300])
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
