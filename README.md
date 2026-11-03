# Load Image (RAM) for ComfyUI

A custom ComfyUI node that loads an image from a **URL**, a **file picker**, or a
**clipboard paste**, and keeps the image bytes **only in server RAM**.

The node's own code never writes image bytes to disk: no `input/`, no `temp/`,
no `output/`, no temp files. It contains no `open(path, "wb")`, `.save(`,
`tempfile`, `shutil`, or `folder_paths` write calls.

- Node class: `LoadImageRam`
- Display name: **Load Image (RAM)**
- Outputs: `IMAGE`, `MASK`
- No extra Python dependencies beyond what ComfyUI already ships (torch,
  Pillow, numpy, requests, aiohttp)

## Install

Copy this folder (the one containing `__init__.py`) into:

```
ComfyUI/custom_nodes/ComfyLoadImgFromURL/
```

Restart ComfyUI. The node appears under `image` → **Load Image (RAM)**.

## Usage

| Source | How |
|---|---|
| File picker | Click **Choose image…** on the node and pick a file. The browser uploads the raw bytes to `POST /ramimage/upload`; the server returns a random token. |
| Clipboard | Select the node on the canvas, copy an image in another app, press **Ctrl+V**. |
| URL | Type an `http`/`https` URL into the `url` field and leave `token` empty. |

Then queue the prompt as usual.

- `consume` (default `true`) removes the RAM entry after the node first
  executes. ComfyUI's output cache lets you queue again with the same tensor;
  if you clear the cache or restart, the token is gone and the node asks you to
  re-upload.
- The workflow JSON contains only the random token (about 22 characters) — never
  the image, never base64.
- A token saved in a workflow becomes invalid after it is consumed, after the
  TTL, or after a ComfyUI restart. Pick/paste the image again.
- The browser preview uses a local `blob:` URL; the image is not sent back from
  the server for display.

## Limits

| Limit | Default | Where |
|---|---:|---|
| Per-image bytes | 50 MiB | `MAX_BYTES_PER_IMAGE` in `ram_store.py` |
| Total store bytes | 500 MiB | `MAX_TOTAL_BYTES` in `ram_store.py` |
| Store TTL | 3600 s | `TTL_SECONDS` in `ram_store.py` |
| Decompression-bomb guard | Pillow's `Image.MAX_IMAGE_PIXELS` (89,478,485 fallback) | `__init__.py` |
| URL redirects | 5 | `REDIRECT_LIMIT` in `url_fetch.py` |
| URL connect/read timeout | 5 s / 30 s | `url_fetch.py` |

When the total cap would be exceeded, the oldest entries are evicted first.
Expired entries are purged on every store operation. Only the first frame of an
animated image is used.

## How the "no disk" guarantee is implemented

1. **Upload route** (`POST /ramimage/upload`): the body is the raw image bytes
   (`fetch(..., {post body: blob})`). The handler reads
   `request.content.iter_chunked(...)` directly, never `request.post()` or a
   multipart file part, so no parser can spool a file. `Content-Length` and the
   streamed byte count are checked against the per-image cap.
2. **URL fetch**: `requests` with `stream=True`; chunks are appended to a
   `bytearray`, aborting above the cap. The response is never handed a file
   object.
3. **Store**: a bounded, lock-protected `dict` of token → `bytes` in
   `ram_store.py`.
4. **Node**: `Image.open(BytesIO(data))` → PIL → numpy → torch. No path is ever
   passed to Pillow.
5. **Frontend**: the file picker and paste handler only read the user's own
   clipboard/device and POST a blob; the preview is a browser object URL.

The only files this package itself ships are code and documentation. Python may
create `__pycache__/*.pyc` bytecode for the package on first import, as it does
for any Python module; those files never contain image bytes.

## Hardening outside the node

These are outside the node's control but matter if "nothing on disk" must hold
for the whole machine:

- Reverse proxy: set `proxy_request_buffering off;` so the proxy does not buffer
  the upload body to a temp file, and raise `client_max_body_size` above the
  per-image cap (50 MiB by default). Better: connect to ComfyUI directly.
- Swap: disable it, or use encrypted swap.
- Hibernation: disable it.
- Core dumps: `ulimit -c 0` or the `systemd`/`sysctl` equivalent.
- Containers: read-only root filesystem plus `tmpfs` mounts for `/tmp`.
- Do not attach **Preview Image** or **Save Image** downstream if you do not
  want a file written. Those nodes write by design.
- If ComfyUI is remote, the upload travels over the network; use HTTPS.

## URL safety

`url_fetch.py` enforces:

- only `http` and `https` schemes;
- a best-effort SSRF guard: the hostname is resolved and every resolved address
  must be globally routable (private, loopback, link-local, reserved, multicast,
  and unspecified addresses are rejected);
- redirects are followed manually, at most 5, and every hop is re-validated;
- streamed size cap and timeouts.

Set `RAMIMAGE_ALLOW_PRIVATE=1` in the ComfyUI environment to allow private /
loopback addresses (development and local image servers only). The guard is
best-effort: DNS rebinding and HTTP proxies can defeat address checks.

## Known limitations

- RAM-only means "this node never stores the image as a file", not "nobody can
  read it". The server's owner, root, or anyone with memory access can inspect
  it, and Python does not zero freed buffers.
- The OS may swap memory; see hardening above.
- ComfyUI keeps the decoded output tensor in its own in-memory cache.
- A workflow token is a capability: while it is alive, anyone who can queue
  prompts on the same ComfyUI process could use it. It is random and single-use,
  and worthless on another machine or after a restart.
- The RAM store is per-process. Multi-worker/proxy setups that route different
  requests to different processes are not supported.
- The original file remains on the user's device, and the browser necessarily
  sees the image it picked or pasted.
- URL fetching decodes only the first frame of animated images.
- Changing a URL's content without changing the URL string will not re-run the
  node because `IS_CHANGED` keys on the token + URL; change the URL or use a
  token to force a reload.

## File layout

```
ComfyLoadImgFromURL/
├── __init__.py       # node, decode, upload route, registrations
├── ram_store.py      # bounded in-RAM token store
├── url_fetch.py      # streamed, SSRF-guarded URL download
├── web/
│   └── ramimage.js   # file picker, paste, status, client-side preview
└── README.md
```
