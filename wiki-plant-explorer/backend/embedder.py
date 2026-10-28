"""DINOv2 + OpenCLIP image embedder.

  - ``visual``   : DINOv2 (facebook/dinov2-base) pooler output, 768-d. Global look: what it looks like.
  - ``semantic`` : OpenCLIP ViT-B-32 (laion2b_s34b_b79k) image embedding, 512-d. Concept / subject.

DINOv2 also sees the image as a 16x16 grid of patch tokens. The same forward pass gives three more
descriptors (``dense``), each a different way of looking at the picture:

  - ``texture`` (1536-d): mean and spread of the mid-layer patch tokens over the whole image, like
    the feature statistics of style transfer (AdaIN). Surfaces, materials, colour patterns,
    regardless of what is depicted or where.
  - ``layout``  (768-d): the mid-layer tokens pooled on a 4x4 grid after removing the image's own
    average, each cell projected to 48-d (fixed random projection) and concatenated. Composition:
    where the edges, masses and contrasts are.
  - ``parts``   (k x 768): the last-layer patch tokens clustered (k-means) into up to PARTS regions
    of one "thing" each (sky, petal, eye, brick wall...). Searched with MaxSim, a single region of
    another image finds every image that contains something like it. ``parts_xy`` gives each
    region's centre and share of the image, in image coordinates.

All vectors are L2-normalised, so a dot product is the cosine similarity.
"""

from __future__ import annotations

import logging

import numpy as np
import open_clip
import torch
import torch.nn.functional as F
from PIL import Image
from transformers import AutoImageProcessor, AutoModel

log = logging.getLogger("embedder")

VISUAL_DIM = 768
SEMANTIC_DIM = 512
TEXTURE_DIM = 1536
LAYOUT_DIM = 768
PARTS = 6
MID_LAYER = 7  # of 12: below it DINOv2 sees edges and textures, above it objects and their parts
LAYOUT_GRID = 4
MAP_SIDE = 448  # long side of the image for the on-demand patch map: 32 patches
MAP_PARTS = 8


def detect_device() -> str:
    if torch.cuda.is_available():
        return "cuda"
    if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def kmeans(x: torch.Tensor, k: int, iters: int = 12) -> tuple[torch.Tensor, torch.Tensor]:
    """Spherical k-means on rows of ``x`` (n, d), unit-norm. Returns (centroids (k, d), labels (n,)).

    Deterministic: starts from the farthest-point sequence beginning at the token closest to the mean.
    """
    n = x.shape[0]
    k = min(k, n)
    first = int(torch.argmax(x @ F.normalize(x.mean(0), dim=0)))
    idx = [first]
    best = x @ x[first]
    for _ in range(k - 1):
        nxt = int(torch.argmin(best))
        idx.append(nxt)
        best = torch.maximum(best, x @ x[nxt])
    c = x[idx].clone()
    for _ in range(iters):
        labels = torch.argmax(x @ c.T, dim=1)
        for j in range(k):
            members = x[labels == j]
            if len(members):
                c[j] = F.normalize(members.mean(0), dim=0)
    return c, torch.argmax(x @ c.T, dim=1)


