"""Claude in the app: the API key (macOS Keychain only) and calls to the Claude API.

The key is pasted once into ⚙ Settings → Claude and stored in the Keychain, like the WordPress
password; it is never written to a file, logged or shown again. Photos sent to Claude are only
small previews (about 1024 px, no GPS or camera data).
"""
import base64
import io
import json
import subprocess
from datetime import date

import anthropic

KEYCHAIN_SERVICE = "Film Look Studio – Claude API"
ACCOUNT = "anthropic"
MODEL = "claude-opus-5-5"
PRICE_IN, PRICE_OUT = 4.00, 20.00          # US$ per million tokens (Claude Opus 5.5)
PREVIEW_EDGE = 1024                         # what Claude sees of a photo: long edge in pixels


def get_key():
    r = subprocess.run(["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", ACCOUNT, "-w"],
                       capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None


def has_key():
    return get_key() is not None


def _client(key=None):
    key = key or get_key()
    if not key:
        raise PermissionError("No Claude API key yet: add it in ⚙ Settings → Claude.")
    return anthropic.Anthropic(api_key=key, timeout=300.0)


def _friendly(e):
    """An API error as a sentence for the app."""
    if isinstance(e, anthropic.AuthenticationError):
        return PermissionError("Claude didn't accept this key. Copy it again from console.anthropic.com → API keys.")
    if isinstance(e, anthropic.PermissionDeniedError):
        return PermissionError("This key isn't allowed to do that. Check it at console.anthropic.com.")
    if isinstance(e, anthropic.RateLimitError):
        return RuntimeError("Claude is busy, or your spending limit was reached. Try again in a minute.")
    if isinstance(e, anthropic.BadRequestError) and ("credit" in str(e).lower() or "billing" in str(e).lower()):
        return PermissionError("Your Anthropic account has no credit left. Add some under Billing at console.anthropic.com.")
    if isinstance(e, anthropic.APIConnectionError):
        return RuntimeError("Couldn't reach Claude. Is the Mac online?")
    if isinstance(e, anthropic.APIStatusError):
        return RuntimeError(f"Claude answered with an error ({e.status_code}). Try again later.")
    return e


def check(key=None):
    """Is the key accepted? Lists the models (free: uses no credit)."""
    try:
        _client(key).models.list(limit=1)
    except anthropic.APIError as e:
        raise _friendly(e) from None
    return True


def save_key(key):
    """Check the key with Anthropic, then keep it in the Keychain (never in a file)."""
    key = (key or "").strip()
    if not key.startswith("sk-ant-"):
        raise ValueError("That doesn't look like a Claude API key (they start with sk-ant-).")
    check(key)
    r = subprocess.run(["security", "add-generic-password", "-U", "-s", KEYCHAIN_SERVICE, "-a", ACCOUNT,
                        "-l", KEYCHAIN_SERVICE, "-w", key], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("Couldn't save the key in the Keychain: " + (r.stderr.strip() or "unknown error"))


def forget():
    subprocess.run(["security", "delete-generic-password", "-s", KEYCHAIN_SERVICE, "-a", ACCOUNT], capture_output=True)


# --- sending photos ----------------------------------------------------------------------------

def preview_block(im):
    """A photo as Claude sees it: at most 1024 px, re-encoded, so no EXIF (GPS, camera) goes along."""
    im = im.convert("RGB")
    im.thumbnail((PREVIEW_EDGE, PREVIEW_EDGE))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=85)                 # Pillow writes no EXIF unless asked
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                        "data": base64.standard_b64encode(buf.getvalue()).decode()}}


def estimate(n_photos, text_tokens=1500, out_tokens=6000):
    """Rough cost in US$ before asking: ~1,000 tokens per 1024 px photo."""
    return (n_photos * 1000 + text_tokens) * PRICE_IN / 1e6 + out_tokens * PRICE_OUT / 1e6


def ask_json(system, content, schema, effort="medium", max_tokens=16000):
    """One request; Claude's answer as a dict matching `schema`. Returns (answer, cost in US$)."""
    try:
        r = _client().beta.messages.create(
            model=MODEL, max_tokens=max_tokens, system=system,
            messages=[{"role": "user", "content": content}],
            output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
            betas=["server-side-fallback-2026-07-01"], fallbacks="default",
        )
    except anthropic.APIError as e:
        raise _friendly(e) from None
    if r.stop_reason == "refusal":
        raise RuntimeError("Claude declined to review this. Try again, or with fewer photos.")
    if r.stop_reason == "max_tokens":
        raise RuntimeError("Claude's answer was too long and got cut off. Try with fewer photos.")
    text = next((b.text for b in r.content if b.type == "text"), "")
    cost = (r.usage.input_tokens * PRICE_IN + r.usage.output_tokens * PRICE_OUT) / 1e6
    log_cost(cost)
    return json.loads(text), cost


# --- what it has cost ----------------------------------------------------------------------------

def _usage_file():
    from .library import LIBRARY_DIR
    return LIBRARY_DIR / "claude_usage.json"


def log_cost(cost):
    f = _usage_file()
    try:
        u = json.loads(f.read_text())
    except (OSError, ValueError):
        u = {}
    m = date.today().strftime("%Y-%m")
    u[m] = round(u.get(m, 0.0) + cost, 4)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(u, indent=2))


def month_cost():
    try:
        return json.loads(_usage_file().read_text()).get(date.today().strftime("%Y-%m"), 0.0)
    except (OSError, ValueError):
        return 0.0
