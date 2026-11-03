# Plan: RAM-Only "Load Image" Node for ComfyUI

A custom ComfyUI node that loads an image from a **URL**, a **local file picker**, or a **clipboard paste**, and holds it only in server RAM. The node never writes the image to disk as a file (no `input/`, no `temp/`, no `output/`).

---

## 1. Goals and non-goals

### Goals
- Load an image from three sources: URL, file picker (upload from the user's device), clipboard paste.
- The node's own code never creates an image file on the server.
- Output the standard ComfyUI types: `IMAGE` and `MASK`, so it works with any downstream node.
- Imaged data is removed from the node's own store as soon as possible (on first use, or after a short TTL).

### Non-goals
- Hiding the image from the server's owner or anyone with root or memory-dump access. RAM is not private from them.
- Controlling what *other* nodes do (Preview Image and Save Image will write files if the user adds them).
- Guaranteeing that swap, hibernation, crash dumps, or proxy buffers never touch disk (see section 8 for hardening).
- Hiding the URL or the connection from network logs.

### Success criteria
- Running the node with each of the three sources, with no Preview/Save node attached, creates **no new files** anywhere on the server (verified with `strace` / `inotifywait`, section 9).
- Works across ComfyUI restarts without crashing (a stale token gives a clear error).

---

## 2. Threat model (what "no trace" means here)

| Concern | In scope? | Handling |
|---|---|---|
| Node writes the image as a file | Yes | Never call `save`, `open(..., "wb")`, `tempfile`, or `folder_paths` write paths |
| aiohttp spooling uploads to a temp file | Yes | Read the raw body with `request.read()`; do not use `request.post()` or multipart |
| Image bytes lingering in the node's store | Yes | Pop on use, TTL, size cap, LRU eviction |
| Image in workflow JSON or prompt history | Yes | Store only a random token in the workflow, never the image or base64 |
| Reverse proxy buffering request bodies | Partly | Documented hardening (section 8) |
| Swap, hibernation, core dumps | Partly | Documented hardening (section 8) |
| Other nodes saving or previewing the image | No | User responsibility |
| Root user or memory inspection | No | Not solvable on a machine you don't control |

---

## 3. Architecture

```
 Browser (user's device)                 ComfyUI server
 ┌──────────────────────┐                ┌───────────────────────────────┐
 │ File picker / Paste  │  raw bytes     │ POST /ramimage/upload         │
 │  (JS extension)      │ ─────────────▶ │  → RAM store {token: bytes}   │
 │  stores token in     │ ◀───────────── │  ← returns random token       │
 │  hidden widget       │     token      │                               │
 └──────────────────────┘                │ Node "LoadImageRam"           │
                                         │  token → bytes → PIL → tensor │
 URL source ───────────────────────────▶ │  url → requests (stream, cap) │
                                         │       → BytesIO → PIL → tensor│
                                         └───────────────────────────────┘
```

Three parts:
1. **Server route** (`/ramimage/upload`) that accepts raw image bytes, stores them in a RAM dict, and returns a token.
2. **Node** (`LoadImageRam`) that resolves a token or a URL to bytes, decodes with PIL, and returns tensors.
3. **Frontend extension** that adds a file-picker button, a paste handler, and a status display to the node.

---

## 4. File layout

```
ComfyUI/custom_nodes/comfyui_ram_image/
├── __init__.py          # node, store, route, NODE_CLASS_MAPPINGS, WEB_DIRECTORY
├── ram_store.py         # thread-safe store with TTL, size cap, LRU
├── url_fetch.py         # safe URL download (scheme check, SSRF guard, size cap)
├── web/
│   └── ramimage.js      # frontend extension
└── README.md
```

Splitting `ram_store.py` and `url_fetch.py` out of `__init__.py` keeps each piece small enough to audit for any disk-writing call.

---

## 5. Component design

### 5.1 RAM store (`ram_store.py`)
- Data: `OrderedDict[token -> (timestamp, bytes)]` guarded by a `threading.Lock`.
- Operations: `put(data) -> token`, `pop(token) -> bytes | None`, `peek(token)`, `purge()`.
- Limits (configurable constants):
  - `MAX_BYTES_PER_IMAGE = 50 MB`
  - `MAX_TOTAL_BYTES = 500 MB`
  - `TTL_SECONDS = 3600`
- Eviction: on every `put`, purge expired entries, then evict oldest entries until the total is under `MAX_TOTAL_BYTES`.
- Tokens: `secrets.token_urlsafe(16)`.

### 5.2 Upload route
- `POST /ramimage/upload` registered on `PromptServer.instance.routes`.
- Body is the raw image bytes (sent as `fetch(..., {method: "POST", body: blob})`).
- Read with `await request.read()`. **Do not** use `request.post()` or `request.multipart()` file parts, because aiohttp spools those to a temp file.
- Reject empty bodies and bodies above the per-image cap (also check `Content-Length` early, before reading).
- Quick validation: attempt `Image.open(BytesIO(data)).verify()` so garbage is rejected at upload time with a clear error.
- Response: `{"token": "..."}`.

### 5.3 URL fetch (`url_fetch.py`)
- Allow only `http` and `https`.
- Resolve the hostname and reject private, loopback, link-local, and reserved IP ranges (SSRF guard), unless an explicit `ALLOW_PRIVATE=True` flag is set.
- Use `requests.get(url, stream=True, timeout=(5, 30), allow_redirects=True)` with a redirect limit, then re-check the final host.
- Read in chunks into a `BytesIO`, aborting once `MAX_BYTES_PER_IMAGE` is exceeded.
- Check the `Content-Type` starts with `image/` (soft check; PIL is the real validator).
- `Image.MAX_IMAGE_PIXELS` kept at a sane value to block decompression bombs.

### 5.4 Node (`LoadImageRam`)
- Inputs:
  - `token` (STRING, filled by the frontend, hidden or read-only)
  - `url` (STRING, optional)
  - `consume` (BOOLEAN, default `True`): if true, the token is removed from the store after first use.
- Outputs: `IMAGE`, `MASK`.
- Source priority: `token` first, then `url`, otherwise raise a clear error.
- Decode: `Image.open(BytesIO(data))`, `ImageOps.exif_transpose`, convert to RGBA, float32 / 255, `IMAGE = rgb[None]`, `MASK = 1 - alpha[None]`.
- Handle animated or multi-frame images by taking the first frame (v1).
- `IS_CHANGED` returns `token + url`, so ComfyUI re-runs the node only when the source changes. If `consume=True` and the same token is re-run from cache, ComfyUI reuses the cached tensor and never calls `load` again; a genuinely new run needs a new upload.
- Errors: a missing or expired token gives `"Image expired or already used; re-upload or re-paste it."`

### 5.5 Frontend extension (`web/ramimage.js`)
- Registered with `app.registerExtension`, acting only on nodes where `comfyClass === "LoadImageRam"`.
- **File picker:** a button widget that creates a hidden `<input type="file" accept="image/*">`, uploads the selected file's blob, and writes the token into the `token` widget.
- **Paste:** a `paste` event listener on `document` that acts only when this node is selected; takes the first `image/*` item from `clipboardData`, uploads it, and sets the token.
- **Status:** a read-only widget showing `empty`, `uploading…`, or `ready (123 KB)`, so the user can see the state.
- **Client-side preview (optional):** `URL.createObjectURL(blob)` drawn on the node, so the server never sends the image back. Revoke the object URL when replaced or when the node is removed.
- **Cleanup:** remove the paste listener in `node.onRemoved`.
- **Behavior on workflow load:** a token loaded from a saved workflow may be stale, so the status widget shows `unknown`, and the node's server-side error handles the rest.

---

## 6. Security checklist

- [ ] No `open(..., "w"/"wb")`, `.save(`, `tempfile`, or `shutil` calls anywhere in the node package (grep before release).
- [ ] Upload route reads the raw body only; no multipart.
- [ ] Per-image and total size caps enforced.
- [ ] URL: scheme allow-list, SSRF guard, redirect limit, streamed size cap, timeouts.
- [ ] Decompression-bomb protection via `MAX_IMAGE_PIXELS`.
- [ ] Tokens are random, unguessable, and never logged.
- [ ] The image or its base64 is never placed in a widget value (it would be saved in the workflow and history).
- [ ] Image bytes are never logged or printed.
- [ ] `consume=True` is the default.

---

## 7. Implementation milestones

1. **Skeleton:** package, `NODE_CLASS_MAPPINGS`, node returns a dummy tensor, loads in ComfyUI.
2. **URL source:** `url_fetch.py` plus node URL path; test with PNG, JPG, WebP, and an image with alpha.
3. **RAM store and upload route:** test with `curl --data-binary @test.png localhost:8188/ramimage/upload`.
4. **Node token path:** decode from the store, pop on use, clear errors.
5. **Frontend:** file-picker button, token widget, status.
6. **Paste support:** paste listener, selected-node check, cleanup on removal.
7. **Hardening and limits:** caps, LRU eviction, SSRF guard, decompression-bomb limits.
8. **Verification:** the strace/inotify tests in section 9.
9. **README:** document behavior, limits, and the hardening notes below.

---

## 8. Deployment hardening (optional but recommended)

These cover traces from outside the node:

- **Reverse proxy:** if ComfyUI is behind nginx, set `proxy_request_buffering off;` (and check that large `client_body_buffer_size` values don't cause spooling). Or run ComfyUI directly without a proxy.
- **Swap:** disable swap, or use encrypted swap.
- **Hibernation:** disable it.
- **Core dumps:** `ulimit -c 0`, or disable them via `systemd` / `sysctl`.
- **Container option:** run ComfyUI with a read-only root filesystem and `tmpfs` mounts for `/tmp`, so any stray write goes to RAM.
- **Workflow hygiene:** do not attach Preview Image or Save Image nodes to this node's output if no file is wanted.

---

## 9. Verification plan

Run ComfyUI under file-activity monitoring while exercising each source:

1. **Watch for new files:** `inotifywait -m -r -e create,modify,moved_to ComfyUI/ /tmp/` while loading an image via URL, file picker, and paste, with no Preview/Save node connected. Expected: no image-sized files created.
2. **Trace syscalls:** `strace -f -e trace=openat,write -p <comfyui_pid>` and confirm there is no `openat` with write flags on image-like paths during a load.
3. **Check directories:** compare `input/`, `output/`, `temp/` and the system temp directory before and after.
4. **Behavior tests:**
   - Token pop: second run with `consume=True` and a cleared cache gives the "expired or already used" error.
   - TTL: an entry expires after `TTL_SECONDS`.
   - Cap: uploading above `MAX_BYTES_PER_IMAGE` returns 400.
   - Total cap: filling the store evicts the oldest entries.
   - Restart: a saved workflow with a stale token shows a clear error.
5. **Negative test:** attach a Save Image node and confirm it *does* write a file, which shows the monitoring would catch writes.
6. **SSRF test:** `http://127.0.0.1:8188/` and `http://169.254.169.254/` are rejected.

---

## 10. Known limitations (to state in the README)

- RAM-only means "never stored as a file by this node", not "unreadable by the server's owner".
- Python does not zero freed buffers, so bytes may linger in memory briefly.
- ComfyUI may keep the decoded output tensor in its in-memory cache after execution.
- A token in a saved workflow becomes invalid after a restart or expiry; the user must re-upload.
- Animated images load only their first frame in v1.
- The browser sees the image (it came from the user's device or the clipboard), and the original file remains on the user's own device.

---

## 11. Possible later extensions

- Batch loading (multiple images, `IMAGE` batch output).
- Multi-frame / animated image support.
- A "Clear RAM store" button or node.
- Configurable limits through a small settings file or environment variables.
- Optional preview served from RAM via a tokenized `GET` route.
