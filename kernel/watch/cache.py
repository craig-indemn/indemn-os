"""In-memory watch cache.

Watches are loaded from all Role entities at startup and cached in memory.
Cache key: (org_id, entity_type) → list of {watch, role_name}.
60-second TTL with immediate invalidation when any Role entity is saved.

For multi-instance deployment: each instance maintains its own cache with TTL.
Stronger consistency via MongoDB Change Stream on roles collection (future).
"""

import time

_cache: dict[tuple[str, str], list[dict]] = {}
_cache_loaded_at: float = 0
_CACHE_TTL = 60  # seconds


async def load_watch_cache():
    """Load all watches from all roles into the cache."""
    global _cache, _cache_loaded_at
    from kernel_entities.role import Role

    _cache = {}

    async for role in Role.find({}):
        for watch in role.watches:
            key = (str(role.org_id), watch.entity_type)
            if key not in _cache:
                _cache[key] = []
            _cache[key].append(
                {
                    "watch": watch,
                    "role_name": role.name,
                }
            )

    _cache_loaded_at = time.time()


def get_cached_watches(org_id: str, entity_type: str) -> list[dict]:
    """Get watches for an org + entity type. Returns stale cache if TTL expired."""
    # In production, a background task would refresh on TTL expiry.
    # For now, return whatever is in the cache.
    key = (org_id, entity_type)
    return _cache.get(key, [])


async def invalidate_watch_cache():
    """Called when any Role entity is saved (kernel entity cache invalidation).
    Triggers immediate reload."""
    await load_watch_cache()
