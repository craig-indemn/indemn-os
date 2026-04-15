"""HTTP client for CLI API-mode.

All CLI commands go through the API. The CLI never touches MongoDB directly.
This is the trust boundary enforcement: CLI is outside, API is inside.
"""

import os
import sys

import httpx
import orjson


class CLIClient:
    """HTTP client for CLI API-mode. All CLI commands go through the API."""

    def __init__(self):
        self.base_url = os.environ.get("INDEMN_API_URL", "http://localhost:8000")
        self.token = os.environ.get("INDEMN_SERVICE_TOKEN", "")

    def _headers(self):
        h = {"Content-Type": "application/json"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def _handle_error(self, response: httpx.Response):
        """Handle non-2xx responses with readable error output."""
        if response.status_code >= 400:
            try:
                body = response.json()
                msg = body.get("message", body.get("error", response.text))
            except Exception:
                msg = response.text
            print(f"Error {response.status_code}: {msg}", file=sys.stderr)
            raise SystemExit(1)

    def _client(self, timeout: int = 30) -> httpx.Client:
        return httpx.Client(
            base_url=self.base_url,
            follow_redirects=True,
            timeout=timeout,
        )

    def get(self, path, params=None):
        with self._client() as client:
            r = client.get(path, params=params, headers=self._headers())
            self._handle_error(r)
            return r.json()

    def post(self, path, json=None, params=None):
        with self._client(timeout=60) as client:
            r = client.post(path, json=json, params=params, headers=self._headers())
            self._handle_error(r)
            return r.json()

    def put(self, path, json=None):
        with self._client(timeout=60) as client:
            r = client.put(path, json=json, headers=self._headers())
            self._handle_error(r)
            return r.json()

    def delete(self, path):
        with self._client() as client:
            r = client.delete(path, headers=self._headers())
            self._handle_error(r)
            return r.json() if r.text else {}

    def stream(self, method, path, params=None):
        """Return an httpx streaming response context manager.

        Usage:
            with client.stream("GET", "/api/events", params={}) as response:
                for line in response.iter_lines():
                    print(line)
        """
        client = httpx.Client(base_url=self.base_url, timeout=None)
        return client.stream(method, path, params=params, headers=self._headers())


_INFRASTRUCTURE_FIELDS = {
    "_id", "id", "org_id", "created_at", "updated_at", "created_by",
    "version", "revision_id",
}


def render(data, fmt: str = "json"):
    """Render output in the requested format."""
    if fmt == "json":
        print(orjson.dumps(data, option=orjson.OPT_INDENT_2).decode())
    elif fmt == "table":
        if isinstance(data, list) and data:
            # Domain fields first, infrastructure last. Show 5 columns max.
            all_keys = list(data[0].keys())
            domain_keys = [k for k in all_keys if k not in _INFRASTRUCTURE_FIELDS]
            keys = domain_keys[:5] if domain_keys else all_keys[:5]

            # Column widths: first column gets 30, rest get 25
            widths = [30] + [25] * (len(keys) - 1)

            header = " | ".join(k.ljust(w)[:w] for k, w in zip(keys, widths))
            print(header)
            print("-" * len(header))
            for row in data:
                vals = []
                for k, w in zip(keys, widths):
                    v = str(row.get(k, ""))
                    vals.append(v.ljust(w)[:w])
                print(" | ".join(vals))
        else:
            print(orjson.dumps(data, option=orjson.OPT_INDENT_2).decode())
