# Noctis relay protocol — version 1

All bodies are UTF-8 JSON objects with `Content-Type: application/json` and a
`Content-Length` (no chunked encoding). Unknown fields, duplicate keys, `NaN`,
`Infinity`, booleans where numbers are expected and bodies above
`NOCTIS_MAX_BODY_BYTES` (1024 by default; `NOCTIS_ECHO_MAX_BODY_BYTES`, 6144 by
default, for `PUT /v1/echoes/clip`) are rejected. Errors are
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
| PUT | `/v1/echoes/clip` | Bearer + `echoes` | see below | `200` |
| POST | `/v1/echoes/next` | Bearer + `echoes` | see below | `200` |
| DELETE | `/v1/echoes` | Bearer + `echoes` | — | `204` |

The two presence routes only exist when the operator enables
`NOCTIS_PRESENCE=1`, the three echo routes when `NOCTIS_ECHOES=1`; otherwise
they answer `404`.

### `GET /v1/info`

```json
{
  "protocol": 1,
  "privacyRevision": 3,
  "retentionSeconds": 30,
  "heartbeatSeconds": 10,
  "operator": {"name": "Example relay", "contact": "relay-admin@example.org"},
  "capabilities": ["sessions"],
  "server": {"name": "noctis-relay", "version": "0.3.0"}
}
```

