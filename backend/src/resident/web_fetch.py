"""The sandboxed web fetcher for the Live World Source (v0.2 Step 1, ADR-0009).

This module is the *visual organ only*: it knows how to safely fetch one URL
and turn the response into a neutral, normalized observation record. It knows
nothing about the mind, thoughts, or the World Window's decision logic — the
chain stays ``fetch → world.observation → world.experience → impression →
(optional) thought`` and **reading ≠ thought** is enforced upstream
(:mod:`resident.world`), not here.

Security posture (the v0.2 brief, verbatim constraints):

- **SSRF guard.** Only ``http``/``https`` public resources: localhost/loopback,
  RFC1918/private, link-local (incl. cloud metadata <internal-ip>),
  CGNAT 100.64/10, unique-local, multicast/reserved and unspecified addresses
  are refused — checked on the *literal* host first, then on **every** address
  DNS returns, and every redirect hop is re-validated from scratch.
- **DNS-rebinding defense (TOCTOU).** After validation the fetcher dials the
  *validated IP* directly (pinned connection, TLS SNI/cert still bound to the
  real hostname) — it never re-resolves, so a rebinding DNS answer cannot
  swap the target between check and connect.
- **Caps.** connect/total timeouts, response-size cap (raw) and
  decompressed-size cap (gzip-bomb guard), redirect-count cap, allowed ports
  (80/443 only), and a content-type allowlist. No credentials, no cookies, no
  scripts, no binaries, no browser (Playwright explicitly out of scope).

Failure is a **value, not a crash**: every refusal/timeout/HTTP error raises a
typed :class:`FetchError` subclass whose ``reason`` maps 1:1 to the audit event
the World Window records (``world.fetch_blocked`` / ``world.fetch_timeout`` /
``world.fetch_failed``), after which the window legitimately no-ops and the
MindLoop never sees an exception.

Determinism/hermetics: the network seam is injectable — ``transport``
(default: real pinned sockets) and ``resolver`` (default: ``socket.getaddrinfo``)
can be replaced by fakes, so every unit test runs with **zero public network**.
"""
from __future__ import annotations

import gzip
import http.client
import ipaddress
import json
import socket
import ssl
import time
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import quote, urljoin, urlsplit

__all__ = [
    "FetchError", "BlockedURL", "FetchTimeout", "UnsupportedContent",
    "HTTPStatusError", "ResponseTooLarge", "NetworkError",
    "FetchResult", "SafeFetcher", "PinnedHttpTransport",
]

_DEFAULT_UA = "ResidentWorldWindow/0.2 (research sandbox; +provenance)"

# Content types we are willing to *read* (v0.2 scope: documents, not apps).
_HTML_TYPES = ("text/html", "application/xhtml+xml")
_TEXT_TYPES = ("text/plain",)
_JSON_TYPES = ("application/json",)
_RSS_TYPES = ("application/rss+xml", "application/atom+xml", "text/xml", "application/xml")


# --------------------------------------------------------------------- errors


class FetchError(Exception):
    """Base class for every fetch refusal/failure. ``reason`` maps 1:1 to the
    audit-event name the World Window records; the window then no-ops."""

    reason = "fetch_failed"

    def __init__(self, message: str, *, detail: dict | None = None):
        super().__init__(message)
        self.detail = detail or {}


class BlockedURL(FetchError):
    """SSRF guard refusal (scheme/host/port/IP not allowed)."""

    reason = "fetch_blocked"


class FetchTimeout(FetchError):
    """Connect or read deadline exceeded."""

    reason = "fetch_timeout"


class UnsupportedContent(FetchError):
    """Content type outside the v0.2 allowlist (JS apps / binaries / media)."""

    reason = "unsupported_content"


class HTTPStatusError(FetchError):
    """Non-2xx final status (404/500/...)."""

    reason = "http_error"


class ResponseTooLarge(FetchError):
    """Raw or decompressed body exceeded the size cap."""

    reason = "response_too_large"


class NetworkError(FetchError):
    """Connection refused/reset/TLS failure — the generic dirty real world."""

    reason = "network_error"


# ------------------------------------------------------------------ ssrf guard


