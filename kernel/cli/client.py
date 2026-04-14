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

    def get(self, path, params=None):
        with httpx.Client(base_url=self.base_url) as client:
            r = client.get(path, params=params, headers=self._headers(), timeout=30)
            self._handle_error(r)
            return r.json()

    def post(self, path, json=None, params=None):
        with httpx.Client(base_url=self.base_url) as client:
            r = client.post(
                path, json=json, params=params, headers=self._headers(), timeout=60
            )
            self._handle_error(r)
            return r.json()

    def put(self, path, json=None):
        with httpx.Client(base_url=self.base_url) as client:
            r = client.put(path, json=json, headers=self._headers(), timeout=60)
            self._handle_error(r)
            return r.json()

    def delete(self, path):
        with httpx.Client(base_url=self.base_url) as client:
            r = client.delete(path, headers=self._headers(), timeout=30)
            self._handle_error(r)
            return r.json() if r.text else {}


def render(data, fmt: str = "json"):
    """Render output in the requested format."""
    if fmt == "json":
        print(orjson.dumps(data, option=orjson.OPT_INDENT_2).decode())
    elif fmt == "table":
        if isinstance(data, list) and data:
            keys = list(data[0].keys())[:6]
            print(" | ".join(k.ljust(20) for k in keys))
            print("-" * (22 * len(keys)))
            for row in data:
                print(" | ".join(str(row.get(k, ""))[:20].ljust(20) for k in keys))
        else:
            print(orjson.dumps(data, option=orjson.OPT_INDENT_2).decode())