`retentionSeconds` is between 10 and 120, `heartbeatSeconds` at most half of
it. When presence is enabled, `capabilities` is `["sessions","presence"]` and a
`presence` object gives `radius`, `vertical`, `limit` and `minIntervalSeconds`.
`privacyRevision` changes whenever the data handled by the server changes; a
client must ask for consent again when it differs from the one the player
accepted. Revision 2 (relay 0.2.0) attaches a keyed in-memory fingerprint of
the client network to each session. Revision 3 (relay 0.3.0) adds the optional
echoes and chat capabilities below; routes and bodies of the earlier protocol 1
features are unchanged. With echoes enabled, `capabilities` also lists
`"echoes"` (and `"chat"` when enabled) and an `echoes` object gives
`ttlSeconds`, `radius`, `vertical`, `publishIntervalSeconds`,
`fetchIntervalSeconds`, `maxBodyBytes` and `chat`.

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
/v1/session` always returns `204` for a well-formed token, known or not, from
any network: holding the token is enough to revoke it, and revocation frees
its quota at once. A failed authentication never changes any session.

### Client networks

A *client network* is one IPv4 address or one IPv6 /64, taken from the TCP
peer or, only when the peer is a declared trusted proxy, from the right-most
`X-Forwarded-For` value. Each session is attributed to the network that last
used it successfully (an in-memory keyed fingerprint, see
[PRIVACY.md](PRIVACY.md)); at most `NOCTIS_MAX_SESSIONS_PER_NETWORK` (16 by
default) live sessions may be attributed to one network.

- Default `NOCTIS_SESSION_BINDING=migrate`: a session used from a new network
  (Wi-Fi to mobile, new lease, IPv6 privacy address outside the /64) moves
  there if that network still has room, otherwise the request gets
  `429 too_many_sessions` and nothing changes. A token handed to another host
  therefore counts against the host that uses it.
- `strict`: a session used from another network gets `401 session_expired`
  without any change; the client opens a new session from its new network.

Clients must already handle `401` by creating a new session and `429`/`503` by
waiting `Retry-After` seconds; nothing else is required from them.

### Errors and limits

| Status | `error` | Meaning |
|---|---|---|
| `400` | `invalid_json`, `invalid_fields`, `invalid_protocol`, `unsupported_protocol`, `invalid_<field>`, `bad_request`, … | Malformed body or request |
| `401` | `invalid_token`, `session_expired` | Create a new session |
| `403` | `forbidden` | Network blocked by the operator (connection closed) |
| `403` | `capability_not_granted` | Presence or echo route (or chat lines) without the capability |
| `404`, `405` (with `Allow`), `411`, `413`, `415`, `417`, `431`, `501` | | HTTP framing errors |
| `429` | `rate_limited` | Per-network, per-prefix or per-session budget, or presence/echo interval |
| `429` | `too_many_sessions` | Network already holds its maximum of live sessions |
| `429` | `concurrency_limited` | Too many requests in progress for this network |
| `503` | `overloaded` | A relay-wide budget is exhausted |
| `503` | `capacity` | `NOCTIS_MAX_SESSIONS` reached |
| `503` | `area_full` | Presence or echo cell full (below) |

Every `429` and `503` carries `Retry-After` (seconds, ≥ 1). Requests refused
before their body was read close the connection. Budgets are checked in this
order: per-network request bucket; then, for a request bearing a live session
token, that session's bucket (`NOCTIS_SESSION_REQUEST_RATE`), otherwise the
relay-wide request budget. In strict binding mode, a token replayed from
another network also uses the anonymous budget and cannot spend its holder’s
session budget. Requests of existing sessions therefore never
compete with anonymous traffic for the relay-wide budget. Session creation
then checks the per-network and per-prefix (IPv4 /24, IPv6 /48) creation
buckets, the live-session cap, the relay-wide creation budget and
`NOCTIS_MAX_SESSIONS`; a refused creation gives its creation tokens back.
Connections from a blocked network that reach the relay directly, from a
network already holding `NOCTIS_MAX_CONNECTIONS_PER_PEER` sockets, or beyond
`NOCTIS_MAX_CONNECTIONS`, are reset without an answer.

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
`429`. `DELETE /v1/presence` removes the position immediately. Presence
updates also draw from a relay-wide budget (`503 overloaded`). A position that
would enter a cell already holding `NOCTIS_PRESENCE_MAX_PER_CELL` (64) live
positions is refused with `503 area_full` and nothing changes (session expiry,
update interval and network included); moving inside one's current cell is
always accepted.

**What presence is not.** It is not Minecraft authentication and not a proof
of position: realm, profile, instance and coordinates are whatever the client
declares, and a modified client can invent them, appear anywhere or never
appear. Several sessions can be opened by one person within the limits above
(per network, per prefix, globally), so the relay cannot prevent Sybil
identities, and nothing here is a defence against network-level DDoS. The
limits only keep the relay's own cost bounded. Clients must treat every
neighbour as untrusted, cosmetic input: no command, block change or game
decision may depend on it.

### Spatial index

Positions are bucketed in cells of `NOCTIS_CELL_SIZE` (32) blocks horizontally
and `NOCTIS_CELL_HEIGHT` (16) vertically using `floor`, so negative coordinates
fall in the right cell. The SQLite table is clustered on
`(realm, profile, cx, cz, cy, session)`; a lookup is one B-tree seek per
neighbouring horizontal cell (5×5 by default) with a range on `cy`, then an
exact distance and height filter. With the per-cell cap, one lookup examines
at most (2×span+1)² × (vertical cells) × cap live rows (5 × 5 × 3 × 64 = 4 800
by default), whatever the crowd size. See [BENCHMARKS.md](BENCHMARKS.md).

## Echoes

Echoes are an **opt-in, operator-enabled** capability (`NOCTIS_ECHOES=1`, off
by default). A player's game uploads a very short clip of its own movement; a
nearby opted-in player of **another** game later receives it once and replays
it briefly as a cosmetic apparition. Chat lines are a separate
sub-capability (`NOCTIS_ECHO_CHAT=1`, off by default) that a client may only
request when its player accepted it separately.

Request them at session creation; `chat` requires `echoes`:

```json
{"protocol": 1, "capabilities": ["echoes", "chat"]}
```

The response lists the granted capabilities. A relay without them answers
`400 capability_unavailable`; a session without `echoes` gets
`403 capability_not_granted` on echo routes, and a clip carrying `chat` from a
session without `chat` gets the same error.

### `PUT /v1/echoes/clip`

```json
{"realm": "astralyth:noctis", "profile": "noctis-t1-3f9c2a7d0b1e4c55",
 "instance": "k3J9-random-per-server-run", "x": 120.5, "y": 64.0, "z": -48.25,
 "account": "0f1e2d3c-4b5a-4968-8776-655443322110",
 "frames": [[0, 0, 0, 0, 64, 0, 64, 1], [2, 6, 0, 0, 64, 0, 64, 1]],
 "actions": [[1, 1, 2, 0, 0, "minecraft:stone"]],
 "chat": [[2, "hello"]]}
