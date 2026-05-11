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
        # Resolution order for both base_url and token:
        #   1. environment variable (highest priority — service tokens, dev override)
        #   2. ~/.indemn/credentials  (set by `indemn auth login`)
        #   3. fallback default     (localhost — useful for local dev with no creds)
        # Pre-fix: base_url ignored credentials, defaulting to localhost:8000.
        # Users authenticated via `indemn auth login` against api.os.indemn.ai
        # then ran `indemn <anything>` and got cryptic 401s because the request
        # silently routed to localhost. The credentials file already carried
        # `api_url` — we just weren't reading it.
        creds_data = self._load_credentials()
        self.base_url = (
            os.environ.get("INDEMN_API_URL")
            or creds_data.get("api_url")
            or "http://localhost:8000"
        )
        self.token = (
            os.environ.get("INDEMN_SERVICE_TOKEN")
            or creds_data.get("access_token")
            or ""
        )

    @staticmethod
    def _load_credentials() -> dict:
        import json as _json
        from pathlib import Path

        token_file = Path.home() / ".indemn" / "credentials"
        if not token_file.exists():
            return {}
        try:
            return _json.loads(token_file.read_text())
        except (OSError, ValueError):
            return {}

    def _headers(self):
        h = {"Content-Type": "application/json"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        causation = os.environ.get("INDEMN_CAUSATION_MESSAGE_ID")
        if causation:
            h["X-Causation-Message-ID"] = causation
        # Effective actor id (Bug #22 forensics): the runtime harness sets
        # this env var to the associate it's running on behalf of, so the
        # changes collection can record which associate acted — separate
        # from the auth token's actor identity.
        effective_actor = os.environ.get("INDEMN_EFFECTIVE_ACTOR_ID")
        if effective_actor:
            h["X-Effective-Actor-Id"] = effective_actor
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

    def _client(self, timeout: int = None) -> httpx.Client:
        # Long default for POST/PUT — capability invocations (fetch-new, scraping,
        # external-system calls) can take minutes. Override with INDEMN_CLI_TIMEOUT env var.
        if timeout is None:
            timeout = int(os.environ.get("INDEMN_CLI_TIMEOUT", "600"))
        return httpx.Client(
            base_url=self.base_url,
            follow_redirects=True,
            timeout=timeout,
        )

    def get(self, path, params=None):
        # GET is typically fast — shorter timeout
        with self._client(timeout=int(os.environ.get("INDEMN_CLI_GET_TIMEOUT", "60"))) as client:
            r = client.get(path, params=params, headers=self._headers())
            self._handle_error(r)
            return r.json()

    def post(self, path, json=None, params=None):
        with self._client() as client:
            r = client.post(path, json=json, params=params, headers=self._headers())
            self._handle_error(r)
            return r.json()

    def put(self, path, json=None):
        with self._client() as client:
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
    "_id",
    "id",
    "org_id",
    "created_at",
    "updated_at",
    "created_by",
    "version",
    "revision_id",
}

_CLEAN_STRIP_FIELDS = {
    "org_id",
    "version",
    "revision_id",
    "created_by",
    "updated_at",
}


def clean_entity(data):
    """Strip internal infrastructure fields from entity data for clean output.

    Keeps _id (useful for referencing), created_at (useful for timing),
    and all domain fields. Strips org_id, version, created_by, updated_at,
    revision_id.
    """
    if isinstance(data, dict):
        return {k: clean_entity(v) for k, v in data.items()
                if k not in _CLEAN_STRIP_FIELDS}
    if isinstance(data, list):
        return [clean_entity(item) for item in data]
    return data


def _format_xml_value(value, indent=0):
    """Format a value as XML content."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        if not value:
            return ""
        prefix = "  " * indent
        parts = []
        for item in value:
            if isinstance(item, dict):
                parts.append(_format_xml_dict(item, indent))
            else:
                parts.append(f"{prefix}<item>{item}</item>")
        return "\n".join(parts)
    if isinstance(value, dict):
        return _format_xml_dict(value, indent)
    return str(value)


def _format_xml_dict(data, indent=0):
    """Format a dict as XML elements."""
    prefix = "  " * indent
    lines = []

    for k, v in data.items():
        if v is None or v == "" or v == [] or v == {}:
            continue

        if isinstance(v, dict):
            inner = _format_xml_dict(v, indent + 1)
            lines.append(f"{prefix}<{k}>")
            lines.append(inner)
            lines.append(f"{prefix}</{k}>")
        elif isinstance(v, list):
            if not v:
                continue
            lines.append(f"{prefix}<{k}>")
            inner_prefix = "  " * (indent + 1)
            for item in v:
                if isinstance(item, dict):
                    lines.append(_format_xml_dict(item, indent + 1))
                else:
                    lines.append(f"{inner_prefix}<item>{item}</item>")
            lines.append(f"{prefix}</{k}>")
        elif isinstance(v, str) and len(v) > 200:
            lines.append(f"{prefix}<{k}>")
            lines.append(v)
            lines.append(f"{prefix}</{k}>")
        else:
            lines.append(f"{prefix}<{k}>{v}</{k}>")

    return "\n".join(lines)


def _format_xml_entity(data):
    """Format a single entity as XML."""
    entity_type = data.pop("_entity_type", None) or "entity"
    entity_id = data.get("_id", "")

    lines = [f"<{entity_type} id=\"{entity_id}\">"]
    lines.append(_format_xml_dict(data, 1))
    lines.append(f"</{entity_type}>")
    return "\n".join(lines)


def _format_xml_list(data):
    """Format a list of entities as XML."""
    if not data:
        return "<results />"
    lines = ["<results>"]
    for item in data:
        if isinstance(item, dict):
            entity_type = item.pop("_entity_type", None) or "item"
            entity_id = item.get("_id", "")
            name = item.get("name") or item.get("subject") or item.get("associate_name") or ""
            status = item.get("status", "")
            attrs = f' id="{entity_id}"'
            if name:
                attrs += f' name="{name}"'
            if status:
                attrs += f' status="{status}"'
            lines.append(f"  <{entity_type}{attrs} />")
        else:
            lines.append(f"  <item>{item}</item>")
    lines.append("</results>")
    return "\n".join(lines)


def render(data, fmt: str = "json", raw: bool = False):
    """Render output. Markdown by default, JSON when INDEMN_OUTPUT_FORMAT=json."""
    if raw or fmt == "raw":
        print(orjson.dumps(data, option=orjson.OPT_INDENT_2).decode())
        return

    data = clean_entity(data)

    if os.environ.get("INDEMN_OUTPUT_FORMAT") == "json":
        print(orjson.dumps(data, option=orjson.OPT_INDENT_2).decode())
        return

    if isinstance(data, dict):
        print(_format_xml_entity(data))
    elif isinstance(data, list):
        print(_format_xml_list(data))
    else:
        print(data)