_CGNAT = ipaddress.ip_network("<internal-ip>/10")  # shared-address space (not is_private on all Pythons)
# RFC 2544 benchmark range — never real services; used by transparent (fake-IP)
# DNS proxies. Default: still forbidden. A host whose DNS *always* answers from
# this range is behind a transparent proxy; dialing it is permitted only by the
# explicit operator opt-in ``allow_proxy_dns`` (TLS stays bound to the hostname).
_PROXY_RANGES = (ipaddress.ip_network("198.18.0.0/15"),)


def _is_proxy_ip(ip) -> bool:
    return any(ip in net for net in _PROXY_RANGES)


def _ip_allowed(ip_str: str) -> bool:
    """False for every address class the sandbox must never dial."""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    if ip.version == 6 and getattr(ip, "ipv4_mapped", None) is not None:
        ip = ip.ipv4_mapped  # ::ffff:<internal-ip> is still <internal-ip>
    if ip in _CGNAT:
        return False
    return not (
        ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_multicast
        or ip.is_reserved or ip.is_unspecified
    )


def _validate_url(url: str) -> tuple:
    """Parse + validate scheme/host/port. Returns (scheme, host, port, path).
    Raises :class:`BlockedURL`. *Literal* IP hosts are IP-checked here; hostname
    checks happen at resolve time (all records) — see :meth:`SafeFetcher._guard`."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise BlockedURL(f"scheme not allowed: {parts.scheme!r}", detail={"url": url})
    host = parts.hostname
    if not host:
        raise BlockedURL("no hostname", detail={"url": url})
    if parts.username or parts.password:
        raise BlockedURL("credentials in URL are not allowed", detail={"url": url})
    port = parts.port
    default = 443 if parts.scheme == "https" else 80
    if port is None:
        port = default
    if port not in (80, 443):
        raise BlockedURL(f"port not allowed: {port}", detail={"url": url})
    try:  # a literal-IP host is checkable right now
        ipaddress.ip_address(host)
    except ValueError:
        pass  # a name — resolved + checked per-address in _guard
    else:
        if not _ip_allowed(host):
            raise BlockedURL(f"IP host not allowed: {host}", detail={"url": url})
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    return parts.scheme, host, port, path


def _wire_target(host: str, path: str) -> tuple[str, str]:
    """IRI → ASCII wire form: percent-encode non-ASCII path/query characters
    (preserving structure and existing %-escapes) and IDNA-encode the host.
    http.client requires an ASCII request target; the *original* IRI is what
    gets recorded in snapshots (the wire form is transport-derived)."""
    try:
        ascii_host = host if host.isascii() else host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise BlockedURL(f"cannot IDNA-encode host {host!r}: {exc}",
                         detail={"host": host})
    wire_path = quote(path, safe="/%:?=&#~+;@,!$'()[]*-._")
    return ascii_host, wire_path


# ------------------------------------------------------------------- transports


class PinnedHttpTransport:
    """The real transport: dials the *validated* IP (rebinding-proof), TLS SNI
    and certificate validation still bound to the real hostname."""

    def open(self, *, scheme: str, host: str, ip: str, port: int, path: str,
             headers: dict[str, str], timeout: float):
        if scheme == "https":
            conn = _PinnedHTTPSConnection(host, ip, port, timeout=timeout)
        else:
            conn = _PinnedHTTPConnection(host, ip, port, timeout=timeout)
        try:
            conn.request("GET", path, headers=headers)
            return conn.getresponse()
        except Exception:
            conn.close()
            raise

    def resolve(self, host: str) -> list[str]:
        infos = socket.getaddrinfo(host, None)
        return [info[4][0] for info in infos]


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host: str, ip: str, port: int, timeout: float):
        super().__init__(host, port, timeout=timeout)
        self._pinned_ip = ip

    def connect(self):
        self.sock = socket.create_connection(
            (self._pinned_ip, self.port), timeout=self.timeout)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, ip: str, port: int, timeout: float):
        super().__init__(host, port, timeout=timeout)
        self._pinned_ip = ip
        self._ssl_ctx = ssl.create_default_context()

    def connect(self):
        sock = socket.create_connection(
            (self._pinned_ip, self.port), timeout=self.timeout)
        # server_hostname = the real host: SNI + certificate validation stay
        # bound to the name even though we dial the pinned IP.
        self.sock = self._ssl_ctx.wrap_socket(sock, server_hostname=self.host)


# ------------------------------------------------------------------- extraction


class _HTMLExtractor(HTMLParser):
    """Title + visible text + outbound links. Scripts/styles/noscript are
    dropped; malformed HTML never raises (the parser is event-driven and
    tolerant by design)."""

    _SKIP = {"script", "style", "noscript", "svg", "template"}

    def __init__(self, base_url: str, max_links: int = 50):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.max_links = max_links
        self.title = ""
        self.text_chunks: list[str] = []
        self.links: list[str] = []
        self._skip_depth = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if self._in_title and tag != "title":
            self._in_title = False  # malformed HTML: title never closed — stop at the next tag
        if tag in self._SKIP:
            self._skip_depth += 1
            return
        if tag == "title":
            self._in_title = True
        if tag in ("a", "area"):
            for name, value in attrs:
                if name == "href" and value:
                    absolute = urljoin(self.base_url, value.strip())
                    if absolute.startswith(("http://", "https://")) and absolute not in self.links:
                        if len(self.links) < self.max_links:
                            self.links.append(absolute)
                    break

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip_depth > 0:
            self._skip_depth -= 1
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._skip_depth:
            return
        if self._in_title:
            self.title += data
        else:
            if data.strip():
                self.text_chunks.append(data.strip())


def _extract_html(body: str, base_url: str) -> tuple[str, str, list[str]]:
    parser = _HTMLExtractor(base_url)
    try:
        parser.feed(body)
        parser.close()
    except Exception:  # malformed HTML: keep whatever was extracted
        pass
    title = " ".join(parser.title.split())
    text = " ".join(parser.text_chunks)
    return (title or "Untitled page"), text, list(parser.links)


def _extract_rss(body: str) -> tuple[str, str, list[str]]:
    """RSS/Atom: the document is observed as one page; its *item links* are the
    outbound links (real accident fodder). A malformed feed degrades to text."""
    import xml.etree.ElementTree as ET

    try:
        root = ET.fromstring(body)
    except Exception:
        return "XML document", " ".join(body.split())[:2000], []
    title = ""
    texts: list[str] = []
    links: list[str] = []

    def _walk(el):
        nonlocal title
        tag = el.tag.rsplit("}", 1)[-1].lower()
        if tag == "title" and el.text and not title:
            title = el.text.strip()
        if tag == "link" and (el.text or "").strip():
            link = el.text.strip()
            if link.startswith(("http://", "https://")) and link not in links:
                links.append(link)
        if tag in ("summary", "description", "content") and el.text:
            texts.append(el.text.strip())
        for child in el:
            _walk(child)

    _walk(root)
    text = " ".join(texts)
    return (title or "Untitled feed"), text, links[:50]


def _extract_json(body: str) -> tuple[str, str]:
    try:
        data = json.loads(body)
    except Exception:
        return "JSON document", " ".join(body.split())[:2000]
    title = "JSON document"
    if isinstance(data, dict):
        for key in ("title", "name"):
            if isinstance(data.get(key), str) and data[key].strip():
                title = data[key].strip()
                break
        text = json.dumps(data, ensure_ascii=False)[:20000]
    else:
        text = json.dumps(data, ensure_ascii=False)[:20000]
    return title, text


def _normalize_text(text: str) -> str:
    return " ".join(text.split())


# -------------------------------------------------------------------- result


@dataclass(frozen=True)
class FetchResult:
    """The neutral record of one successful fetch — exactly the fields an
    immutable snapshot persists (requested/final URL, status, content type,
    title, normalized text, hash, redirect chain, timings, sizes)."""

    requested_url: str
    final_url: str
    status: int
    content_type: str
    source_type: str            # html | text | json | rss
    title: str
    text: str                   # normalized (whitespace-collapsed)
    links: tuple[str, ...]      # absolute outbound links (deduped, capped)
    content_hash: str           # sha256 of the raw response bytes
    byte_size: int              # raw byte size as received
    redirect_chain: tuple[str, ...] = field(default_factory=tuple)
    duration_ms: int = 0
    fetched_at: str = ""

    @property
    def excerpt(self) -> str:
        return self.text[:500]


# -------------------------------------------------------------------- fetcher


class SafeFetcher:
    """Fetches one public http(s) document behind the SSRF guard and the caps.
    The network seam (``transport``/``resolver``) is injectable for hermetic
    tests; the default is real pinned sockets + real DNS."""

    def __init__(
        self,
        transport=None,
        resolver=None,
        *,
        connect_timeout: float = 10.0,
        total_timeout: float = 30.0,
        max_bytes: int = 2 * 1024 * 1024,
        max_decompressed: int = 5 * 1024 * 1024,
        max_redirects: int = 5,
        max_links: int = 50,
        user_agent: str = _DEFAULT_UA,
        allow_proxy_dns: bool = False,
    ):
        self.transport = transport or PinnedHttpTransport()
        self.resolver = resolver or self.transport.resolve
        self.connect_timeout = connect_timeout
        self.total_timeout = total_timeout
        self.max_bytes = max_bytes
        self.max_decompressed = max_decompressed
        self.max_redirects = max_redirects
        self.max_links = max_links
        self.user_agent = user_agent
        self.allow_proxy_dns = allow_proxy_dns

    # ------------------------------------------------------------- public API

    def fetch(self, url: str) -> FetchResult:
        started = time.monotonic()
        fetched_at = datetime.now(timezone.utc).isoformat()
        chain: list[str] = []
        current = url

        for _hop in range(self.max_redirects + 1):
            scheme, host, port, path = _validate_url(current)
            host, path = _wire_target(host, path)
            # every hop (initial or redirect) is validated from scratch: resolve
            # + require every address public, then dial the pinned IP.
            ips = self._guard(host, current)
            ip = ips[0]

            deadline = started + self.total_timeout
            remaining = max(0.05, deadline - time.monotonic())
            try:
                resp = self.transport.open(
                    scheme=scheme, host=host, ip=ip, port=port, path=path,
                    headers=self._headers(host), timeout=min(self.connect_timeout, remaining),
                )
            except FetchTimeout:
                raise
            except (socket.timeout, TimeoutError) as exc:
                raise FetchTimeout(f"timeout fetching {current}: {exc}") from exc
            except BlockedURL:
                raise
            except Exception as exc:
                raise NetworkError(f"network error fetching {current}: {exc}",
                                   detail={"url": current}) from exc

            status = resp.status
            if status in (301, 302, 303, 307, 308):
                resp.close()
                location = (resp.getheader("Location") or "").strip()
                if not location:
                    raise HTTPStatusError(f"{status} without Location for {current}",
                                          detail={"url": current, "status": status})
                if len(chain) >= self.max_redirects:
                    raise NetworkError(f"too many redirects for {url}",
                                       detail={"url": url, "hops": len(chain)})
                target = urljoin(current, location)
                _validate_url(target)  # scheme/port/credentials of the hop
                chain.append(target)
                current = target
                continue

            body = self._read_body(resp, current)
            duration_ms = int((time.monotonic() - started) * 1000)
            content_type = (resp.getheader("Content-Type") or "").strip()
            return self._build_result(
                url, current, status, content_type, body, chain, duration_ms, fetched_at)

        raise NetworkError(f"redirect limit exhausted for {url}", detail={"url": url})

    # -------------------------------------------------------------- internals

    def _guard(self, host: str, url: str) -> list[str]:
        """Resolve ``host`` and require every address to be public (or, with the
        explicit ``allow_proxy_dns`` opt-in, in the benchmark/proxy range). Returns
        the validated addresses; the fetcher dials ``addresses[0]`` (pinned)."""
        try:
            ips = list(self.resolver(host))
        except FetchError:
            raise
        except Exception as exc:
            raise NetworkError(f"DNS resolution failed for {host}: {exc}",
                               detail={"url": url}) from exc
        if not ips:
            raise BlockedURL(f"host resolved to no address: {host}", detail={"url": url})
        for ip_str in ips:
            try:
                ip = ipaddress.ip_address(ip_str)
            except ValueError:
                raise BlockedURL(f"unparseable resolved address {ip_str}",
                                 detail={"url": url, "resolved": ips})
            if _ip_allowed(ip_str) or (self.allow_proxy_dns and _is_proxy_ip(ip)):
                continue
            raise BlockedURL(
                f"host {host} resolves to a forbidden address ({ip_str})",
                detail={"url": url, "resolved": ips, "forbidden": ip_str})
        return ips

    def _headers(self, host: str) -> dict[str, str]:
        return {
            "Host": host,
            "User-Agent": self.user_agent,
            "Accept": "text/html, text/plain, application/json, application/rss+xml, application/xml; q=0.9",
            "Accept-Encoding": "gzip",
            "Connection": "close",
        }

    def _read_body(self, resp, url: str) -> bytes:
        """Stream the body under both caps: raw received bytes and (for gzip)
        decompressed output — the gzip-bomb guard."""
        raw = bytearray()
        chunks = iter(lambda: resp.read(64 * 1024), b"")
        try:
            for chunk in chunks:
                raw.extend(chunk)
                if len(raw) > self.max_bytes:
                    raise ResponseTooLarge(
                        f"response exceeds {self.max_bytes} bytes: {url}",
                        detail={"url": url, "byte_size": len(raw)})
        except FetchTimeout:
            raise
        except (socket.timeout, TimeoutError) as exc:
            raise FetchTimeout(f"timeout reading {url}: {exc}") from exc
        except ResponseTooLarge:
            raise
        except Exception as exc:
            raise NetworkError(f"read error on {url}: {exc}", detail={"url": url}) from exc

        encoding = (resp.getheader("Content-Encoding") or "").lower()
        if "gzip" in encoding:
            try:
                return self._gunzip_capped(bytes(raw), url)
            except ResponseTooLarge:
                raise
            except Exception as exc:
                raise NetworkError(f"bad gzip body from {url}: {exc}",
                                   detail={"url": url}) from exc
        return bytes(raw)

    def _gunzip_capped(self, raw: bytes, url: str) -> bytes:
        out = bytearray()
        decomp = zlib.decompressobj(16 + zlib.MAX_WBITS)
        for i in range(0, len(raw), 64 * 1024):
            piece = decomp.decompress(raw[i:i + 64 * 1024], self.max_decompressed + 1)
            out.extend(piece)
            if len(out) > self.max_decompressed:
                raise ResponseTooLarge(
                    f"decompressed body exceeds {self.max_decompressed} bytes: {url}",
                    detail={"url": url, "decompressed_size": len(out)})
        return bytes(out)

    def _build_result(self, requested_url: str, final_url: str, status: int,
                      content_type: str, body: bytes, chain: list[str],
                      duration_ms: int, fetched_at: str) -> FetchResult:
        if status >= 400:
            raise HTTPStatusError(f"HTTP {status} for {final_url}",
                                  detail={"url": final_url, "status": status})
        import hashlib

        media = content_type.split(";")[0].strip().lower()
        text_body = body.decode("utf-8", errors="replace")
        if media in _HTML_TYPES:
            source_type = "html"
            title, text, links = _extract_html(text_body, final_url)
        elif media in _RSS_TYPES:
            source_type = "rss"
            title, text, links = _extract_rss(text_body)
        elif media in _JSON_TYPES or media.endswith("+json"):
            source_type = "json"
            title, text = _extract_json(text_body)
            links = []
        elif media in _TEXT_TYPES:
            source_type = "text"
            title, text, links = "Text document", _normalize_text(text_body), []
        elif not media or media == "application/octet-stream":
            # no usable header: sniff the body (some servers send nothing)
            head = text_body.lstrip()[:512].lower()
            if head.startswith(("<!doctype html", "<html")) or "<html" in head:
                source_type = "html"
                title, text, links = _extract_html(text_body, final_url)
            elif head.startswith(("<?xml", "<rss", "<feed")):
                source_type = "rss"
                title, text, links = _extract_rss(text_body)
            elif head.startswith(("{", "[")):
                source_type = "json"
                title, text = _extract_json(text_body)
                links = []
            else:
                raise UnsupportedContent(
                    f"content type {media or 'unknown'} not readable by the v0.2 sandbox",
                    detail={"url": final_url, "content_type": content_type})
        else:
            # an explicit type outside the allowlist is refused (no sniffing
            # past a declared non-document type: binaries/apps stay out)
            raise UnsupportedContent(
                f"content type {media} not readable by the v0.2 sandbox",
                detail={"url": final_url, "content_type": content_type})
        return FetchResult(
            requested_url=requested_url,
            final_url=final_url,
            status=status,
            content_type=content_type or f"({source_type} sniffed)",
            source_type=source_type,
            title=title,
            text=_normalize_text(text)[:200000],
            links=tuple(links[: self.max_links]),
            content_hash=hashlib.sha256(body).hexdigest(),
            byte_size=len(body),
            redirect_chain=tuple(chain),
            duration_ms=duration_ms,
            fetched_at=fetched_at,
        )
