"""Build event metadata attached to messages.

One save = one event with full change metadata.
The metadata tells watches what happened — which method was invoked,
which state was transitioned to, which fields changed.
"""


def build_event_metadata(entity, method: str, changes: list) -> dict:
    """Build the event metadata attached to messages.
    One save = one event with full change metadata."""
    meta = {}

    if method:
        meta["method"] = method

    if hasattr(entity, "_pending_transition") and entity._pending_transition:
        meta["state_transition"] = entity._pending_transition
        entity._pending_transition = None  # Clear after capture

    if changes:
        meta["fields_changed"] = [c["field"] for c in changes]

    return meta
