---
name: mai-image
description: Generate images with Bing Image Creator (bing.com/images/create) by running a bundled Python script. Works in two modes - "cookie" using the user's signed-in bing.com cookies, or "fresh" with no cookies at all (anonymous sessions that rotate automatically when their quota runs out). Use this skill whenever the user asks to generate, create, draw, render, or produce an image, picture, illustration, photo, artwork, wallpaper, or concept art, and mentions MAI, MAI-Image, Bing Image Creator, or Microsoft Image Creator, or wants a free image generator needing no API key. Saves images to disk.
---

# MAI-Image generation (Bing Image Creator)

Generate images by running `scripts/mai_image.py`. It posts to Bing's own
image-creator endpoint, polls the async result (or reads results returned inline),
then downloads the finished images from `th.bing.com`.

## ALWAYS ASK FIRST: cookie mode or fresh mode?

**Before generating, ask the user which mode to use.** Do not silently pick one.
Run with `--ask`, or just ask in chat and pass `--cookie` / `--fresh`.

| | **Cookie mode** | **Fresh mode** |
|---|---|---|
| Needs | exported bing.com cookies (`_U`, `SUID`) | **nothing** |
| Quota | the account's daily allowance (e.g. 14 tokens) | ~5 requests = **20 images per session**, then rotate |
| Persistence | same account, history saved to their profile | anonymous, ephemeral, nothing saved to any account |
| Rotation | never (would burn the same account) | automatic, unlimited within `--max-rotations` |
| Best for | lots of images, keeping a history, the user's own account | quick one-offs, or when cookies are missing/expired |

Both modes produce identical image quality — it is the same backend model.

```bash
python scripts/mai_image.py "<prompt>" --ask              # interactive picker
python scripts/mai_image.py "<prompt>" --fresh -n 3       # no cookies, auto-rotate
python scripts/mai_image.py "<prompt>" --cookie           # use their account
```

When invoked non-interactively, the script asks via `--ask` only if a TTY is
available; otherwise pass the flag explicitly. If no cookies file exists,
`--cookie` fails fast with the searched paths and fresh mode still works.

## Models — and the big caveat

| Flag | `mdl` sent | Registry batch | Reference image |
|---|---|---|---|
| `mai-image-2.5-flash` (**default**) | `10` | 4 | **yes** |
| `mai-image-2` | `7` | 4 | no |
| `mai-image-2.5` | `10` | 1 | **yes** |
| `mai-image-1` | `4` | 1 | no |
| `dalle3` | `0` | 1 | no |
| `gpt4o` | `1` | 1 | **yes** |

Aliases resolve too: `flash` / `mai-flash` / `mai-image-2.5flash` →
`mai-image-2.5-flash`, plus `maiimage2`, `maiimage1`, `dalle`, `gpt4`.

**Verified 2026-10-25: Bing currently ignores `mdl` entirely.** Every value —
`0`, `1`, `4`, `7`, `10`, `12`, even a bogus `999` — renders on
**`MAI-Image-2.5-Flash` with 4 images per request**, in both modes. The model is
chosen server-side, not by this parameter.

That is precisely why the default is `mai-image-2.5-flash`: it is the model Bing
actually serves, so the flag name now matches reality instead of promising DALL-E 3
that never renders.

`-m` still validates against the list and still sends the documented id (so the
script is correct if Bing re-enables selection), but **do not promise the user a
specific model**. Quote the server-reported name that the script prints. Never tell
the user you generated with DALL-E 3 or GPT-4o — you almost certainly did not.

The ids come from `GenerativeMediaRegistry.GmRegistry` in the page. They are
Bing-internal and shift on redeploy; re-read that blob if names drift.

## Reference images (image-to-image)

`-r/--ref PATH` uploads **one** local image and conditions the generation on it.
Works in **both** modes (upload is quota-independent), and the default model
supports it.

```bash
python scripts/mai_image.py "make the car red" --cookie -r photo.jpg
python scripts/mai_image.py "turn this into watercolor" --fresh -r photo.jpg
```

### Only ONE reference image — this is a hard Bing limit

Verified 2026-10-25 by testing every plausible path. **Bing does not support
multi-image reference.** Do not promise the user a collage/combine workflow, and do
not attempt to work around it.

Evidence:

- The page's only file input is
  `<input type="file" accept=".jpg,.jpeg,.png,.gif,.webp">` — **no `multiple`**.
- `attachImageInternal()` calls `unattachImageInternal()` first, and the state field
  is a singular `p.attachment`; there is no `attachments[]` anywhere in the bundle.
- Uploading two `imageBase64` parts in one request returns a **single `bcid`, and it
  is the first image only**. The second is silently dropped — proven by swapping the
  order and watching the returned bcid change to match whichever came first.
- Sending two `bcid` params in submit is **rejected with `data-status="3"`** (no
  polling url, no generation). It is not merged into a multi-reference request.

So the count is **1**, and extra references must be declined rather than silently
ignored. If the user asks for two source images, say plainly that Bing accepts one
and offer the alternatives: combine them yourself first into a single image, or do
the merge via a different provider (e.g. a FLUX/Gemini skill) that accepts multiple
references.

