"""Standard-library web search adapter with SSRF and size protections."""
from __future__ import annotations

import ipaddress
import os
import socket
import time
from dataclasses import asdict, dataclass
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote_plus, unquote, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener


class WebSearchError(RuntimeError):
    """Search/page failure, kept separate from inference failure."""

    def __init__(self, message: str, code: str = "unavailable"):
        super().__init__(message)
        self.code = code


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _public_https(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme.lower() != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise WebSearchError("only public HTTPS URLs are allowed")
    if parsed.hostname.lower() in {"localhost", "localhost.localdomain"}:
        raise WebSearchError("local hosts are blocked")
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(parsed.hostname, None)}
    except OSError as exc:
        raise WebSearchError("host resolution failed") from exc
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise WebSearchError("private or local addresses are blocked")
    return url


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise WebSearchError("redirect rejected")


def _fetch(url: str, max_bytes: int = 1_000_000):
    _public_https(url)
    request = Request(url, headers={"User-Agent": "PonkunHome/1.0 local search"})
    with build_opener(_NoRedirect).open(request, timeout=8.0) as response:
        length = response.headers.get("Content-Length")
        if length and int(length) > max_bytes:
            raise WebSearchError("response exceeds size limit")
        body = response.read(max_bytes + 1)
        if len(body) > max_bytes:
            raise WebSearchError("response exceeds size limit")
        return response.headers, body


class _SearchParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.results = []
        self.current = None
        self.capture = None
        self.buffer = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = set((attrs.get("class") or "").split())
        if tag == "a" and "result__a" in classes:
            href = attrs.get("href", "")
            parsed = urlparse(href)
            target = parse_qs(parsed.query).get("uddg", [""])[0] if parsed.path == "/l/" else href
            self.current = {"title": "", "url": unquote(target), "snippet": ""}
            self.capture, self.buffer = "title", []
        elif self.current and "result__snippet" in classes:
            self.capture, self.buffer = "snippet", []

    def handle_data(self, data):
        if self.current and self.capture:
            self.buffer.append(data)

    def handle_endtag(self, tag):
        if self.current and self.capture:
            self.current[self.capture] = " ".join("".join(self.buffer).split())
        if tag == "a" and self.current and self.current["title"]:
            self.results.append(self.current)
            self.current = None
        self.capture, self.buffer = None, []


def web_search(query: str, max_results: int = 5) -> list[dict]:
    if os.environ.get("PONKUN_WEB_SEARCH_ENABLED", "0").strip() != "1":
        raise WebSearchError("web search is not configured")
    if os.environ.get("PONKUN_WEB_SEARCH_ENGINE", "duckduckgo").strip().lower() != "duckduckgo":
        raise WebSearchError("unsupported search engine")
    query = str(query).strip()
    if not query:
        raise WebSearchError("empty search query")
    endpoint = os.environ.get("PONKUN_DDG_ENDPOINT", "https://html.duckduckgo.com/html/")
    url = endpoint + ("&" if "?" in endpoint else "?") + "q=" + quote_plus(query)
    try:
        headers, body = _fetch(url)
        if "text/html" not in (headers.get("Content-Type") or "").lower():
            raise WebSearchError("search response is not HTML")
        lowered_body = body.lower()
        if b"challenge" in lowered_body or b"captcha" in lowered_body:
            raise WebSearchError("search provider challenge or access block")
        parser = _SearchParser()
        parser.feed(body.decode("utf-8", errors="replace"))
    except (HTTPError, URLError, TimeoutError, ValueError) as exc:
        raise WebSearchError(f"search request failed: {type(exc).__name__}") from exc
    results = []
    for item in parser.results:
        try:
            _public_https(item["url"])
        except WebSearchError:
            continue
        results.append({"title": item["title"][:300], "url": item["url"], "snippet": item["snippet"][:1000], "fetched_at": _now(), "published_at": None})
        if len(results) >= max(1, min(int(max_results), 10)):
            break
    if not results:
        raise WebSearchError("no usable search results")
    return results


class _TextParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag.lower() in {"script", "style", "noscript", "template"}:
            self.skip += 1

    def handle_endtag(self, tag):
        if tag.lower() in {"script", "style", "noscript", "template"} and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def read_web_page(url: str, max_bytes: int = 1_000_000) -> dict:
    try:
        headers, body = _fetch(url, max_bytes=max_bytes)
    except (HTTPError, URLError, TimeoutError, ValueError) as exc:
        raise WebSearchError(f"page request failed: {type(exc).__name__}") from exc
    if "text/html" not in (headers.get("Content-Type") or "").lower():
        raise WebSearchError("page is not HTML")
    parser = _TextParser()
    parser.feed(body.decode("utf-8", errors="replace"))
    return {"url": url, "text": " ".join(" ".join(parser.parts).split())[:200_000], "fetched_at": _now()}
