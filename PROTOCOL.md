# Noctis relay protocol — version 1

All bodies are UTF-8 JSON objects with `Content-Type: application/json` and a
`Content-Length` (no chunked encoding). Unknown fields, duplicate keys, `NaN`,
`Infinity`, booleans where numbers are expected and bodies above
`NOCTIS_MAX_BODY_BYTES` (1024 by default) are rejected. Errors are
`{"error": "<code>"}`. Responses carry `Cache-Control: no-store` and no CORS
headers: the API is meant for the game client, not for browsers.

## Endpoints

| Method | Path | Auth | Body | Success |
|---|---|---|---|---|
| GET | `/healthz` | — | — | `200 {"status":"ok"}` |
| GET | `/v1/info` | — | — | `200` (below) |
| POST | `/v1/sessions` | — | `{"protocol":1}` | `201` |
| PUT | `/v1/session` | Bearer | `{}` | `200` |
| DELETE | `/v1/session` | Bearer | — | `204` (idempotent) |
| PUT | `/v1/presence` | Bearer + `presence` | see below | `200` |
| DELETE | `/v1/presence` | Bearer + `presence` | — | `204` |

The two presence routes only exist when the operator enables
`NOCTIS_PRESENCE=1`; otherwise they answer `404`.

### `GET /v1/info`

```json
{
  "protocol": 1,
  "privacyRevision": 1,
  "retentionSeconds": 30,
  "heartbeatSeconds": 10,
  "operator": {"name": "Example relay", "contact": "relay-admin@example.org"},
  "capabilities": ["sessions"],
  "server": {"name": "noctis-relay", "version": "0.1.0"}
}
```

`retentionSeconds` is between 10 and 120, `heartbeatSeconds` at most half of
it. When presence is enabled, `capabilities` is `["sessions","presence"]` and a
`presence` object gives `radius`, `vertical`, `limit` and `minIntervalSeconds`.
`privacyRevision` changes whenever the data handled by the server changes; a
client must ask for consent again when it differs from the one the player
accepted.

### Sessions

`POST /v1/sessions` with `{"protocol":1}` returns

```json
{"protocol": 1, "token": "<64 hex chars>", "expiresInSeconds": 30, "heartbeatSeconds": 10}
```

The token is 32 random bytes in lowercase hex, to be sent as
`Authorization: Bearer <token>`. The server stores only its SHA-256. A session
expires `expiresInSeconds` after creation or after the last successful
heartbeat (`PUT /v1/session` with `{}`). An expired or unknown token gets
`401 session_expired`; the client then creates a new session. `DELETE
/v1/session` always returns `204` for a well-formed token, known or not.

Status codes: `400` invalid body (`invalid_json`, `invalid_fields`,
`invalid_protocol`, `unsupported_protocol`, …), `401` (`invalid_token`,
`session_expired`), `404`, `405` (with `Allow`), `411`, `413`, `415`, `417`,
`429 rate_limited` (with `Retry-After`), `431`, `501` (chunked), `503 capacity`
(with `Retry-After`).

## Experimental: presence

Presence is an **opt-in, operator-enabled** building block for later Noctis
features. The relay only stores and matches positions; it does not render,
validate or animate anything, and it gives no gameplay guarantee. Everything
received from other players must be treated by the game client as untrusted
input.

A client asks for it explicitly at session creation:

```json
{"protocol": 1, "capabilities": ["presence"]}
```

The response then also contains `"capabilities": ["presence"]`. Asking for it
on a server without the capability returns `400 capability_unavailable`; a
session created without it gets `403 capability_not_granted` on presence
routes.

`PUT /v1/presence`:

```json
{"realm": "astralyth:noctis", "profile": "gen1-3f9c2a7d", "instance": "k3J9-random-per-load",
 "x": 120.5, "y": 64.0, "z": -48.25}
```

- `realm`: dimension-like identifier `namespace:path` (lowercase).
- `profile`: 8–64 chars `[A-Za-z0-9_-]`, an opaque key for "same terrain
  generation". Only players with exactly the same realm and profile can match.
  It must not be a raw world seed: derive a non-reversible value on the client.
- `instance`: 16–64 chars, random for each running Minecraft session and shared
  by players in that same session. A future multiplayer integration must
  synchronise this value locally; generating a different value on every client
  would not exclude players already playing together. It is not a persistent
  save identifier. Players sharing an instance never see each other. The relay
  keeps only a truncated SHA-256.
- `x`, `z` in ±30 000 000, `y` in −2048…4096, finite numbers.

The call stores the position (replacing the previous one), extends the session
and returns at most `limit` neighbours sorted by horizontal distance, within
`radius` blocks horizontally **and** `vertical` blocks vertically (so caves and
surface do not mix):

```json
{"expiresInSeconds": 30,
 "nearby": [{"ref": "9c1f0a3be2d47781", "x": 110.0, "y": 65.0, "z": -50.0, "distance": 10.69}]}
```

`ref` is an opaque handle, stable for one observer while both sessions live,
different for every observer, never an account or session identifier. A
position expires `retentionSeconds` after its last update even if the session
is kept alive by heartbeats. Updates faster than `minIntervalSeconds` get
`429`. `DELETE /v1/presence` removes the position immediately.

### Spatial index

Positions are bucketed in cells of `NOCTIS_CELL_SIZE` (32) blocks horizontally
and `NOCTIS_CELL_HEIGHT` (16) vertically using `floor`, so negative coordinates
fall in the right cell. The SQLite table is clustered on
`(realm, profile, cx, cz, cy, session)`; a lookup is one B-tree seek per
neighbouring horizontal cell (5×5 by default) with a range on `cy`, then an
exact distance and height filter. See [BENCHMARKS.md](BENCHMARKS.md).
