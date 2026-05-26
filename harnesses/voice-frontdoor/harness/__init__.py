"""Voice frontdoor — Starlette HTTP server for POST /sessions.

Part of AI-407 Phase 2B (voice runtime build). Two-Railway-services model
per design §10.3 + Option C (§15.2): this service handles POST /sessions
(JWT validation + LiveKit room creation + AgentDispatch + token mint);
the existing indemn-runtime-voice-worker handles per-room agent jobs.
"""
