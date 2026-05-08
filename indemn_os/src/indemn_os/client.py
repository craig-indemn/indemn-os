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


_LONG_TEXT_THRESHOLD = 200


def _format_md_value(value, indent=0):
    """Format a value for markdown output."""
    prefix = "  " * indent
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        if not value:
            return ""
        if all(isinstance(v, str) for v in value):
            if len(value) <= 3 and all(len(v) < 40 for v in value):
                return ", ".join(value)
            return "\n" + "\n".join(f"{prefix}- {v}" for v in value)
        if all(isinstance(v, dict) for v in value):
            parts = []
            for i, item in enumerate(value):
                parts.append("")
                parts.append(_format_md_dict(item, indent + 1))
            return "\n".join(parts)
        return ", ".join(str(v) for v in value)
    if isinstance(value, dict):
        return "\n" + _format_md_dict(value, indent + 1)
    return str(value)


def _format_md_dict(data, indent=0):
    """Format a dict as markdown key-value pairs."""
    prefix = "  " * indent
    lines = []
    long_fields = []

    for k, v in data.items():
        if v is None or v == [] or v == {} or v == "":
            continue

        if isinstance(v, str) and len(v) > _LONG_TEXT_THRESHOLD:
            long_fields.append((k, v))
            continue

        formatted = _format_md_value(v, indent)
        if "\n" in formatted:
            lines.append(f"{prefix}**{k}:**{formatted}")
        else:
            lines.append(f"{prefix}**{k}:** {formatted}")

    for k, v in long_fields:
        lines.append("")
        lines.append(f"{prefix}**{k}:**")
        lines.append(v)

    return "\n".join(lines)


def _format_md_entity(data):
    """Format a single entity as markdown."""
    entity_type = data.pop("_entity_type", None)
    entity_id = data.get("_id", "")
    short_id = entity_id[:8] if isinstance(entity_id, str) and len(entity_id) > 8 else entity_id

    if entity_type:
        header = f"# {entity_type} {short_id}"
    elif entity_id:
        header = f"# {short_id}"
    else:
        header = ""

    body = _format_md_dict(data)
    return f"{header}\n\n{body}" if header else body


def _format_md_list(data):
    """Format a list of entities as markdown."""
    if not data:
        return "(empty)"
    if all(isinstance(item, dict) for item in data):
        parts = []
        for item in data:
            entity_id = item.get("_id", "")
            short_id = entity_id[:8] if isinstance(entity_id, str) and len(entity_id) > 8 else entity_id
            name = item.get("name") or item.get("subject") or item.get("associate_name") or ""
            status = item.get("status", "")

            summary_parts = [f"**{short_id}**"]
            if name:
                summary_parts.append(name)
            if status:
                summary_parts.append(f"({status})")
            parts.append(" — ".join(summary_parts))
        return "\n".join(parts)
    return "\n".join(f"- {item}" for item in data)


def render(data, fmt: str = "json", raw: bool = False):
    """Render output. Default: markdown. Use --format json for structured data."""
    if raw:
        print(orjson.dumps(data, option=orjson.OPT_INDENT_2).decode())
        return

    data = clean_entity(data)

    if fmt == "json":
        if isinstance(data, dict):
            print(_format_md_entity(data))
        elif isinstance(data, list):
            print(_format_md_list(data))
        else:
            print(data)
    elif fmt == "raw":
        print(orjson.dumps(data, option=orjson.OPT_INDENT_2).decode())
