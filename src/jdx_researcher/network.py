"""Public web transport: validate URLs, pin vetted DNS answers, recheck redirects.

Configured LLM/search services use separate clients and never pass credentials here.
"""
import asyncio
import ipaddress
import socket
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpcore
import httpx
from httpcore._backends.anyio import AnyIOBackend

from .models import ReaderError


def public_ip(value: str) -> bool:
    try:
        ip = ipaddress.ip_address(value)
        if not ip.is_global:
            return False
        if isinstance(ip, ipaddress.IPv6Address):
            return not (ip.ipv4_mapped or ip.sixtofour or ip.teredo)
        return True
    except ValueError:
        return False


def validate_url(value: str) -> str:
    if len(value) > 4096 or value != value.strip() or any(ord(c) < 32 for c in value):
        raise ReaderError("Invalid URL")
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in ("https", "http") or not parsed.hostname:
            raise ReaderError("Only HTTP(S) sources are supported")
        if parsed.username is not None or parsed.password is not None:
            raise ReaderError("Source URLs cannot contain credentials")
        if parsed.port not in (None, 80, 443):
            raise ReaderError("Source ports must be 80 or 443")
        host = parsed.hostname.rstrip(".").encode("idna").decode("ascii").lower()
        if "%" in host or host in ("localhost", "metadata.google.internal") or host.endswith((".localhost", ".local", ".internal")):
            raise ReaderError("Private source host is blocked")
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            if not public_ip(host):
                raise ReaderError("Private source address is blocked")
        rendered_host = "[" + host + "]" if ":" in host else host
        port = parsed.port
        if port is not None and not ((parsed.scheme == "https" and port == 443) or (parsed.scheme == "http" and port == 80)):
            rendered_host += ":" + str(port)
        return urlunsplit((parsed.scheme, rendered_host, parsed.path or "/", parsed.query, ""))
    except (ValueError, UnicodeError) as exc:
        raise ReaderError("Malformed source URL") from exc


class PublicNetworkBackend(httpcore.AsyncNetworkBackend):
    def __init__(self, delegate=None, resolver=None):
        self.delegate = delegate or AnyIOBackend()
        self.resolver = resolver

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        if isinstance(host, bytes):
            host = host.decode("ascii")
        resolver = self.resolver or asyncio.get_running_loop().getaddrinfo
        records = await resolver(host, port, type=socket.SOCK_STREAM)
        addresses = list(dict.fromkeys(record[4][0] for record in records))
        if not addresses or any(not public_ip(address) for address in addresses):
            raise ReaderError("DNS points to a private or reserved address")
        last_error = None
        for address in addresses:
            try:
                # Connect to this exact address; httpcore preserves the original TLS hostname.
                return await self.delegate.connect_tcp(
                    address, port, timeout=timeout, local_address=local_address,
                    socket_options=socket_options)
            except (OSError, httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                last_error = exc
        raise httpcore.ConnectError("Unable to connect to public source") from last_error

    async def connect_unix_socket(self, *args, **kwargs):
        raise ReaderError("Unix sockets are not source URLs")

    async def sleep(self, seconds):
        await asyncio.sleep(seconds)


class CoreStream(httpx.AsyncByteStream):
    def __init__(self, stream):
        self.stream = stream

    async def __aiter__(self):
        async for part in self.stream:
            yield part

    async def aclose(self):
        await self.stream.aclose()


class PublicTransport(httpx.AsyncBaseTransport):
    def __init__(self):
        self.pool = httpcore.AsyncConnectionPool(
            network_backend=PublicNetworkBackend(), max_connections=4,
            max_keepalive_connections=4, http2=False, retries=0)

    async def handle_async_request(self, request):
        core_request = httpcore.Request(
            method=request.method,
            url=httpcore.URL(scheme=request.url.raw_scheme, host=request.url.raw_host,
                             port=request.url.port, target=request.url.raw_path),
            headers=request.headers.raw, content=request.stream, extensions=request.extensions)
        try:
            response = await self.pool.handle_async_request(core_request)
        except httpcore.TimeoutException as exc:
            raise httpx.TimeoutException("Source request timed out", request=request) from exc
        except httpcore.NetworkError as exc:
            raise httpx.TransportError("Source network error", request=request) from exc
        return httpx.Response(response.status, headers=response.headers,
                              stream=CoreStream(response.stream), extensions=response.extensions)

    async def aclose(self):
        await self.pool.aclose()


class SafeFetcher:
    def __init__(self, settings, transport=None):
        self.settings = settings
        self.client = httpx.AsyncClient(
            transport=transport or PublicTransport(), trust_env=False, follow_redirects=False,
            timeout=httpx.Timeout(settings.http_timeout_seconds),
            headers={"User-Agent": "jdx-researcher/0.1 (personal research)", "Accept-Encoding": "identity"})

    async def close(self):
        await self.client.aclose()

    async def fetch(self, url: str):
        current = validate_url(url)
        for _ in range(5):
            async with self.client.stream("GET", current) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    location = response.headers.get("location")
                    if not location:
                        raise ReaderError("Redirect has no target")
                    current = validate_url(urljoin(current, location))
                    continue
                if response.status_code != 200:
                    raise ReaderError(f"Source HTTP status {response.status_code}")
                media_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
                if media_type not in ("text/html", "application/xhtml+xml", "text/plain", "text/markdown", "application/pdf"):
                    raise ReaderError("Unsupported or missing source content type")
                limit = self.settings.max_pdf_bytes if media_type == "application/pdf" else self.settings.max_html_bytes
                try:
                    advertised = int(response.headers.get("content-length", "0"))
                except ValueError:
                    advertised = 0
                if advertised > limit:
                    raise ReaderError("Source exceeds byte limit")
                content = bytearray()
                # Count decoded bytes too, even if a server ignores Accept-Encoding: identity.
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > limit:
                        raise ReaderError("Source exceeds byte limit")
                if not content:
                    raise ReaderError("Empty source")
                return current, media_type, bytes(content)
        raise ReaderError("Too many redirects")
