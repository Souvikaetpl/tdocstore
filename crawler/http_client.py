import time
import logging

import httpx

from . import config

log = logging.getLogger("crawler.http")


class HttpClient:
    """Thin wrapper around httpx with an honest UA, rate limiting, and retries."""

    def __init__(self):
        self._client = httpx.Client(
            headers={"User-Agent": config.USER_AGENT},
            timeout=config.REQUEST_TIMEOUT_SECONDS,
            follow_redirects=True,
        )
        self._last_request_at = 0.0

    def _throttle(self):
        elapsed = time.monotonic() - self._last_request_at
        wait = config.REQUEST_DELAY_SECONDS - elapsed
        if wait > 0:
            time.sleep(wait)
        self._last_request_at = time.monotonic()

    def get(self, url: str) -> httpx.Response:
        last_exc = None
        for attempt in range(1, config.MAX_RETRIES + 1):
            self._throttle()
            try:
                resp = self._client.get(url)
                if resp.status_code == 429 or resp.status_code >= 500:
                    raise httpx.HTTPStatusError(
                        f"retryable status {resp.status_code}", request=resp.request, response=resp
                    )
                resp.raise_for_status()
                return resp
            except (httpx.HTTPStatusError, httpx.TransportError) as exc:
                last_exc = exc
                backoff = 2 ** attempt
                log.warning("GET %s failed (attempt %d/%d): %s — retrying in %ds",
                            url, attempt, config.MAX_RETRIES, exc, backoff)
                time.sleep(backoff)
        raise RuntimeError(f"Failed to GET {url} after {config.MAX_RETRIES} attempts") from last_exc

    def download(self, url: str, dest_path):
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = dest_path.with_suffix(dest_path.suffix + ".part")
        last_exc = None
        for attempt in range(1, config.MAX_RETRIES + 1):
            self._throttle()
            try:
                with self._client.stream("GET", url) as resp:
                    if resp.status_code == 429 or resp.status_code >= 500:
                        raise httpx.HTTPStatusError(
                            f"retryable status {resp.status_code}", request=resp.request, response=resp
                        )
                    resp.raise_for_status()
                    with open(tmp_path, "wb") as f:
                        for chunk in resp.iter_bytes():
                            f.write(chunk)
                    tmp_path.replace(dest_path)
                return
            except (httpx.HTTPStatusError, httpx.TransportError) as exc:
                last_exc = exc
                tmp_path.unlink(missing_ok=True)
                backoff = 2 ** attempt
                log.warning("Download %s failed (attempt %d/%d): %s — retrying in %ds",
                            url, attempt, config.MAX_RETRIES, exc, backoff)
                time.sleep(backoff)
        raise RuntimeError(f"Failed to download {url} after {config.MAX_RETRIES} attempts") from last_exc

    def close(self):
        self._client.close()
