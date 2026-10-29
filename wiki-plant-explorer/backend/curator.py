"""The curator: Claude looks at an image and opens doors to knowledge from what is in it.

Each door starts from something visible (with its box in the image) and leads to a concept worth
knowing, of one kind: what is inside it (the mechanism), why it happens, the same principle far
away, the human story, another scale. The curator also gets the concepts already met on this
journey, so it never leads back. Everything it says is then grounded by the caller: a door is kept
only if its Wikipedia article exists, and the text shown is Wikipedia's.

Needs ANTHROPIC_API_KEY (in .env). The image is sent in memory as base64; nothing is stored.
"""

from __future__ import annotations

import base64
import io
import json
import logging
from typing import Optional

import anthropic
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
                    "anchor": {"type": "string", "description": "The visible detail, 2-6 Italian words."},
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


def jpeg_b64(image: Image.Image) -> str:
    im = image.convert("RGB")
    im.thumbnail((SIDE, SIDE))
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=85)
    return base64.standard_b64encode(buf.getvalue()).decode()


class Curator:
    def __init__(self, api_key: str, model: str = MODEL):
        self.client = anthropic.AsyncAnthropic(api_key=api_key)
        self.model = model

    async def doors(self, image: Image.Image, name: str, via: Optional[str], seen: list[str]) -> Optional[dict]:
        """{"seeing", "doors": [...]} for ``image``, or None when the model declines or fails."""
        context = [f"Picture: {name}."]
        if via:
            context.append(f"The person arrived at this picture through the concept: {via}.")
        if seen:
            context.append("Concepts already met (do not lead back to them or near variants): " + "; ".join(seen[-60:]) + ".")
        context.append(f"Open {DOORS} doors. Boxes are fractions of the picture's width and height (0 to 1).")
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
                        {"type": "text", "text": "\n".join(context)},
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
        return data
