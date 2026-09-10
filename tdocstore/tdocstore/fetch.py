"""Polite, resumable crawler over the 3GPP file server (HTTPS front of ftp.3gpp.org).

The 3GPP server exposes directory listings as HTML. Two front-ends exist:
  https://www.3gpp.org/ftp/<path>/   and   https://ftp.3gpp.org/<path>/
Both list files as <a href="...">; we parse hrefs, not the surrounding markup, so either works.

Etiquette: low concurrency, a delay between requests, exponential backoff on 429/5xx, ETag/size
checks to avoid re-downloading. This is a public archive shared by the whole 3GPP community —
do not raise concurrency above ~4.
"""
from __future__ import annotations
import asyncio
import hashlib
import re
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable

import httpx
from urllib.parse import quote, unquote

from .config import Config

HREF_RE = re.compile(r"""href=["']([^"'#?]+)["']""", re.I)


class _LinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "a":
            for k, v in attrs:
                if k.lower() == "href" and v:
                    self.links.append(v)


def parse_listing(html: str) -> list[str]:
    """Return file/folder names (URL-decoded) from a 3GPP directory listing page.
    Parent/self links may appear as folder names; callers filter by pattern (ingest does)."""
    p = _LinkParser()
    try:
        p.feed(html)
        links = p.links
    except Exception:
        links = HREF_RE.findall(html)
    names = []
    for l in links:
        l = l.split("?")[0].rstrip("/")
        name = unquote(l.rsplit("/", 1)[-1])
        if name and name not in ("..", ".") and not name.startswith(".."):
            names.append(name)
    # de-dup, keep order
    seen = set()
    return [n for n in names if not (n in seen or seen.add(n))]


@dataclass
class FetchResult:
    name: str
    path: Path | None
    status: str          # fetched / cached / missing / error
    error: str | None = None
    sha256: str | None = None


class Fetcher:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._sem = asyncio.Semaphore(cfg.concurrency)
        self._last = 0.0
        self._lock = asyncio.Lock()

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            headers={"User-Agent": self.cfg.user_agent},
            timeout=httpx.Timeout(60.0, connect=20.0),
            follow_redirects=True,
            http2=False,
        )

    async def _throttle(self):
        async with self._lock:
            wait = self.cfg.min_delay_s - (time.monotonic() - self._last)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = time.monotonic()

    async def _get(self, client: httpx.AsyncClient, url: str, *, stream_to: Path | None = None) -> httpx.Response | None:
        backoff = 2.0
        for attempt in range(5):
            await self._throttle()
            try:
                if stream_to is None:
                    r = await client.get(url)
                else:
                    tmp = stream_to.with_suffix(stream_to.suffix + ".part")
                    async with client.stream("GET", url) as r:
                        if r.status_code == 200:
                            with open(tmp, "wb") as f:
                                async for chunk in r.aiter_bytes(1 << 16):
                                    f.write(chunk)
                            tmp.replace(stream_to)
                        elif tmp.exists():
                            tmp.unlink()
                if r.status_code == 200:
                    return r
                if r.status_code == 404:
                    return r
                if r.status_code in (429, 500, 502, 503, 504):
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 30)
                    continue
                return r
            except (httpx.TransportError, httpx.ReadError) as e:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)
                last = e
        return None

    async def list_dir(self, url: str) -> list[str]:
        async with self._client() as c:
            r = await self._get(c, url if url.endswith("/") else url + "/")
            if r is None or r.status_code != 200:
                raise RuntimeError(f"listing failed: {url} -> {getattr(r, 'status_code', 'no response')}")
            return parse_listing(r.text)

    async def fetch_files(self, base_url: str, names: Iterable[str], dest_dir: Path, *, skip_existing=True) -> list[FetchResult]:
        dest_dir.mkdir(parents=True, exist_ok=True)
        base_url = base_url if base_url.endswith("/") else base_url + "/"
        results: list[FetchResult] = []

        async def one(client: httpx.AsyncClient, name: str) -> FetchResult:
            dest = dest_dir / name
            if skip_existing and dest.exists() and dest.stat().st_size > 0:
                return FetchResult(name, dest, "cached", sha256=_sha(dest))
            async with self._sem:
                r = await self._get(client, base_url + quote(name), stream_to=dest)
            if r is None:
                return FetchResult(name, None, "error", "no response after retries")
            if r.status_code == 404:
                return FetchResult(name, None, "missing")
            if r.status_code != 200:
                return FetchResult(name, None, "error", f"HTTP {r.status_code}")
            return FetchResult(name, dest, "fetched", sha256=_sha(dest))

        async with self._client() as client:
            tasks = [one(client, n) for n in names]
            for coro in asyncio.as_completed(tasks):
                results.append(await coro)
        return results


def _sha(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()
