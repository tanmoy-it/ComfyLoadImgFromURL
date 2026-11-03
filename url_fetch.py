"""RAM-only image download with a best-effort SSRF guard.

No part of this module opens a file.  The response body is streamed into a
``bytearray`` and returned, bounded by ``max_bytes``.
"""

from __future__ import annotations

import ipaddress
import os
import socket
from urllib.parse import urljoin, urlsplit

import requests

from .ram_store import MAX_BYTES_PER_IMAGE

# Opt-in escape hatch for local/private image servers (dev/test only).
ALLOW_PRIVATE = os.environ.get("RAMIMAGE_ALLOW_PRIVATE", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}

CONNECT_TIMEOUT = 5.0
READ_TIMEOUT = 30.0
REDIRECT_LIMIT = 5
CHUNK_SIZE = 64 * 1024
USER_AGENT = "ComfyUI-RamImage/1.0"


class UrlFetchError(ValueError):
    """A download failure whose message is safe to show in the ComfyUI UI."""


class UrlTooLarge(UrlFetchError):
    pass


class PrivateAddressError(UrlFetchError):
    pass


def fetch_image(url: str, max_bytes: int | None = None) -> bytes:
    """Download *url* into memory and return the raw bytes.

    Only http/https are accepted.  Redirects are followed manually (bounded and
    re-validated) and the body is capped while streaming, so neither a huge
    response nor a redirect to a private address is accepted.
    """
    limit = MAX_BYTES_PER_IMAGE if max_bytes is None else int(max_bytes)
    if limit <= 0:
        raise ValueError("max_bytes must be positive")

    current = url
    session = requests.Session()
    try:
        for _ in range(REDIRECT_LIMIT + 1):
            _validate_url(current)
            try:
                response = session.get(
                    current,
                    stream=True,
                    timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
                    allow_redirects=False,
                    headers={"User-Agent": USER_AGENT, "Accept": "image/*"},
                )
            except requests.RequestException as exc:
                raise UrlFetchError(
                    f"Could not download the image ({type(exc).__name__})."
                ) from None

            try:
                if response.is_redirect or response.status_code in (301, 302, 303, 307, 308):
                    location = response.headers.get("Location")
                    if not location:
                        raise UrlFetchError("The image URL redirected without a target.")
                    current = urljoin(current, location)
                    continue

                if response.status_code != 200:
                    raise UrlFetchError(
                        f"The image URL returned HTTP {response.status_code}."
                    )

                # Content-Type is advisory only: image hosts often send
                # application/octet-stream. The node validates actual bytes
                # with Pillow before decoding.

                declared = response.headers.get("Content-Length")
                if declared is not None:
                    try:
                        if int(declared) > limit:
                            raise UrlTooLarge(_too_large_message(limit))
                    except ValueError:
                        pass

                body = bytearray()
                try:
                    for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
                        if not chunk:
                            continue
                        body.extend(chunk)
                        if len(body) > limit:
                            raise UrlTooLarge(_too_large_message(limit))
                except requests.RequestException as exc:
                    raise UrlFetchError(
                        f"Could not download the image ({type(exc).__name__})."
                    ) from None

                if not body:
                    raise UrlFetchError("The image URL returned an empty body.")
                return bytes(body)
            finally:
                response.close()
    finally:
        session.close()

    raise UrlFetchError(f"Too many redirects (limit is {REDIRECT_LIMIT}).")


def _too_large_message(limit: int) -> str:
    return f"Image is too large; the download limit is {limit} bytes."


def _validate_url(url: str) -> None:
    try:
        parts = urlsplit(url)
        scheme = parts.scheme.lower()
        host = parts.hostname
        port = parts.port
        userinfo = parts.username is not None or parts.password is not None
    except ValueError:
        raise UrlFetchError("The URL is not valid.") from None

    if scheme not in ("http", "https"):
        raise UrlFetchError("Only http and https URLs are supported.")
    if userinfo:
        raise UrlFetchError("URLs with embedded credentials are not supported.")
    if not host:
        raise UrlFetchError("The URL has no host.")
    if port is None:
        port = 443 if scheme == "https" else 80

    if not ALLOW_PRIVATE:
        _check_public_host(host, port)


def _check_public_host(host: str, port: int) -> None:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError, OSError):
        raise UrlFetchError("Could not resolve the image URL host.") from None

    if not infos:
        raise UrlFetchError("Could not resolve the image URL host.")

    for info in infos:
        address_text = str(info[4][0]).split("%", 1)[0]
        try:
            address = ipaddress.ip_address(address_text)
        except ValueError:
            raise PrivateAddressError(
                "The image URL host resolved to an invalid address."
            ) from None
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            address = address.ipv4_mapped
        if not address.is_global:
            raise PrivateAddressError(
                "Refusing to download from a private, loopback, or reserved address."
            )
