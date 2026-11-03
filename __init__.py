"""ComfyUI node: load an image from the clipboard/file picker (via an
in-RAM upload) or from a URL, and decode it purely in memory.

The package never writes image bytes to disk: the upload route reads the raw
request body in chunks, the URL fetcher streams into a bytearray, the store
keeps bytes in a bounded in-process dict, and decoding uses ``BytesIO``.
"""

from __future__ import annotations

import logging
from io import BytesIO

import numpy as np
import torch
from PIL import Image, ImageOps

from .ram_store import (
    MAX_BYTES_PER_IMAGE,
    RAM_STORE,
    EmptyImage,
    RamStoreError,
)
from .url_fetch import fetch_image

NODE_CLASS = "LoadImageRam"
UPLOAD_ROUTE = "/ramimage/upload"
UPLOAD_CHUNK_SIZE = 64 * 1024

# Fallback if Pillow's own bomb limit has been disabled globally.
DEFAULT_MAX_IMAGE_PIXELS = 89_478_485

_log = logging.getLogger(__name__)


class ImageDecodeError(ValueError):
    """A decode failure whose message is safe to show in the ComfyUI UI."""


def decode_image_bytes(data: bytes) -> tuple[np.ndarray, np.ndarray]:
    """Decode image bytes into ComfyUI ``IMAGE`` / ``MASK`` numpy arrays.

    Returns ``(image, mask)`` with shapes ``(1, H, W, 3)`` and ``(1, H, W)``,
    both float32 in [0, 1].  Only the first frame of an animated image is used.
    """
    if not data:
        raise ImageDecodeError("The image data is empty.")

    try:
        with Image.open(BytesIO(data)) as im:
            _check_pixel_count(im.size)
            im.load()

            frame = ImageOps.exif_transpose(im)
            try:
                rgba = frame.convert("RGBA")
            finally:
                frame.close()

        try:
            array = np.asarray(rgba, dtype=np.float32)
        finally:
            rgba.close()
    except ImageDecodeError:
        raise
    except Image.DecompressionBombError:
        raise ImageDecodeError(
            "The image exceeds the decompression-bomb pixel limit."
        ) from None
    except (OSError, ValueError, SyntaxError, EOFError):
        raise ImageDecodeError(
            "Could not decode the image data (invalid or corrupt image)."
        ) from None

    np.divide(array, 255.0, out=array)
    rgb = np.ascontiguousarray(array[:, :, :3])
    alpha = array[:, :, 3]
    mask = np.ascontiguousarray(1.0 - alpha)

    return rgb[None, ...], mask[None, ...]


def store_uploaded_image(data: bytes) -> str:
    """Validate raw upload bytes with Pillow and put them in the RAM store."""
    if not data:
        raise EmptyImage("The uploaded image is empty.")

    try:
        with Image.open(BytesIO(data)) as im:
            _check_pixel_count(im.size)
            im.verify()
    except (ImageDecodeError, RamStoreError):
        raise
    except Image.DecompressionBombError:
        raise ImageDecodeError(
            "The image exceeds the decompression-bomb pixel limit."
        ) from None
    except (OSError, ValueError, SyntaxError, EOFError):
        raise ImageDecodeError("The uploaded file is not a valid image.") from None

    return RAM_STORE.put(data)


def _check_pixel_count(size: tuple[int, int]) -> None:
    width, height = int(size[0]), int(size[1])
    if width <= 0 or height <= 0:
        raise ImageDecodeError("The image has invalid dimensions.")
    limit = getattr(Image, "MAX_IMAGE_PIXELS", None) or DEFAULT_MAX_IMAGE_PIXELS
    if width * height > int(limit):
        raise ImageDecodeError(
            f"The image is too large ({width}x{height} pixels, limit {int(limit)})."
        )


