"""DINOv2 + OpenCLIP image embedder.

  - ``visual``   : DINOv2 (facebook/dinov2-base) pooler output, 768-d. Shape / texture / style.
  - ``semantic`` : OpenCLIP ViT-B-32 (laion2b_s34b_b79k) image embedding, 512-d. Concept / subject.

Both are L2-normalised, so a dot product is the cosine similarity.
"""

from __future__ import annotations

import logging

import open_clip
import torch
from PIL import Image
from transformers import AutoImageProcessor, AutoModel

log = logging.getLogger("embedder")

VISUAL_DIM = 768
SEMANTIC_DIM = 512


def detect_device() -> str:
    if torch.cuda.is_available():
        return "cuda"
    if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
        return "mps"
    return "cpu"


class Embedder:
    def __init__(self, device: str | None = None):
        self.device = device or detect_device()
        log.info("Loading DINOv2 (facebook/dinov2-base) on %s", self.device)
        self.dino_processor = AutoImageProcessor.from_pretrained("facebook/dinov2-base")
        self.dino = AutoModel.from_pretrained("facebook/dinov2-base").to(self.device).eval()

        log.info("Loading OpenCLIP ViT-B-32 (laion2b_s34b_b79k) on %s", self.device)
        self.clip, _, self.clip_preprocess = open_clip.create_model_and_transforms(
            "ViT-B-32", pretrained="laion2b_s34b_b79k"
        )
        self.clip = self.clip.to(self.device).eval()

    @torch.inference_mode()
    def embed_batch(self, images: list[Image.Image]) -> tuple[list[list[float]], list[list[float]]]:
        dino_inputs = self.dino_processor(images=images, return_tensors="pt").to(self.device)
        visual = self.dino(**dino_inputs).pooler_output  # layer-normed CLS token, (n, 768)
        visual = torch.nn.functional.normalize(visual, dim=-1)

        clip_input = torch.stack([self.clip_preprocess(im) for im in images]).to(self.device)
        semantic = self.clip.encode_image(clip_input)  # (n, 512)
        semantic = torch.nn.functional.normalize(semantic, dim=-1)
        return visual.float().cpu().tolist(), semantic.float().cpu().tolist()
