# E2 Reliable Gateway Design

## Goal

Make the QQ bridge resilient to slow model calls, duplicate OneBot events, temporary WebSocket failures, and process restarts while preserving the existing E1 security boundary.

## Architecture

The bridge continues to use the pinned `openclaw` CLI, but removes `--local` when `OPENCLAW_USE_GATEWAY=true`. The CLI becomes a thin authenticated client of the already-running loopback Gateway, so model execution and session ownership remain in the persistent Gateway process.

OneBot input is dispatched into bounded asynchronous tasks. Model calls run in worker threads so the event loop remains available for WebSocket input, reminders, backups, and outbox delivery. A global semaphore limits model concurrency, while one `asyncio.Lock` per QQ user preserves conversation order.

SQLite remains the canonical state store. E2 adds:

- `inbound_messages` for message-id idempotency and processing state.
- `outbox` for durable replies and reminders with dedupe keys, attempts, and exponential retry times.
- `runtime_metrics` for last successful model call, last successful delivery, and circuit-breaker state.

## Message Flow

1. Parse and filter a OneBot private-message event.
2. Atomically claim its `message_id`; ignore an already completed or active claim.
3. Acquire the user's session lock and the global model semaphore.
4. Run local identity, security, housekeeper, and style handlers first. Run OpenClaw through the Gateway only when needed.
5. Persist the reply in the outbox using `reply:<message_id>` as its dedupe key.
6. Deliver due outbox rows through the active OneBot WebSocket. Mark the row sent only after `websocket.send()` succeeds.
7. Mark the inbound message completed after its reply is confirmed sent.

Reminders use `reminder:<notification_id>` dedupe keys. A reminder is marked sent only after the corresponding outbox row is sent. If the process stops between those steps, the next reminder scan sees the already-sent outbox row and completes the reminder state without sending twice.

## Failure Handling

- Agent calls retry once for transient timeout/network failures.
- Three consecutive agent failures open a 60-second circuit breaker. During the cooldown, requests receive the existing fallback immediately instead of spawning more failing calls.
- Outbox failures use bounded exponential retry and survive bridge restarts.
- A disconnected OneBot socket cancels connection-scoped workers without deleting queued outbox rows. Reconnection drains them.
- Gateway health checks probe its loopback WebSocket port rather than merely checking that the wrapper script exists.

## Configuration

- `OPENCLAW_USE_GATEWAY=true`
- `OPENCLAW_GATEWAY_PROBE_URL=ws://127.0.0.1:18789`
- `MAX_CONCURRENT_AGENT_CALLS=2`
- `MAX_PENDING_EVENTS=50`
- `AGENT_RETRY_ATTEMPTS=2`
- `AGENT_CIRCUIT_FAILURE_THRESHOLD=3`
- `AGENT_CIRCUIT_COOLDOWN_SECONDS=60`
- `OUTBOX_RETRY_BASE_SECONDS=2`
- `OUTBOX_MAX_ATTEMPTS=8`

All values are positive integers where applicable. The Gateway remains loopback-only and the `qq` agent keeps the E1 read-only Docker sandbox and denied tool list.

## Observability And Testing

The dashboard reports Gateway mode, pending/failed outbox counts, active inbound work, and last successful model/delivery timestamps. The watchdog verifies bridge, OneBot, proxy, Gateway TCP reachability, and SQLite queue health.

Tests cover command construction, configuration validation, asynchronous dispatch, per-user ordering, duplicate suppression, outbox persistence/retry/dedupe, reminder recovery, dashboard metrics, and watchdog probes. The complete existing suite must remain green before deployment.
