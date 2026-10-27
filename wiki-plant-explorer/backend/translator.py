"""Local Italian -> English translation (Helsinki-NLP opus-mt-it-en, MarianMT, ~300 MB).

Commons titles and the CLIP text encoder are English, so queries are translated before searching.
It runs on this machine: queries never leave it. Whole phrases are translated at once ("fori nel
metallo" -> "holes in metal"), plurals included; English queries come back about unchanged.
"""

from __future__ import annotations

import logging

import torch
from transformers import MarianMTModel, MarianTokenizer

log = logging.getLogger("translator")

MODEL = "Helsinki-NLP/opus-mt-it-en"


class Translator:
    def __init__(self):
        log.info("Loading %s", MODEL)
        self.tokenizer = MarianTokenizer.from_pretrained(MODEL)
        self.model = MarianMTModel.from_pretrained(MODEL).eval()  # CPU: ~0.1 s per query
        self._cache: dict[str, str] = {}

    @torch.inference_mode()
    def translate(self, text: str) -> str:
        text = text.strip()
        if text not in self._cache:
            out = self.model.generate(**self.tokenizer([text], return_tensors="pt"), max_new_tokens=32, num_beams=4)
            self._cache[text] = self.tokenizer.decode(out[0], skip_special_tokens=True).strip().rstrip(".")
        return self._cache[text]
