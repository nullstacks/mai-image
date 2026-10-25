#!/usr/bin/env python3
"""
mai_image.py - Bing Image Creator client with two auth modes.

  cookie mode : your signed-in bing.com cookies (persistent daily tokens)
  fresh mode  : no cookies at all; a brand-new anonymous session each time,
                rotated automatically when its small quota runs out

Reverse engineered from https://www.bing.com/images/create/ai-image-generator

Flow:
  1. POST /images/create/ai-image-generator?FORM=GENCRE&q=..&ctype=image&mdl=..&ar=..&rt=3
     -> HTML page carrying data-polling-url (absent when refused)
  2. GET the polling URL until {"status": 2}, then read records[0].mediaItems[]
  3. Download mediaItems[].src from th.bing.com

IMPORTANT, verified 2026-10-25: the `mdl` parameter is currently IGNORED by Bing.
Every value - including a bogus 999 - renders on MAI-Image-2.5-Flash, 4 images per
request, both signed-in and anonymous. Model names are kept below so the script
still sends the right id if Bing re-enables selection, and the server-reported name
is always printed so you can see what actually ran.
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import math
import os
import re
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    from curl_cffi import requests  # type: ignore

    BACKEND = "curl_cffi"
except Exception:  # pragma: no cover
    import requests  # type: ignore

    BACKEND = "requests"

BASE = "https://www.bing.com"
CREATE_URL = f"{BASE}/images/create/ai-image-generator"
FORM_CODE = "GENCRE"
UPLOAD_URL = f"{BASE}/images/create/upload?"

# name -> (mdl id, allowed aspect ratios, images per request, registry says "Upload")
# The `accepts_ref` column mirrors the capabilities array in the site's GmRegistry.
MODELS: Dict[str, Tuple[str, Tuple[str, ...], int, bool]] = {
    "mai-image-2.5-flash": ("10", ("1:1", "3:2", "2:3"), 4, True),
    "mai-image-2": ("7", ("1:1", "3:2", "2:3"), 4, False),
    "mai-image-2.5": ("10", ("1:1", "3:2", "2:3"), 1, True),
    "mai-image-1": ("4", ("1:1", "3:2", "2:3"), 1, False),
    "dalle3": ("0", ("1:1", "7:4", "4:7"), 1, False),
    "gpt4o": ("1", ("1:1", "3:2", "2:3"), 1, True),
}
# Friendly aliases -> canonical key above.
MODEL_ALIASES = {
    "mai-flash": "mai-image-2.5-flash",
    "flash": "mai-image-2.5-flash",
    "mai-image-2.5flash": "mai-image-2.5-flash",
    "maiimage25flash": "mai-image-2.5-flash",
    "maiimage2": "mai-image-2",
    "maiimage1": "mai-image-1",
    "dalle": "dalle3",
    "gpt4": "gpt4o",
}
# MAI-Image-2.5-Flash is what bing actually serves today, so it is the default.
DEFAULT_MODEL = "mai-image-2.5-flash"

ASPECTS = {"1:1": "1", "7:4": "2", "4:7": "3", "3:2": "4", "2:3": "5"}
ASPECT_ALIASES = {
    "square": "1:1", "landscape": "3:2", "wide": "3:2", "portrait": "2:3",
    "tall": "2:3", "16:9": "3:2", "9:16": "2:3",
}
SIZE_TO_ASPECT = {
    "1024x1024": "1:1", "1536x1024": "3:2", "1024x1536": "2:3",
    "1792x1024": "3:2", "1024x1792": "2:3",
}

# the creator clamps uploads to this pixel area at JPEG q0.8 before base64
UPLOAD_MAX_AREA = 36 * 10**4
UPLOAD_JPEG_QUALITY = 80
# Bing accepts exactly one reference image. Verified 2026-10-25:
#   - the page's only file input has no `multiple` attribute
#   - attachImageInternal() un-attaches the previous image first
#   - two imageBase64 parts -> one bcid, and it is the FIRST image only
#   - two bcid params in submit -> data-status="3", no generation
MAX_REFERENCES = 1

# empty sessions get ~5 requests (20 images) before hitting nsiindivlimit
FRESH_REQUESTS_PER_SESSION = 5

COOKIE_ENV = "MAI_COOKIES_JSON"
SKILL_ROOT = Path(__file__).resolve().parent.parent

ERROR_HINTS = {
    "nsiindivlimit": (
        "No image quota left. In fresh mode this just means rotate - the script does "
        "it automatically. In cookie mode it is the account's daily allowance."
    ),
    "insufficientcredits": "Not enough image boosts left today.",
    "ratelimit": "Rate limited - wait a minute and retry.",
    "suspended": "The prompt (or the account) was blocked by the safety filter.",
    "vagueprompt": "Bing judged the prompt too vague - add detail.",
    "unavailable": "Image generation is temporarily unavailable.",
    "realnameauth": "This account needs real-name verification before generating.",
}


class QuotaExhausted(RuntimeError):
    """Signals that this session has no generation budget left."""


class RetryableSubmit(RuntimeError):
    """A transient 'no request started' answer - worth retrying, not fatal."""


# --------------------------------------------------------------------------- #
# cookies
# --------------------------------------------------------------------------- #
def candidate_cookie_paths() -> List[Path]:
    paths: List[Path] = []
    env = os.environ.get(COOKIE_ENV)
    if env:
        paths.append(Path(env).expanduser())
    for root in (SKILL_ROOT, Path.cwd()):
        for name in ("cookies.json", "cookies.json.txt", "cookies.txt"):
            paths.append(root / name)
    return paths


def find_cookies_file(hint: Optional[str] = None) -> Optional[Path]:
    if hint:
        p = Path(hint).expanduser()
        if not p.is_file():
            raise FileNotFoundError(f"cookies file not found: {p}")
        return p
    for p in candidate_cookie_paths():
        if p.is_file():
            return p
    return None


def load_cookies(path: Path) -> List[Dict[str, str]]:
    with open(path, "r", encoding="utf-8-sig") as fh:
        data = json.load(fh)

    out: List[Dict[str, str]] = []
    if isinstance(data, list):
        for c in data:
            if isinstance(c, dict) and c.get("name") and c.get("value") is not None:
                out.append({
                    "name": str(c["name"]), "value": str(c["value"]),
                    "domain": c.get("domain") or ".bing.com",
                    "path": c.get("path") or "/",
                })
    elif isinstance(data, dict):
        for name, value in data.items():
            out.append({"name": name, "value": str(value), "domain": ".bing.com", "path": "/"})
    else:
        raise ValueError("unsupported cookies format (expected list or dict)")

    if not out:
        raise ValueError(f"no usable cookies in {path}")
    return out


# --------------------------------------------------------------------------- #
# session management (the heart of the two modes)
# --------------------------------------------------------------------------- #
class SessionManager:
    """Hands out bing sessions, either cookie-backed or blank.

    Blank sessions each carry their own small quota, so when one dries up the
    manager throws it away and starts another. Cookie sessions are long-lived and
    never rotated (they would just burn the same account's allowance).
    """

    def __init__(self, mode: str = "fresh", cookies_file: Optional[str] = None):
        self.mode = mode
        self.cookies_path = find_cookies_file(cookies_file) if mode == "cookie" else None
        if mode == "cookie" and self.cookies_path is None:
            raise FileNotFoundError(
                "cookie mode requested but no cookies file was found. "
                f"Searched: {', '.join(str(p) for p in candidate_cookie_paths())}"
            )
        self.cookie_data: List[Dict[str, str]] = (
            load_cookies(self.cookies_path) if self.cookies_path else []
        )
        self.rotations = 0
        self._session: Any = None
        self._used_in_session = 0

    # -- construction ------------------------------------------------------ #
    def _build(self) -> Any:
        if BACKEND == "curl_cffi":
            http = requests.Session(impersonate="chrome")
        else:
            http = requests.Session()

        if self.mode == "cookie":
            for c in self.cookie_data:
                try:
                    http.cookies.set(c["name"], c["value"], domain=c["domain"], path=c["path"])
                except Exception:
                    http.cookies.set(c["name"], c["value"])

        http.headers.update({
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "accept-language": "en-US,en;q=0.9",
            "origin": BASE,
            "referer": CREATE_URL,
            "user-agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0"
            ),
        })
        return http

    @property
    def http(self) -> Any:
        if self._session is None:
            self._session = self._build()
            # Warm-up: the creator page hands out MUID/_SS and seeds server-side
            # state. Posting to a cold jar answers 'requestmissed'.
            try:
                self._session.get(CREATE_URL, timeout=60)
            except Exception:
                pass
        return self._session

    # -- rotation ---------------------------------------------------------- #
    @property
    def can_rotate(self) -> bool:
        return self.mode == "fresh"

    def rotate(self) -> None:
        """Throw away the current session and start a blank one."""
        self._session = None
        self._used_in_session = 0
        self.rotations += 1

    def note_request(self) -> None:
        self._used_in_session += 1

    def describe(self) -> str:
        if self.mode == "cookie":
            return f"cookie mode ({self.cookies_path})"
        return "fresh mode (no cookies, rotating anonymous sessions)"

    # -- identity inspection ---------------------------------------------- #
    def auth_state(self, timeout: int = 60) -> Dict[str, Any]:
        """Report the account state bing sees for the current session."""
        resp = self.http.get(CREATE_URL, timeout=timeout)
        text = resp.text or ""
        m = re.search(
            r'"imageTokens":(\d+),"imageCredits":(\d+),"videoCredits":\d+,"rewardPoints":(\d+)'
            r'[^}]*?"isSignedIn":(true|false),"isMuidUser":(true|false)',
            text,
        )
        info: Dict[str, Any] = {"http": resp.status_code, "mode": self.mode}
        if self.cookies_path:
            info["cookies_file"] = str(self.cookies_path)
        if m:
            info.update({
                "image_tokens": int(m.group(1)),
                "image_credits": int(m.group(2)),
                "reward_points": int(m.group(3)),
                "signed_in": m.group(4) == "true",
                "is_muid_user": m.group(5) == "true",
            })
        return info


# --------------------------------------------------------------------------- #
# generation
# --------------------------------------------------------------------------- #
def _unescape(text: str) -> str:
    return (text.replace("&quot;", '"').replace("&#39;", "'")
                .replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">"))


def _attr(tag: str, name: str) -> Optional[str]:
    """Read one attribute from a RAW (still HTML-escaped) tag.

    Must run before unescaping: `data-results` holds JSON whose inner `&quot;`
    would otherwise become real quotes and cut the attribute short.
    """
    m = re.search(rf'{name}="([^"]*)"', tag)
    return m.group(1) if m else None


def parse_submit_page(raw_html: str) -> Dict[str, Any]:
    """Pull the meaningful state out of the POST response.

    Raw HTML only. Returns status / request_id / polling_url / inline records /
    error type, where status is the `data-status` of <aigc-results-view>:
        0,1 -> still working (poll)
        2   -> finished, results may already be embedded
        3   -> refused (usually quota or filter)
    """
    out: Dict[str, Any] = {
        "status": None, "request_id": None, "polling_url": None,
        "records": [], "error": None,
    }
    view = re.search(r"<aigc-results-view[^>]*>", raw_html)
    if view:
        tag = view.group(0)
        st = _attr(tag, "data-status")
        out["status"] = int(st) if st and st.isdigit() else None
        out["request_id"] = _attr(tag, "data-request-id") or None
        pu = _attr(tag, "data-polling-url")
        out["polling_url"] = pu or None

        res = _attr(tag, "data-results")
        if res:
            try:
                payload = json.loads(_unescape(res))
                recs = payload.get("records") or []
                out["records"] = recs
                if recs:
                    out.setdefault("model", recs[0].get("model"))
            except Exception:
                pass

    viewer = re.search(r"<aigc-media-viewer[^>]*>", raw_html)
    if viewer:
        el = viewer.group(0)
        vstatus = _attr(el, "data-status")
        etype = _attr(el, "data-error-type")
        # `requestmissed` lingers on perfectly successful viewers - ignore it
        if etype and etype != "requestmissed":
            out["error"] = etype
        elif vstatus == "error" and etype:
            out["error"] = etype
    return out


def resolve_model(model: Optional[str]) -> Tuple[str, str, Tuple[str, ...], int, bool]:
    key = (model or DEFAULT_MODEL).strip().lower()
    key = MODEL_ALIASES.get(key, key)
    if key not in MODELS:
        raise ValueError(
            f"unknown model {model!r}; choose from "
            f"{', '.join(k for k in MODELS if k != 'mai-image-2.5')}"
        )
    mdl, aspects, batch, accepts_ref = MODELS[key]
    return key, mdl, aspects, batch, accepts_ref


def resolve_aspect(aspect: Optional[str], allowed: Tuple[str, ...], size: Optional[str]) -> str:
    raw = (size or aspect or "1:1").strip().lower()
    raw = SIZE_TO_ASPECT.get(raw, ASPECT_ALIASES.get(raw, raw))
    if raw not in ASPECTS:
        raise ValueError(f"unknown aspect {aspect!r}; choose from {', '.join(allowed)}")
    if raw not in allowed:
        raise ValueError(
            f"aspect {raw} is not supported by this model; use one of {', '.join(allowed)}"
        )
    return raw


def submit(
    manager: SessionManager,
    prompt: str,
    model: str = DEFAULT_MODEL,
    aspect: str = "1:1",
    timeout: int = 120,
    bcid: Optional[str] = None,
) -> Dict[str, Any]:
    """POST the prompt; return request_id / polling_url / reported model."""
    key, mdl, aspects, _, _ = resolve_model(model)
    ar = resolve_aspect(aspect, aspects, None)

    params = {
        "FORM": FORM_CODE, "q": prompt, "ctype": "image",
        "mdl": mdl, "ar": ASPECTS[ar],
        # rt: 3 = free/anonymous, 4 = account has paid image tokens
        "rt": "4" if manager.mode == "cookie" else "3",
    }
    if bcid:
        params["bcid"] = bcid
        params["edt"] = "1"

    resp = manager.http.post(f"{CREATE_URL}?{urllib.parse.urlencode(params)}", timeout=timeout)
    raw = resp.text or ""
    page = parse_submit_page(raw)

    err = page["error"]
    if err in ("nsiindivlimit", "insufficientcredits"):
        raise QuotaExhausted(err)

    # a finished response may carry its images inline with no polling url
    inline = [it for rec in page["records"] for it in (rec.get("mediaItems") or [])]
    if inline:
        manager.note_request()
        manager._last_items = inline  # type: ignore[attr-defined]
        return {
            "request_id": page["request_id"], "polling_url": None,
            "model": page.get("model") or key, "batch": len(inline), "aspect": ar,
            "items": inline,
        }

    if not page["polling_url"]:
        reason = err or f"HTTP {resp.status_code}"
        if reason in ("requestmissed",) or reason.startswith("HTTP 5"):
            raise RetryableSubmit(reason)
        raise RuntimeError(
            f"bing did not start a generation ({reason}). {ERROR_HINTS.get(reason, '')}".strip()
        )

    manager.note_request()
    return {
        "request_id": page["request_id"],
        "polling_url": BASE + page["polling_url"],
        "model": page.get("model") or key,
        "batch": None,
        "aspect": ar,
        "items": None,
    }


def poll(manager: SessionManager, polling_url: str, timeout: int = 300,
         interval: float = 2.0) -> List[Dict[str, Any]]:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            data = manager.http.get(
                polling_url, timeout=60,
                headers={"accept": "application/json, text/plain, */*", "referer": CREATE_URL},
            ).json()
        except Exception:
            data = {}

        last = data.get("status")
        if last == 2:
            records = data.get("records") or []
            if not records:
                raise RuntimeError("generation finished but returned no records")
            rec = records[0]
            if rec.get("errorType") or rec.get("requestStatus") == 3:
                raise RuntimeError(
                    f"generation failed (errorType={rec.get('errorType')}). "
                    "The prompt was probably filtered."
                )
            if rec.get("mediaItems"):
                return rec["mediaItems"]
        elif last not in (0, 1, None):
            raise RuntimeError(f"generation failed with status {last}")

        time.sleep(interval)

    raise TimeoutError(f"timed out after {timeout}s (last status={last})")


def upload_reference(manager: SessionManager, image_path: Path, timeout: int = 120) -> str:
    """Upload a reference image; return its bcid."""
    if not image_path.is_file():
        raise FileNotFoundError(f"reference image not found: {image_path}")
    try:
        from PIL import Image  # type: ignore
    except ImportError as exc:
        raise RuntimeError("Pillow is required for --ref: pip install pillow") from exc

    img = Image.open(image_path).convert("RGB")
    w, h = img.size
    if w * h > UPLOAD_MAX_AREA:
        scale = math.sqrt(UPLOAD_MAX_AREA / float(w * h))
        img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=UPLOAD_JPEG_QUALITY)
    payload = base64.b64encode(buf.getvalue()).decode()

    sid = None
    for name in ("_SS", "_EDGE_S"):
        val = manager.http.cookies.get(name)
        if val:
            m = re.search(r"SID=([0-9A-Fa-f]+)", str(val))
            if m:
                sid = m.group(1)
                break
    url = UPLOAD_URL + (f"&sid={sid}" if sid else "")

    if BACKEND == "curl_cffi":
        from curl_cffi import CurlMime  # type: ignore

        mp = CurlMime()
        mp.addpart(name="imageBase64", data=payload)
        try:
            resp = manager.http.post(url, multipart=mp, timeout=timeout,
                                     headers={"accept": "*/*", "origin": BASE, "referer": CREATE_URL})
        finally:
            mp.close()
    else:
        resp = manager.http.post(url, files={"imageBase64": (None, payload)}, timeout=timeout,
                                 headers={"accept": "*/*", "origin": BASE, "referer": CREATE_URL})

    if resp.status_code != 200:
        raise RuntimeError(f"reference upload failed (HTTP {resp.status_code})")
    data = resp.json()
    if data.get("error") or not data.get("bcid"):
        raise RuntimeError(f"reference upload rejected: {data}")
    return data["bcid"]


def generate(
    manager: SessionManager,
    prompt: str,
    model: str = DEFAULT_MODEL,
    aspect: str = "1:1",
    batches: int = 1,
    timeout: int = 300,
    quiet: bool = False,
    bcid: Optional[str] = None,
    max_rotations: int = 10,
) -> List[Dict[str, Any]]:
    """Generate `batches` requests, rotating blank sessions when quota runs out."""
    items: List[Dict[str, Any]] = []
    i = 0
    transient_retries = 0
    while i < max(1, batches):
        try:
            info = submit(manager, prompt, model=model, aspect=aspect, bcid=bcid)
        except QuotaExhausted as exc:
            if not manager.can_rotate or manager.rotations >= max_rotations:
                raise RuntimeError(
                    f"out of quota ({exc}) and cannot rotate further "
                    f"({manager.rotations} rotations). Use cookie mode, or wait for the daily reset."
                ) from exc
            manager.rotate()
            if not quiet:
                print(f"  quota spent -> rotated to a fresh session "
                      f"(#{manager.rotations})", file=sys.stderr)
            time.sleep(1.5)
            continue
        except RetryableSubmit as exc:
            transient_retries += 1
            if transient_retries > 6:
                raise RuntimeError(
                    f"bing kept refusing to start the request ({exc}) after "
                    f"{transient_retries} tries. It may be temporarily blocking this IP; "
                    "wait a minute and retry."
                ) from exc
            if not quiet:
                print(f"  transient '{exc}' -> retrying ({transient_retries}/6)",
                      file=sys.stderr)
            time.sleep(2 * transient_retries)
            # a fresh session that keeps refusing is probably stale; roll a new one
            if manager.can_rotate and transient_retries % 3 == 0:
                manager.rotate()
            continue

        i += 1
        if not quiet:
            print(f"  [{i}/{batches}] {info['model']} "
                  f"(mdl={model}, batch={info['batch']}, aspect={info['aspect']}"
                  + (", with reference" if bcid else "") + ")",
                  file=sys.stderr)
        if info.get("items"):
            batch = info["items"]          # bing answered fully inline
        else:
            batch = poll(manager, info["polling_url"], timeout=timeout)
        for it in batch:
            it.setdefault("request_id", info["request_id"])
        items.extend(batch)
        # pace requests: hammering back-to-back earns 'requestmissed' throttling
        if i < batches:
            time.sleep(1.5)
    return items


def download(manager: SessionManager, url: str, dest_dir: Path, index: int, stem: str,
             attempts: int = 3) -> Path:
    """Fetch one image, retrying transient th.bing.com hiccups."""
    last: Exception = RuntimeError("no attempt made")
    for n in range(attempts):
        try:
            resp = manager.http.get(url, timeout=120)
            resp.raise_for_status()
            if not resp.content:
                raise RuntimeError("empty body")
            ctype = (resp.headers.get("content-type") or "").lower()
            ext = "jpg" if ("jpeg" in ctype or "jpg" in ctype) else "png"
            dest_dir.mkdir(parents=True, exist_ok=True)
            path = dest_dir / f"{stem}_{index:02d}.{ext}"
            path.write_bytes(resp.content)
            return path
        except Exception as exc:
            last = exc
            if n < attempts - 1:
                time.sleep(1.5 * (n + 1))
    raise last


def slugify(text: str, limit: int = 48) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", text.lower()).strip("-")
    return slug[:limit].rstrip("-") or "image"


# --------------------------------------------------------------------------- #
# mode selection
# --------------------------------------------------------------------------- #
def ask_mode(cookies_available: bool) -> str:
    """Interactively ask which auth mode to use. Falls back to fresh if not a TTY."""
    if not sys.stdin.isatty():
        return "fresh" if not cookies_available else "cookie"

    print("\nHow should I generate?", file=sys.stderr)
    if cookies_available:
        print("  1) Use my cookies   - your signed-in account (persistent daily tokens)",
              file=sys.stderr)
        print("  2) Fresh sessions   - anonymous, auto-rotates when quota runs out  [default]",
              file=sys.stderr)
    else:
        print("  2) Fresh sessions   - anonymous, auto-rotates when quota runs out  [default]",
              file=sys.stderr)
        print("     (no cookies file found, so cookie mode is unavailable)", file=sys.stderr)

    while True:
        try:
            answer = input("  choose [1/2]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return "fresh"
        if answer in ("", "2", "f", "fresh"):
            return "fresh"
        if answer in ("1", "c", "cookie"):
            if cookies_available:
                return "cookie"
            print("  no cookies file available - pick 2", file=sys.stderr)
            continue
        print("  please enter 1 or 2", file=sys.stderr)


# --------------------------------------------------------------------------- #
# cli
# --------------------------------------------------------------------------- #
def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="mai_image.py",
        description="Bing Image Creator. Works with your cookies, or with fresh "
                    "anonymous sessions that need no cookies at all.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "modes:\n"
            "  --ask     prompt me which mode to use (default when interactive)\n"
            "  --fresh   force anonymous rotating sessions (no cookies needed)\n"
            "  --cookie  force my signed-in cookies\n\n"
            "models: mai-image-2 (default), mai-image-2.5, mai-image-1, dalle3, gpt4o\n"
            "aspect: 1:1, 3:2, 2:3 (7:4, 4:7 also on dalle3)\n"
            "ref   : -r IMAGE for image-to-image (upload works in both modes)\n\n"
            'example: python mai_image.py "a lighthouse at dusk" -a 3:2\n'
            'example: python mai_image.py "a cat" --fresh -n 3\n'
            'example: python mai_image.py "make it green" -r apple.jpg'
        ),
    )
    ap.add_argument("prompt", nargs="*", help="image prompt (quote it)")

    g = ap.add_mutually_exclusive_group()
    g.add_argument("--ask", action="store_true", help="ask which mode to use")
    g.add_argument("--fresh", action="store_true", help="force anonymous rotating sessions")
    g.add_argument("--cookie", action="store_true", help="force cookie mode")

    ap.add_argument("-m", "--model", default=DEFAULT_MODEL, help=f"default: {DEFAULT_MODEL}")
    ap.add_argument("-a", "--aspect", default="1:1", help="1:1 | 3:2 | 2:3")
    ap.add_argument("-s", "--size", help="size alias e.g. 1024x1024 (overrides -a)")
    ap.add_argument("-n", "--batches", type=int, default=1,
                    help="number of requests (4 images each on the current backend)")
    ap.add_argument("-o", "--out", help="output folder (default: ./output)")
    ap.add_argument("-c", "--cookies", help=f"cookies json path (env: {COOKIE_ENV})")
    ap.add_argument(
        "-r", "--ref", action="append", metavar="IMAGE",
        help="reference image for image-to-image (repeatable for a clear error; "
             "bing accepts only ONE)",
    )
    ap.add_argument("-t", "--timeout", type=int, default=300, help="per-request poll timeout (s)")
    ap.add_argument("--max-rotations", type=int, default=10,
                    help="cap on fresh-session rotations per run")
    ap.add_argument("--url-only", action="store_true", help="print URLs, skip downloading")
    ap.add_argument("--json", action="store_true", help="emit JSON result on stdout")
    ap.add_argument("--check", action="store_true", help="report auth/quota state and exit")
    args = ap.parse_args(argv)

    prompt = " ".join(args.prompt).strip()
    if not prompt and not args.check:
        ap.error('a prompt is required, e.g.: python mai_image.py "a cat riding a bicycle"')

    cookies_available = find_cookies_file(args.cookies) is not None

    # -- mode -------------------------------------------------------------- #
    if args.fresh:
        mode = "fresh"
    elif args.cookie:
        mode = "cookie"
    elif args.ask:
        mode = ask_mode(cookies_available)
    elif not cookies_available:
        mode = "fresh"
    else:
        # cookies exist and the user did not say which they want -> ask
        mode = ask_mode(cookies_available)

    try:
        manager = SessionManager(mode, None if mode == "fresh" else args.cookies)
    except Exception as exc:
        print(f"error : {exc}", file=sys.stderr)
        return 2

    if args.check:
        info = AuthProbe(manager)
        print(json.dumps(info, indent=2))
        return 0 if not (info.get("signed_in") is False and mode == "cookie") else 2

    key, mdl, aspects, _, accepts_ref = resolve_model(args.model)
    aspect = resolve_aspect(args.aspect, aspects, args.size)
    out_dir = Path(args.out) if args.out else Path.cwd() / "output"

    if args.ref and len(args.ref) > MAX_REFERENCES:
        print(
            f"error : bing accepts only {MAX_REFERENCES} reference image, but "
            f"{len(args.ref)} were given ({', '.join(args.ref)}).\n"
            "        A second reference is either dropped or refuses the whole request,\n"
            "        so it is rejected here instead of silently doing the wrong thing.\n"
            "        Combine the images into one file first, or use a provider that\n"
            "        supports multiple references.",
            file=sys.stderr,
        )
        return 2

    if args.ref and not accepts_ref:
        print(f"warning: {key} is not marked upload-capable by bing's registry; "
              "the reference may be ignored.", file=sys.stderr)

    if not args.json:
        print(f"backend : {BACKEND}", file=sys.stderr)
        print(f"mode    : {manager.describe()}", file=sys.stderr)
        print(f"model   : {args.model} (mdl={mdl})", file=sys.stderr)
        print(f"aspect  : {aspect}", file=sys.stderr)

    bcid: Optional[str] = None
    if args.ref:
        ref_path = Path(args.ref[0]).expanduser()
        try:
            bcid = upload_reference(manager, ref_path)
        except Exception as exc:
            print(f"error : {exc}", file=sys.stderr)
            return 1
        if not args.json:
            print(f"ref     : {ref_path} -> bcid={bcid}", file=sys.stderr)

    try:
        images = generate(
            manager, prompt, model=args.model, aspect=aspect, batches=args.batches,
            timeout=args.timeout, quiet=args.json, bcid=bcid,
            max_rotations=args.max_rotations,
        )
    except Exception as exc:
        print(f"error : {exc}", file=sys.stderr)
        return 1

    results: List[Dict[str, Any]] = []
    stem = slugify(prompt)
    for i, item in enumerate(images, start=1):
        url = item.get("src") or item.get("url") or ""
        if not url:
            continue
        entry: Dict[str, Any] = {
            "index": i, "url": url, "width": item.get("w"), "height": item.get("h"),
            "thid": item.get("thid"), "request_id": item.get("request_id"),
        }
        if not args.url_only:
            try:
                entry["file"] = str(download(manager, url, out_dir, i, stem))
            except Exception as exc:
                entry["download_error"] = str(exc)
        results.append(entry)

    if args.json:
        print(json.dumps({"mode": manager.mode, "rotations": manager.rotations,
                          "images": results}, indent=2))
    else:
        print(f"\n{len(results)} image(s) via {manager.mode} mode"
              + (f" ({manager.rotations} session rotation(s))" if manager.rotations else "")
              + ":", file=sys.stderr)
        for r in results:
            print(f"  {r.get('file') or r['url']}", file=sys.stderr)

    return 0 if results else 1


def AuthProbe(manager: SessionManager) -> Dict[str, Any]:  # noqa: N802
    return manager.auth_state()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:  # pragma: no cover
        sys.exit(0)
