import gzip
import socket

import httpx
import pymupdf
import pytest

from jdx_researcher.models import ReaderError
from jdx_researcher.network import PublicNetworkBackend, SafeFetcher, public_ip, validate_url
from jdx_researcher.readers import extract


@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "ftp://example.org/file", "http://127.0.0.1/",
    "http://[::1]/", "http://169.254.169.254/latest/meta-data",
    "http://192.168.1.5/", "http://10.0.0.2/", "http://localhost/",
    "https://secret@example.org/", "https://example.org:8080/",
    "http://[::ffff:127.0.0.1]/", "http://[2002:7f00:1::]/",
    "https://server.internal/", " http://example.org/",
])
def test_source_policy(url):
    with pytest.raises(ReaderError):
        validate_url(url)


def test_preserve_functional_query_and_normalize_fragment():
    assert validate_url("https://EXAMPLE.org/item?version=1&lang=es#heading") == "https://example.org/item?version=1&lang=es"


@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "::ffff:8.8.8.8"])
def test_reserved_addresses(address):
    assert not public_ip(address)


async def test_dns_pinning_uses_exact_public_address():
    class Delegate:
        async def connect_tcp(self, host, port, **kwargs):
            assert host == "93.184.216.34"
            return "pinned-stream"
    async def resolve(host, port, **kwargs):
        assert host == "source.example.org"
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", port))]
    backend = PublicNetworkBackend(delegate=Delegate(), resolver=resolve)
    assert await backend.connect_tcp("source.example.org", 443) == "pinned-stream"


async def test_dns_mixed_private_answer_blocked():
    async def resolve(host, port, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (ip, port))
                for ip in ("93.184.216.34", "127.0.0.1")]
    with pytest.raises(ReaderError):
        await PublicNetworkBackend(resolver=resolve).connect_tcp("rebinding.example.org", 443)


async def test_redirect_to_private_blocked(settings):
    def respond(request):
        return httpx.Response(302, headers={"Location": "http://169.254.169.254/secrets"})
    fetcher = SafeFetcher(settings, transport=httpx.MockTransport(respond))
    try:
        with pytest.raises(ReaderError):
            await fetcher.fetch("https://example.org/start")
    finally:
        await fetcher.close()


async def test_decoded_byte_limit(settings):
    settings.max_html_bytes = 1024
    body = gzip.compress(b"a" * 5000)
    def respond(request):
        return httpx.Response(200, content=body, headers={
            "Content-Type": "text/html", "Content-Encoding": "gzip", "Content-Length": str(len(body))})
    fetcher = SafeFetcher(settings, transport=httpx.MockTransport(respond))
    try:
        with pytest.raises(ReaderError):
            await fetcher.fetch("https://example.org/large")
    finally:
        await fetcher.close()


async def test_http_failures_are_explicit(settings):
    fetcher = SafeFetcher(settings, transport=httpx.MockTransport(lambda _: httpx.Response(429)))
    try:
        with pytest.raises(ReaderError, match="429"):
            await fetcher.fetch("https://example.org/")
    finally:
        await fetcher.close()


def test_pdf_page_localizers_and_limits(settings):
    doc = pymupdf.open()
    for index in range(2):
        page = doc.new_page()
        page.insert_text((50, 50), f"PDF evidence page {index + 1}: throughput 42")
    body = doc.tobytes()
    doc.close()
    settings.max_pdf_pages = 1
    result = extract(body, "application/pdf", settings)
    assert len(result.pages) == 1 and result.pages[0][0] == 1
    assert "page 1" in result.text and "page 2" not in result.text
    assert result.warnings


def test_html_extracts_text_and_keeps_unknown_date(settings):
    body = b"<html><head><title>Study</title></head><body><article><h1>Local inference</h1><p>Measured throughput is 42 requests per second under test conditions.</p><p>Limitations include missing independent reproduction and absent hardware specifications.</p></article></body></html>"
    result = extract(body, "text/html", settings)
    assert "throughput" in result.text
    assert result.published_at is None


def test_empty_document_rejected(settings):
    with pytest.raises(ReaderError):
        extract(b"", "text/plain", settings)
