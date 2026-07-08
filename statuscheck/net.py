"""HTTP helpers built on the standard library."""

import json
import time
import urllib.error
import urllib.request

from . import REPO_URL, __version__

USER_AGENT = f"Mozilla/5.0 (compatible; status-page-check/{__version__}; +{REPO_URL})"


class FetchError(Exception):
    """A URL could not be fetched after retries."""


def http_get(url, timeout=30, retries=2, retry_delay=2.0):
    """GET a URL and return the decoded body. Retries transient failures."""
    last_err = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            last_err = e
            # 4xx won't improve on retry
            if 400 <= e.code < 500 and e.code != 429:
                break
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            last_err = e
        if attempt < retries:
            time.sleep(retry_delay * (attempt + 1))
    raise FetchError(f"GET {url} failed: {last_err}")


def http_post_json(url, payload, headers=None, timeout=180):
    """POST a JSON payload and return the parsed JSON response."""
    body = json.dumps(payload).encode("utf-8")
    all_headers = {"User-Agent": USER_AGENT, "Content-Type": "application/json"}
    if headers:
        all_headers.update(headers)
    req = urllib.request.Request(url, data=body, headers=all_headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            pass
        raise FetchError(f"POST {url} failed: HTTP {e.code} {detail}")
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
        raise FetchError(f"POST {url} failed: {e}")