class LoadImageRam:
    """Load an image from RAM (file picker / paste) or a URL."""

    CATEGORY = "image"
    FUNCTION = "load"
    RETURN_TYPES = ("IMAGE", "MASK")
    RETURN_NAMES = ("image", "mask")
    DESCRIPTION = (
        "Loads an image from a URL or from an in-RAM upload (file picker or "
        "clipboard). This node never writes image data to disk."
    )

    @classmethod
    def INPUT_TYPES(cls) -> dict:
        return {
            "required": {
                "token": ("STRING", {"default": "", "multiline": False}),
                "consume": ("BOOLEAN", {"default": True}),
            },
            "optional": {
                "url": ("STRING", {"default": "", "multiline": False}),
            },
        }

    @classmethod
    def IS_CHANGED(cls, token: str = "", url: str = "", consume: bool = True) -> str:
        # Re-run only when the source changes.  With consume=True ComfyUI's
        # output cache is what makes a repeated queue reuse the tensor.
        return f"{token}\n{url}"

    def load(self, token: str, url: str = "", consume: bool = True):
        token = (token or "").strip()
        url = (url or "").strip()

        if token:
            data = RAM_STORE.pop(token) if consume else RAM_STORE.peek(token)
            if data is None:
                raise ValueError(
                    "Image expired or already used; re-upload or re-paste it."
                )
        elif url:
            data = fetch_image(url)
        else:
            raise ValueError(
                "No image source: choose/paste an image or set a URL."
            )

        image, mask = decode_image_bytes(data)
        return (torch.from_numpy(image), torch.from_numpy(mask))


NODE_CLASS_MAPPINGS = {NODE_CLASS: LoadImageRam}
NODE_DISPLAY_NAME_MAPPINGS = {NODE_CLASS: "Load Image (RAM)"}
WEB_DIRECTORY = "./web"


# -- HTTP upload route ------------------------------------------------------


def _upload_response(web, status: int, payload: dict):
    return web.json_response(
        payload, status=status, headers={"Cache-Control": "no-store"}
    )


def _make_upload_handler():
    async def upload(request):
        # Imported here so the node package can be imported without aiohttp
        # outside ComfyUI (e.g. by the test harness).
        from aiohttp import web

        declared = request.content_length
        if declared is not None and declared > MAX_BYTES_PER_IMAGE:
            return _upload_response(
                web,
                413,
                {
                    "error": (
                        f"Image is too large; the per-image limit is "
                        f"{MAX_BYTES_PER_IMAGE} bytes."
                    )
                },
            )

        body = bytearray()
        try:
            # iterate the raw body: no multipart parser, no spooled temp file.
            async for chunk in request.content.iter_chunked(UPLOAD_CHUNK_SIZE):
                body.extend(chunk)
                if len(body) > MAX_BYTES_PER_IMAGE:
                    return _upload_response(
                        web,
                        413,
                        {
                            "error": (
                                f"Image is too large; the per-image limit is "
                                f"{MAX_BYTES_PER_IMAGE} bytes."
                            )
                        },
                    )
        except Exception:
            return _upload_response(
                web, 400, {"error": "Upload failed while reading the request body."}
            )

        try:
            token = store_uploaded_image(bytes(body))
        except (ImageDecodeError, RamStoreError) as exc:
            return _upload_response(web, 400, {"error": str(exc)})
        except Exception:
            _log.exception("RAM image upload failed")
            return _upload_response(web, 500, {"error": "Could not store the image."})

        return _upload_response(web, 200, {"token": token, "bytes": len(body)})

    return upload


def _register_upload_route() -> bool:
    try:
        from server import PromptServer
    except ImportError:
        return False

    instance = getattr(PromptServer, "instance", None)
    if instance is None:
        return False

    try:
        instance.routes.post(UPLOAD_ROUTE)(_make_upload_handler())
    except Exception:
        _log.exception("RAM image: could not register the upload route")
        return False
    return True


if not _register_upload_route():
    _log.warning(
        "RAM image: upload route was not registered (ComfyUI server not "
        "available at import time); URL loading still works."
    )
