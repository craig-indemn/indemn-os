"""In-process token-bucket rate limiter for the voice frontdoor
(AI-407 Task 2.36 / §10.7 row "Replay of session creation").

**Ordering invariant:** rate-limit MUST fire BEFORE Interaction creation
+ LiveKit room creation. Otherwise an attacker exhausts LiveKit room
slots + writes Interaction audit-trail records before being throttled —
defeating the mitigation.

v1 — single-container in-memory windows per-IP / per-actor / per-Deployment.
When the frontdoor scales beyond one Railway instance, replace with a
Redis-backed limiter (the API surface here is stable; only the storage
changes).

Defaults sized for an internal-team sales surface:
- 10 req/min per IP (a fast-typing user creating multiple sessions; a
  whole office NAT'd to one IP can still saturate this — bump if
  multi-tenant deployments hit it)
- 30 req/min per actor (one user opening/closing many sessions; well
  above any reasonable UX)
- 100 req/min per Deployment (the whole surface combined; circuit
  breaker against runaway clients)

The first limit that trips wins — the response's `scope` field tells
the SDK + ops which limit tripped so retries can target the right
back-off.
"""

import time
from collections import defaultdict, deque


class RateLimiter:
    def __init__(
        self,
        *,
        per_ip_per_minute: int = 10,
        per_actor_per_minute: int = 30,
        per_deployment_per_minute: int = 100,
        window_seconds: int = 60,
    ):
        self.per_ip = per_ip_per_minute
        self.per_actor = per_actor_per_minute
        self.per_deployment = per_deployment_per_minute
        self.window = window_seconds
        # Sliding-window timestamps per key
        self._ip_hits: dict[str, deque] = defaultdict(deque)
        self._actor_hits: dict[str, deque] = defaultdict(deque)
        self._deployment_hits: dict[str, deque] = defaultdict(deque)

    def _check(self, key: str, store: dict, limit: int) -> bool:
        """Sliding-window check + record. Returns True if allowed; False
        if over limit. Mutates the store on True (records the hit)."""
        now = time.monotonic()
        window_start = now - self.window
        hits = store[key]
        # Evict timestamps that fell out of the window
        while hits and hits[0] < window_start:
            hits.popleft()
        if len(hits) >= limit:
            return False
        hits.append(now)
        return True

    def check_ip(self, ip: str) -> bool:
        return self._check(ip, self._ip_hits, self.per_ip)

    def check_actor(self, actor_id: str) -> bool:
        return self._check(actor_id, self._actor_hits, self.per_actor)

    def check_deployment(self, deployment_id: str) -> bool:
        return self._check(
            deployment_id, self._deployment_hits, self.per_deployment
        )

    def check_with_retry(
        self,
        ip: str,
        actor: str | None,
        deployment: str | None,
    ) -> dict:
        """Run all three checks in order; return {allowed,
        retry_after_seconds, scope}. `scope` identifies WHICH limit
        tripped so the SDK can back off the right key (a per-actor
        429 means slow this user; a per-IP 429 means a whole office's
        NAT'd traffic is hot).

        If allowed, RECORDS the hit on all three counters.
        """
        candidates = [
            ("ip", ip, self._ip_hits, self.per_ip),
            ("actor", actor, self._actor_hits, self.per_actor),
            ("deployment", deployment, self._deployment_hits, self.per_deployment),
        ]
        for label, key, store, limit in candidates:
            if key is None:
                continue
            if not self._check(key, store, limit):
                # Compute retry-after as "when does the oldest hit
                # drop out of the window?" — that's when the bucket
                # has room for one more request.
                tail = store[key][0] if store[key] else time.monotonic()
                retry_after = max(
                    1, int(self.window - (time.monotonic() - tail))
                )
                return {
                    "allowed": False,
                    "retry_after_seconds": retry_after,
                    "scope": label,
                }
        return {"allowed": True, "retry_after_seconds": 0, "scope": None}
