"""DINOv2 + OpenCLIP image embedder.

  - ``visual``   : DINOv2 (facebook/dinov2-base) pooler output, 768-d. Shape / texture / style.
  - ``semantic`` : OpenCLIP ViT-B-32 (laion2b_s34b_b79k) image embedding, 512-d. Concept / subject.

Both are L2-normalised, so a dot product is the cosine similarity.

``regions`` splits one image into the things it shows, from DINOv2's patch tokens: patches of the
same thing (an eye, a window, a wave) have similar tokens, so clustering them and keeping the
connected blobs gives the parts of the picture. These are the "links" of an image page.
"""

from __future__ import annotations

import logging
from collections import deque

import numpy as np
import open_clip
import torch
import torch.nn.functional as F
from PIL import Image
from transformers import AutoImageProcessor, AutoModel

log = logging.getLogger("embedder")

VISUAL_DIM = 768
SEMANTIC_DIM = 512
MAP_SIDE = 448  # long side of the image for the patch map: 32 patches of 14 px
MAP_CLUSTERS = 10


def detect_device() -> str:
    if torch.cuda.is_available():
        return "cuda"
    if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def kmeans(x: torch.Tensor, k: int, iters: int = 15) -> torch.Tensor:
    """Spherical k-means on unit rows of ``x`` (n, d); returns a cluster label per row.

    Deterministic: starts from the farthest-point sequence beginning at the row closest to the mean.
    """
    k = min(k, x.shape[0])
    idx = [int(torch.argmax(x @ F.normalize(x.mean(0), dim=0)))]
    best = x @ x[idx[0]]
    for _ in range(k - 1):
        idx.append(int(torch.argmin(best)))
        best = torch.maximum(best, x @ x[idx[-1]])
    c = x[idx].clone()
    for _ in range(iters):
        labels = torch.argmax(x @ c.T, dim=1)
        for j in range(k):
            members = x[labels == j]
            if len(members):
                c[j] = F.normalize(members.mean(0), dim=0)
    return torch.argmax(x @ c.T, dim=1)


def blobs(labels: np.ndarray) -> list[np.ndarray]:
    """Connected components (4-neighbour) of equal labels on a 2-D grid, as boolean masks."""
    h, w = labels.shape
    seen = np.zeros_like(labels, dtype=bool)
    out = []
    for y0 in range(h):
        for x0 in range(w):
            if seen[y0, x0]:
                continue
            mask = np.zeros_like(seen)
            queue = deque([(y0, x0)])
            seen[y0, x0] = True
            while queue:
                y, x = queue.popleft()
                mask[y, x] = True
                for ny, nx in ((y + 1, x), (y - 1, x), (y, x + 1), (y, x - 1)):
                    if 0 <= ny < h and 0 <= nx < w and not seen[ny, nx] and labels[ny, nx] == labels[y, x]:
                        seen[ny, nx] = True
                        queue.append((ny, nx))
            out.append(mask)
    return out


class Embedder:
    def __init__(self, device: str | None = None):
        self.device = device or detect_device()
        log.info("Loading DINOv2 (facebook/dinov2-base) on %s", self.device)
        self.dino_processor = AutoImageProcessor.from_pretrained("facebook/dinov2-base")
        self.dino = AutoModel.from_pretrained("facebook/dinov2-base").to(self.device).eval()
        self.mean = torch.tensor(self.dino_processor.image_mean).view(3, 1, 1)
        self.std = torch.tensor(self.dino_processor.image_std).view(3, 1, 1)

        log.info("Loading OpenCLIP ViT-B-32 (laion2b_s34b_b79k) on %s", self.device)
        self.clip, _, self.clip_preprocess = open_clip.create_model_and_transforms(
            "ViT-B-32", pretrained="laion2b_s34b_b79k"
        )
        self.clip = self.clip.to(self.device).eval()
        self.clip_tokenizer = open_clip.get_tokenizer("ViT-B-32")

    @torch.inference_mode()
    def embed_batch(self, images: list[Image.Image]) -> tuple[list[list[float]], list[list[float]]]:
        dino_inputs = self.dino_processor(images=images, return_tensors="pt").to(self.device)
        visual = self.dino(**dino_inputs).pooler_output  # layer-normed CLS token, (n, 768)
        visual = F.normalize(visual, dim=-1)

        clip_input = torch.stack([self.clip_preprocess(im) for im in images]).to(self.device)
        semantic = self.clip.encode_image(clip_input)  # (n, 512)
        semantic = F.normalize(semantic, dim=-1)
        return visual.float().cpu().tolist(), semantic.float().cpu().tolist()

    @torch.inference_mode()
    def regions(self, image: Image.Image) -> dict:
        """The things in ``image``, as DINOv2 sees them.

        The image is read at MAP_SIDE px on its long side, in its own aspect ratio (DINOv2 adapts its
        position embeddings), giving a grid of up to 32 x 32 patch tokens. Their k-means clusters are
        kinds of things; the connected blobs of a cluster are the things themselves. Returns the grid
        size and a blob id per patch (``blob``), plus every blob's mask.
        """
        w, h = image.size
        scale = MAP_SIDE / max(w, h)
        gw, gh = max(2, round(w * scale / 14)), max(2, round(h * scale / 14))
        x = torch.from_numpy(np.asarray(image.convert("RGB").resize((gw * 14, gh * 14), Image.BICUBIC), dtype=np.float32) / 255)
        x = ((x.permute(2, 0, 1) - self.mean) / self.std)[None].to(self.device)
        tokens = F.normalize(self.dino(pixel_values=x).last_hidden_state[0, 1:].float().cpu(), dim=-1)
        labels = kmeans(tokens, MAP_CLUSTERS).numpy().reshape(gh, gw)
        return {"w": gw, "h": gh, "masks": blobs(labels)}

    @torch.inference_mode()
    def embed_texts(self, texts: list[str]) -> np.ndarray:
        """One CLIP text embedding per text (n, 512), unit-norm."""
        tokens = self.clip_tokenizer(texts).to(self.device)
        return F.normalize(self.clip.encode_text(tokens), dim=-1).float().cpu().numpy()

    @torch.inference_mode()
    def embed_text(self, prompts: list[str]) -> list[float]:
        """Mean CLIP text embedding of ``prompts`` (a small prompt ensemble), in the ``semantic`` space."""
        tokens = self.clip_tokenizer(prompts).to(self.device)
        text = F.normalize(self.clip.encode_text(tokens), dim=-1).mean(dim=0)
        return F.normalize(text, dim=-1).float().cpu().tolist()