```

- `realm`, `profile`, `instance`, `x`, `y`, `z`: as for presence; `x/y/z` is
  the clip origin. The game derives `profile` from its terrain generator
  settings (never the seed) and shares `instance` among all players of one
  server run, so they never see each other.
- `account`: lowercase UUID of the Minecraft account. It is **not verified**.
- `frames`: 2–60 arrays `[t, dx, dy, dz, yaw, pitch, head, flags]`; `t` in
  ticks (0–120, strictly increasing, first frame `[0,0,0,0,…]`), offsets in
  1/32 block (±1536), `yaw`/`head` 0–255, `pitch` −64…64, `flags` bits
  1 ground, 2 crouch, 4 sprint, 8 swing, 16 swim. Steps faster than
  `NOCTIS_ECHO_MAX_SPEED` (1.2) blocks/tick horizontally or
  `NOCTIS_ECHO_MAX_VERTICAL_SPEED` (4.0) vertically get
  `400 implausible_movement`.
- `actions` (optional): ≤ 8 arrays `[t, kind, bx, by, bz, block]`, `kind`
  1 break / 2 place, block offsets ±64, `block` a `namespace:path` id. Each
  must be within 6.5 blocks of the body's eyes within 10 ticks
  (`400 implausible_action`).
- `chat` (optional, needs `chat`): ≤ 2 arrays `[t, text]`, 1–64 characters,
  no controls, format/bidi characters, private use, line separators, `§`,
  leading/trailing or double spaces (`400 invalid_chat`). Lines with links,
  long repeats or duplicating the previous line are silently dropped.

Unknown keys, wrong types or out-of-range values are refused as a whole. The
relay stores the clip re-serialised from validated values, replacing the
session's previous clip, and answers `{"expiresInSeconds": 60, "chat": <lines
kept>}`. Uploads closer than `publishIntervalSeconds` get `429`; a clip that
would enter a cell already holding `NOCTIS_ECHO_MAX_PER_CELL` (32) live clips
of the same realm and profile gets `503 area_full` and nothing changes.

### `POST /v1/echoes/next`

Body: `realm`, `profile`, `instance`, `x`, `y`, `z` of the observer. Returns
`{"echo": null}` or the nearest live clip within `radius` blocks horizontally
and `vertical` blocks vertically, with the same realm and profile, from
another live session **and** another instance, not yet delivered to this
session:

```json
{"echo": {"ref": "9c1f0a3be2d47781", "ageSeconds": 4.2,
          "x": 120.5, "y": 64.0, "z": -48.25,
          "account": "0f1e2d3c-4b5a-4968-8776-655443322110",
          "frames": [...], "actions": [...], "chat": [[2, "hello"]]}}
```

`chat` is only present when both sessions hold the `chat` capability. `ref`
is opaque and differs per observer. Each clip is delivered at most once per
observer; the delivery record expires with the clip. Lookups closer than
`fetchIntervalSeconds` get `429`. Uploads and lookups both draw from a
relay-wide echo budget (`503 overloaded`).

### Withdrawal

`DELETE /v1/echoes` removes the session's clip and delivery records at once;
`DELETE /v1/session` and session expiry do the same. Clips also expire after
`ttlSeconds`. A clip whose source session ended is never served, even before
the purge removes it.

**What echoes are not.** Accounts, positions, movements and actions are
declared by clients; a modified client can invent them. Echoes are not proof
of anything and must stay cosmetic on the receiving side: no block change,
entity, command or game decision may depend on them. Receiving clients should
check every block action against their own world before showing it and fetch
names and skins only from the official account service.
