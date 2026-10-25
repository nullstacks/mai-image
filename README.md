# MAI-Image

**Free image generation via Bing Image Creator — no API key, no billing, works with or without cookies.**

An agent skill plus a standalone Python CLI that talks directly to Bing's own
image-creator backend (the one behind `bing.com/images/create`). It runs in two
modes:

- **fresh** — no cookies at all. Anonymous sessions each carry a small quota and are
  rotated automatically when they run dry.
- **cookie** — your signed-in bing.com account, using its daily image tokens.

Both produce the same images from the same model. The script is a single file with
no dependency on any SDK; `curl_cffi` is recommended, `requests` is the fallback.

```
pip install curl_cffi pillow
python scripts/mai_image.py "a lighthouse on a cliff at dusk" --fresh
```

---

## What it does

| | |
|---|---|
| **Default model** | `mai-image-2.5-flash` — what Bing actually serves today |
| **Images per request** | 4 (text-to-image), 1 when using `--ref` |
| **Aspect ratios** | `1:1`, `3:2`, `2:3` (DALL-E 3 ids also accept `7:4`, `4:7`) |
| **Reference image** | yes — exactly **one** per request |
| **Fresh session budget** | ~5 requests ≈ **20 images**, then auto-rotate |
| **Output** | 1024×1024, 1248×832, or 832×1248 JPG/PNG |

## Install

**As an agent skill** — drop the folder into your skills directory:

```
~/.agents/skills/mai-image/      (or wherever your harness loads skills from)
```

The `SKILL.md` frontmatter is what makes it discoverable.

**As a plain CLI** — copy `scripts/mai_image.py` anywhere and run it with Python 3.9+:

```bash
pip install curl_cffi pillow
python mai_image.py "a paper boat on a puddle" --fresh
```

## Usage

```bash
# fresh mode: no cookies needed, rotates sessions when quota runs out
python scripts/mai_image.py "a brass lantern on a wet ledge" --fresh

# cookie mode: uses your account's daily tokens
python scripts/mai_image.py "a brass lantern on a wet ledge" --cookie

# let it ask you
python scripts/mai_image.py "a brass lantern on a wet ledge" --ask

# batch -- each request returns 4 images, so this yields 24
python scripts/mai_image.py "neon ramen shop in the rain" --fresh -n 6 -o renders

# landscape, and just print URLs
python scripts/mai_image.py "wide desert highway at noon" -a 3:2 --url-only

# image-to-image with one reference
python scripts/mai_image.py "restyle this as polished chrome" --fresh -r jug.jpg

# check what the account state looks like
python scripts/mai_image.py --check --cookie
```

| Flag | Meaning |
|---|---|
| `--fresh` / `--cookie` / `--ask` | auth mode (mutually exclusive; asks if none given) |
| `-m, --model` | `mai-image-2.5-flash` (default), `mai-image-2`, `mai-image-1`, `dalle3`, `gpt4o` |
| `-a, --aspect` | `1:1` (default), `3:2`, `2:3` |
| `-s, --size` | size alias, e.g. `1024x1536` (overrides `-a`) |
| `-n, --batches` | number of requests |
| `-o, --out` | output folder (default `./output`) |
| `-r, --ref` | one reference image for image-to-image |
| `-t, --timeout` | per-request poll timeout (default 300 s) |
| `--json` | machine-readable result on stdout |
| `--url-only` | skip downloading |
| `--check` | report auth/quota state and exit |
| `--max-rotations` | cap on fresh-session rotations per run |

## Cookies (cookie mode only)

Export cookies from a **signed-in** bing.com tab. The file must include `_U` and
`SUID`, otherwise Bing treats you as an anonymous visitor.

Accepted formats:

```json
[{ "name": "_U", "value": "...", "domain": ".bing.com", "path": "/" }]
```
```json
{ "_U": "...", "SUID": "..." }
```

Lookup order: `--cookies PATH` → `$MAI_COOKIES_JSON` → `<skill root>/cookies.json`
→ `./cookies.json`. Never commit this file — `.gitignore` already covers it.

## Notes from reverse engineering

These are the findings that shaped the script; they are documented because the
endpoint is undocumented and changes without notice.

- **`mdl` is currently ignored.** Every value — `0`, `1`, `4`, `7`, `10`, `12`, even
  a bogus `999` — renders on `MAI-Image-2.5-Flash` with 4 images. Model choice is
  server-side. The script still sends the documented id and always prints the
  name the server reports back.
- **One reference image only.** The page's file input has no `multiple`; posting two
  `imageBase64` parts returns a single `bcid` matching the *first* image (the second
  is silently dropped), and two `bcid` params in submit are rejected outright. A
  second `--ref` is refused locally rather than silently ignored.
- **A reference is genuinely honored.** A terracotta jug with lavender restyled to
  "polished chrome metal" kept the jug silhouette, the lavender arrangement, and the
  table; the same prompt without `--ref` produced an unrelated chrome form.
- **Results may arrive inline.** When `data-status="2"`, the images are already in
  the `data-results` attribute and `data-polling-url` is empty. Parse attributes
  *before* HTML-unescaping, or the embedded JSON breaks.
- **Cold sessions refuse.** Posting to a session that never fetched the creator page
  returns `requestmissed`; a warm-up GET also seeds `_SS` (needed for uploads).
- **IP-level throttling.** Rotating many sessions back-to-back starts refusing
  everything for a minute. Pacing plus a small retry budget handles it.

### Endpoints

```
GET  /images/create/ai-image-generator            warm-up: seeds MUID / _SS

POST /images/create/ai-image-generator
       ?FORM=GENCRE&q=<prompt>&ctype=image&mdl=<id>&ar=<1-5>&rt=<3|4>
       [&bcid=<upload id>&edt=1]
  -> HTML; read <aigc-results-view> data-status / data-request-id /
     data-polling-url / data-results
     (data-status: 0|1 working, 2 done, 3 refused)

POST /images/create/upload?&sid=<SID>
       multipart imageBase64 = <base64 jpeg>    -> {"bcid":"r.B.XXXX","error":null}

GET  /images/create/ai-image-generator/async/results/<requestId>
  -> {"status":0|1} … {"status":2,"records":[{"mediaItems":[{"src":...}]}]}
```

`ar`: 1:1→1, 7:4→2, 4:7→3, 3:2→4, 2:3→5. `rt`: 3 anonymous, 4 with paid tokens.

## Disclaimer

Unofficial and reverse-engineered. Not affiliated with Microsoft or Bing. This is
for personal and educational use; you are responsible for complying with Bing's
terms of service, and the endpoint may change or block you at any time.

## License

MIT — see [LICENSE](LICENSE).