**Verified 2026-10-25 that a single reference is genuinely honored.** A terracotta
jug with lavender restyled to "polished chrome metal" kept the exact jug silhouette,
the identical lavender arrangement, and the same table and backdrop — only the
material changed. The same prompt *without* `--ref` produced an unrelated chrome
form. Trust the feature, but still eyeball the output; an upload's HTTP 200 alone
proves nothing.

- **`--ref` returns 1 image, not 4.** Image-to-image runs a different path that
  yields a single result even though text-to-image batches 4. Do not set `-n 4`
  expecting 16.
- The registry marks only `mai-image-2.5` and `gpt4o` as upload-capable. Since
  `mdl` is ignored anyway, the request is accepted regardless, but the script warns
  when `-r` is used with a non-upload model.
- Requires Pillow. The image is resized to Bing's **360,000-pixel area** cap and
  re-encoded JPEG q80 before upload — the same thing the browser composer does.
  Accepted formats: `.jpg`, `.jpeg`, `.png`, `.gif`, `.webp`.
- Upload returns a `bcid`; submit then carries `bcid` + `edt=1`.

## Quota, rotation, and pacing

- A blank session sustains **~5 requests (20 images)**, then submit returns
  `nsiindivlimit`. The script detects that, discards the session, and continues on a
  fresh one — verified: 28 images across 7 batches with 1 rotation.
- Cookie mode does **not** rotate. When the account's tokens hit 0 the script stops
  and tells you; rotation would only burn the same account again.
- The script paces requests (~1.5 s) and retries transient `requestmissed`
  answers. Bing also throttles at the **IP** level: hammering many sessions back to
  back will start refusing everything for a minute or so. If you see repeated
  `requestmissed`, stop for a minute rather than looping.

## Steps

1. Ask: cookie mode or fresh mode?
2. Refine the prompt — subject, style, lighting, composition. Keep the user's intent.
3. Run it (`-a` defaults to `1:1`, `-o` defaults to `./output`):

```
python scripts/mai_image.py "<prompt>" [--cookie|--fresh] [-a 1:1|3:2|2:3]
    [-n BATCHES] [-o DIR] [-r REF] [--url-only] [--json]
```

   `-n` counts **requests**; the current backend returns 4 images per request, so
   `-n 3` gives 12 — **except with `-r`, which yields 1 image per request**.
   `--json` emits `{mode, rotations, images:[{url,width,height,thid,request_id,file}]}`.
4. Present the files. Mention the mode used, the server-reported model, and the
   rotation count if it is non-zero.

## Troubleshooting

- **`nsiindivlimit` in fresh mode** — expected; the script rotates automatically. If
  it cannot (hit `--max-rotations`), the IP is throttled — wait a minute.
- **`nsiindivlimit` in cookie mode** — the account's daily tokens are gone. Wait for
  the reset, or switch to `--fresh`.
- **`requestmissed` repeatedly** — IP-level throttling, not a code bug. It also
  appears harmlessly on successful viewers; only a missing polling URL *and* empty
  inline results means a real refusal.
- **`suspended` / `vagueprompt`** — prompt tripped moderation or was too vague.
  Reword; avoid real people, brands, violence.
- **`reference upload failed`** — the uploader wants `sid` from the `_SS` cookie;
  fresh mode gets `_SS` automatically on warm-up. Pillow is required.
- **User asks for two or more reference images** — impossible on this backend. Bing
  accepts exactly one; a second is dropped or the submit is refused. Tell them
  directly and offer to pre-composite the images into one file first.
- **`TimeoutError`** — raise `-t 600`. Generation is slower under load.
- **Sizes** — you do not choose pixels. Output is 1024×1024, 1248×832, or 832×1248
  depending on aspect. Ask for an aspect, not a size.

## Reference: endpoints

```
GET  /images/create/ai-image-generator          (warm-up: seeds MUID/_SS)

POST /images/create/ai-image-generator
       ?FORM=GENCRE&q=<prompt>&ctype=image&mdl=<id>&ar=<1-5>&rt=<3|4>
       [&bcid=<upload id>&edt=1]
  -> HTML. Parse <aigc-results-view ...>:
       data-status       0|1 = working, 2 = done, 3 = refused
       data-request-id   request id
       data-polling-url  empty when results came back inline
       data-results      HTML-escaped JSON {records:[{mediaItems:[...]}]}
     Parse attributes BEFORE unescaping &quot;, or the JSON attribute breaks.

POST /images/create/upload?&sid=<SID>
       multipart imageBase64 = <base64 jpeg>   -> {"bcid":"r.B.XXXX","error":null}

GET  /images/create/ai-image-generator/async/results/<requestId>
  -> {"status":0|1}  working
     {"status":2,"records":[{"mediaItems":[{"src":...}]}]}  done
```

`ar`: 1:1→1, 7:4→2, 4:7→3, 3:2→4, 2:3→5. `rt`: 3 anonymous, 4 with paid tokens.

## Notes

- The cookies file is a live credential. Never print cookie values, commit the file,
  or paste it into logs. The script reports only the path.
- Fresh mode stores nothing; it is the fallback when no cookies are configured.
- Unofficial, reverse-engineered endpoint. Bing changes it without notice — the
  registry blob and the `data-*` attributes are the contract to re-verify.
