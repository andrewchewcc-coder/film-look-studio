"""Local image-text model (SigLIP 2 via OpenCLIP) on the M1 GPU.

Turns each photo's thumbnail into an embedding so you can search in plain words, and
sorts photos into street / travel / portrait / misc. Runs offline once the model
weights are downloaded (one time, ~1.5 GB, cached in ~/.cache/huggingface).
"""
import os
import threading

import numpy as np

MODEL = "ViT-B-16-SigLIP2-256"
PRETRAINED = "webli"
VIEWS = 6  # whole photo + four corner close-ups + centre close-up: small things (a hat, a bicycle) show up
MODEL_ID = f"{MODEL}/{PRETRAINED}/views{VIEWS}"

# Several phrasings per category; a photo's score for a category is its best match.
CATEGORY_PROMPTS = {
    "street": [
        "a candid street photograph of strangers in a city",
        "street photography of people walking on a busy sidewalk",
        "a black and white street photo of a passer-by",
        "an unposed moment on a city street with shadows and light",
        "people at a market stall or hawker centre, candid",
    ],
    "travel": [
        "a travel photo of a famous landmark",
        "a scenic landscape photographed on holiday",
        "a tourist photo of historic architecture abroad",
        "a view of a foreign city skyline",
        "a beach, mountain or countryside view on a trip",
        "cherry blossoms, gardens or nature in a park",
        "a holiday snapshot of a friend or family member at a sight",
        "a selfie of a couple on holiday",
        "steam, mist or fog over a landscape",
    ],
    "portrait": [
        "a posed portrait of a person looking at the camera",
        "a portrait photo shoot of a model",
        "a close-up headshot of a person",
        "a group photo of friends or family posing",
    ],
    "misc": [
        "a screenshot of a phone app or website",
        "a photo of food on a plate",
        "a photo of a document, map, receipt or sign",
        "a product photo of an object on a table",
        "a photo of a pet or animal at home",
        "a close-up photo of a printed label or packaging",
    ],
}
CATEGORIES = list(CATEGORY_PROMPTS)

_lock = threading.Lock()
_model = None
# One model call at a time: PyTorch on the GPU crashes (segfault) if two threads run it
# at once, e.g. a scan embedding photos while a search embeds its words.
_infer = threading.Lock()


def device():
    import torch
    return "mps" if torch.backends.mps.is_available() else "cpu"


def _go_offline_if_cached():
    """Once the weights are downloaded, never touch the network again."""
    from pathlib import Path
    hub = Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface")) / "hub"
    if any(hub.glob(f"models--*{MODEL}*/snapshots/*")):
        os.environ.setdefault("HF_HUB_OFFLINE", "1")


def load():
    """(model, preprocess, tokenizer), loaded once."""
    global _model
    with _lock:
        if _model is None:
            _go_offline_if_cached()
            import open_clip
            import torch
            model, _, preprocess = open_clip.create_model_and_transforms(MODEL, pretrained=PRETRAINED)
            model = model.to(device()).eval()
            tokenizer = open_clip.get_tokenizer(MODEL)
            torch.set_grad_enabled(False)
            _model = (model, preprocess, tokenizer)
    return _model


def _normalise(x):
    x = x.float().cpu().numpy()
    return x / np.linalg.norm(x, axis=1, keepdims=True).clip(1e-8)


def embed_images(images):
    """List of PIL images -> (N, D) float32, L2-normalised."""
    import torch
    model, preprocess, _ = load()
    batch = torch.stack([preprocess(im.convert("RGB")) for im in images])
    with _infer, torch.inference_mode():
        return _normalise(model.encode_image(batch.to(device())))


def views(im):
    """The whole photo plus five 60% close-ups (corners and centre)."""
    im = im.convert("RGB")
    w, h = im.size
    cw, ch = int(w * 0.6), int(h * 0.6)
    boxes = [(x, y, x + cw, y + ch) for x in (0, w - cw) for y in (0, h - ch)]
    boxes.append(((w - cw) // 2, (h - ch) // 2, (w + cw) // 2, (h + ch) // 2))
    return [im] + [im.crop(b) for b in boxes]


def embed_photos(images):
    """List of PIL images -> (N, VIEWS, D): one vector per view of each photo."""
    flat = [v for im in images for v in views(im)]
    return embed_images(flat).reshape(len(images), VIEWS, -1)


def embed_text(texts):
    import torch
    model, _, tokenizer = load()
    with _infer, torch.inference_mode():
        return _normalise(model.encode_text(tokenizer(texts).to(device())))


_category_vecs = None


def classify(vectors):
    """(N, D) image embeddings -> [(category, margin)], margin = best minus runner-up."""
    global _category_vecs
    if _category_vecs is None:
        _category_vecs = {c: embed_text(p) for c, p in CATEGORY_PROMPTS.items()}
    scores = np.stack([(vectors @ _category_vecs[c].T).max(axis=1) for c in CATEGORIES], axis=1)
    order = np.argsort(-scores, axis=1)
    out = []
    for row, o in zip(scores, order):
        out.append((CATEGORIES[o[0]], float(row[o[0]] - row[o[1]])))
    return out