class Embedder:
    def __init__(self, device: str | None = None):
        self.device = device or detect_device()
        log.info("Loading DINOv2 (facebook/dinov2-base) on %s", self.device)
        self.dino_processor = AutoImageProcessor.from_pretrained("facebook/dinov2-base")
        self.dino = AutoModel.from_pretrained("facebook/dinov2-base").to(self.device).eval()
        self.mean = torch.tensor(self.dino_processor.image_mean).view(3, 1, 1)
        self.std = torch.tensor(self.dino_processor.image_std).view(3, 1, 1)
        # Fixed projection 768 -> 48 for the layout cells (numpy's legacy generator never changes).
        proj = np.random.RandomState(0).standard_normal((VISUAL_DIM, LAYOUT_DIM // LAYOUT_GRID**2))
        self.layout_proj = torch.tensor(np.linalg.qr(proj)[0], dtype=torch.float32)

        log.info("Loading OpenCLIP ViT-B-32 (laion2b_s34b_b79k) on %s", self.device)
        self.clip, _, self.clip_preprocess = open_clip.create_model_and_transforms(
            "ViT-B-32", pretrained="laion2b_s34b_b79k"
        )
        self.clip = self.clip.to(self.device).eval()
        self.clip_tokenizer = open_clip.get_tokenizer("ViT-B-32")

    @torch.inference_mode()
    def embed_batch(self, images: list[Image.Image]) -> tuple[list[list[float]], list[list[float]], list[dict]]:
        dino_inputs = self.dino_processor(images=images, return_tensors="pt").to(self.device)
        out = self.dino(**dino_inputs, output_hidden_states=True)
        visual = F.normalize(out.pooler_output, dim=-1)  # layer-normed CLS token, (n, 768)
        # The per-image work (k-means, pooling) is many tiny ops: faster on the CPU than on MPS.
        mid, last = out.hidden_states[MID_LAYER][:, 1:].float().cpu(), out.last_hidden_state[:, 1:].float().cpu()
        dense = [self._dense(mid[i], last[i], im.size) for i, im in enumerate(images)]

        clip_input = torch.stack([self.clip_preprocess(im) for im in images]).to(self.device)
        semantic = self.clip.encode_image(clip_input)  # (n, 512)
        semantic = F.normalize(semantic, dim=-1)
        return visual.float().cpu().tolist(), semantic.float().cpu().tolist(), dense

    def _dense(self, mid: torch.Tensor, last: torch.Tensor, size: tuple[int, int]) -> dict:
        """texture / layout / parts of one image from its 256 patch tokens (see the module docstring)."""
        side = int(round(mid.shape[0] ** 0.5))
        mid = F.normalize(mid, dim=-1)
        texture = torch.cat([F.normalize(mid.mean(0), dim=0), F.normalize(mid.std(0), dim=0)])

        grid = (mid - mid.mean(0)).T.reshape(1, -1, side, side)
        cells = F.adaptive_avg_pool2d(grid, LAYOUT_GRID).reshape(VISUAL_DIM, -1).T  # (16, 768)
        layout = (F.normalize(cells, dim=-1) @ self.layout_proj).flatten()

        centroids, labels = kmeans(F.normalize(last, dim=-1), PARTS)
        # Region centres: the processor resized the short side to 256 and cropped the central 224.
        w, h = size
        fx, fy = (224 / 256) * min(w, h) / w, (224 / 256) * min(w, h) / h
        ys, xs = torch.meshgrid(torch.arange(side), torch.arange(side), indexing="ij")
        xs, ys = (xs.flatten().float() + 0.5) / side, (ys.flatten().float() + 0.5) / side
        parts, xy = [], []
        for j in range(centroids.shape[0]):
            m = labels == j
            if int(m.sum()) == 0:
                continue
            parts.append(centroids[j].tolist())
            xy.append([round(0.5 + (float(xs[m].mean()) - 0.5) * fx, 3),
                       round(0.5 + (float(ys[m].mean()) - 0.5) * fy, 3),
                       round(float(m.float().mean()), 3)])
        return {
            "texture": F.normalize(texture, dim=0).tolist(),
            "layout": F.normalize(layout, dim=0).tolist(),
            "parts": parts,
            "parts_xy": xy,
        }

    @torch.inference_mode()
    def patch_map(self, image: Image.Image) -> dict:
        """The image as DINOv2 sees it, at higher resolution and in its own aspect ratio.

        Returns the grid size, the last-layer patch tokens (unit-norm, for picking a detail), an RGB
        colour per patch (first three principal components: patches of the same kind of thing get
        the same colour) and a segment id per patch (k-means regions).
        """
        w, h = image.size
        scale = MAP_SIDE / max(w, h)
        gw, gh = max(1, round(w * scale / 14)), max(1, round(h * scale / 14))
        x = torch.from_numpy(np.asarray(image.convert("RGB").resize((gw * 14, gh * 14), Image.BICUBIC), dtype=np.float32) / 255)
        x = ((x.permute(2, 0, 1) - self.mean) / self.std)[None].to(self.device)
        tokens = F.normalize(self.dino(pixel_values=x).last_hidden_state[0, 1:].float().cpu(), dim=-1)  # (gh*gw, 768)

        centred = tokens - tokens.mean(0)
        _, _, v = torch.pca_lowrank(centred, q=3, center=False)
        pcs = centred @ v[:, :3]
        lo = torch.quantile(pcs, 0.02, dim=0)
        hi = torch.quantile(pcs, 0.98, dim=0)
        rgb = ((pcs - lo) / (hi - lo + 1e-6)).clamp(0, 1)
        _, segments = kmeans(tokens, MAP_PARTS)
        return {
            "w": gw,
            "h": gh,
            "tokens": tokens.numpy(),
            "rgb": (rgb.numpy() * 255).astype(np.uint8),
            "segments": segments.numpy().astype(np.uint8),
        }

    @torch.inference_mode()
    def embed_text(self, prompts: list[str]) -> list[float]:
        """Mean CLIP text embedding of ``prompts`` (a small prompt ensemble), in the ``semantic`` space."""
        tokens = self.clip_tokenizer(prompts).to(self.device)
        text = F.normalize(self.clip.encode_text(tokens), dim=-1).mean(dim=0)
        return F.normalize(text, dim=-1).float().cpu().tolist()
