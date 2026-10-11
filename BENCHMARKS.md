# Benchmarks

Reproduce with:

```sh
python3 bench/bench_spatial.py            # SQLite store only, in process
python3 bench/bench_http.py               # real relay process, closed loop
python3 bench/bench_http.py --clients 2000 --hot 300 --interval 1 --seconds 15
python3 bench/bench_abuse.py --attack none|single|rotate|create|slow --seconds 8
python3 bench/bench_abuse.py --attack rotate --warmup 1.5 --check   # fails if a player is degraded
python3 bench/bench_spatial.py --max-per-cell 64   # density pass with the cell cap
```

`bench_http.py` runs client and server on the same machine and shares one
source address: it **lifts every abuse limit** (per-client and per-prefix
rates, per-network session cap, per-session and relay-wide budgets, sockets
per peer, in-progress requests, per-cell cap) so that it measures capacity,
not the limits. `bench_abuse.py` keeps the **default** limits and only trusts
127.0.0.1 as reverse proxy, so that `X-Forwarded-For` emulates distinct client
networks. Numbers are indicative for one host; they are **not** a promise of
world-scale capacity, nor of protection against network-level DDoS.

## 2026-10-11 — Apple M4 Pro (14 cores), Python 3.12.14, SQLite 3.53.4

Two runs were taken. The first on a lightly loaded machine; the second while
other heavy jobs were running (load average ≈ 25), which mostly inflates the
tail latencies.

### Spatial store (5 000 presences: 4 500 spread over 20 000 × 20 000 blocks in four profiles, surface and caves; 500 in a 64-block hot zone)

| Measure | light load | heavy load |
|---|---|---|
| Query plan | `SEARCH presence USING PRIMARY KEY (realm=? AND profile=? AND cx=? AND cz=? AND cy>? AND cy<?)` | same |
| Presence update (write + neighbours), single thread | 19 400 ops/s | 7 000 ops/s |
| — spread players p50 / p95 | 38 / 49 µs | 43 / 186 µs |
| — hot-zone players p50 / p95 (8 neighbours returned) | 134 / 182 µs | 155 / 734 µs |
| Neighbour query alone p50 / p95 | 26 / 105 µs | 28 / 132 µs |
| Same query as a full scan (no spatial key) p50 / p95 | 316 / 542 µs | 315 / 449 µs |
| Row changes per update | 2.15 | 2.15 |
| SQLite pages | 1.96 MiB (≈ 410 B per player) | same |
| Purge of 5 000 expired players | 14.5 ms (3 batches of 2 000) | 16.3 ms |

The full scan grows linearly with the number of players; the indexed query
depends only on the players in the 5 × 5 horizontal cells around the caller
(with the default 48-block radius and 32-block cells), filtered by height.

Hot-zone density (all players within 64 blocks, same profile), update p50 / p95:

| Players | light load | heavy load |
|---|---|---|
| 50 | 54 / 61 µs | 56 / 71 µs |
| 200 | 79 / 95 µs | 86 / 177 µs |
| 1 000 | 206 / 340 µs | 212 / 385 µs |
| 3 000 | 484 / 672 µs | 517 / 815 µs |

A dense crowd costs more because every candidate in the neighbouring cells is
distance-checked before the `LIMIT`; at 3 000 players in one spot the update
stays under 1 ms. The per-session update interval (1 s by default) bounds how
often that cost is paid.

### HTTP, real relay process on localhost

| Scenario | Throughput | Presence p50 / p95 | Heartbeat p50 / p95 | Server RSS |
|---|---|---|---|---|
| 100 clients, closed loop (light load) | 13 300 req/s | 8.0 / 11.8 ms | 6.7 / 11.1 ms | 25 MiB |
| 1 000 clients, one request every 0.5 s (light load) | 1 980 req/s | 1.1 / 8.1 ms | 0.3 / 2.8 ms | 35 MiB |
| 100 clients, closed loop (heavy load) | 5 800 req/s | 10.5 / 47.5 ms | 9.2 / 44.2 ms | 20 MiB |
| 2 000 clients, one request per second (heavy load) | 1 940 req/s | 4.0 / 90 ms | 0.9 / 28 ms | 39 MiB |

All responses were `200`. In closed loop the latency is queueing: clients send
as fast as the single server thread answers.

## 2026-10-11 — relay 0.2.0 (abuse limits), same machine, heavy load

Taken while other heavy jobs were running (load average ≈ 17–28): tails are
inflated, compare with the heavy-load columns above.

### Default budgets introduced in 0.2.0

| Setting | Default | Before |
|---|---|---|
| `NOCTIS_CREATE_PREFIX_RATE` / `_BURST` (per IPv4 /24 or IPv6 /48) | 0.5/s, 24 | none |
| `NOCTIS_MAX_SESSIONS_PER_NETWORK` (IPv4 or /64) | 16 | none |
| `NOCTIS_SESSION_REQUEST_RATE` / `_BURST` (per session) | 2/s, 20 | none |
| `NOCTIS_GLOBAL_REQUEST_RATE` / `_BURST` (anonymous requests) | 2 000/s, 4 000 | none |
| `NOCTIS_GLOBAL_CREATE_RATE` / `_BURST` | 50/s, 500 | none |
| `NOCTIS_GLOBAL_PRESENCE_RATE` / `_BURST` | 1 000/s, 2 000 | none |
| `NOCTIS_MAX_CONNECTIONS_PER_PEER` (trusted proxies exempt) | 32 | none (only the global socket cap) |
| `NOCTIS_MAX_INFLIGHT_PER_CLIENT` | 8 | none |
| `NOCTIS_PRESENCE_MAX_PER_CELL` | 64 | none |

Per-client request and creation rates are unchanged. `0` disables each new
limit.

### Players under attack (`bench_abuse.py`, 8 s, 200 players heartbeating every second, 64 attacker connections, default limits)

| Attack | Players | Heartbeat p50 / p95 | Attacker obtained | Server RSS |
|---|---|---|---|---|
| none | every heartbeat `200` | 0.55 / 4.5 ms | — | 20 MiB |
| one address flooding `GET /v1/info` (≈ 21 000 attempts/s) | every heartbeat `200` | 2.8 / 21 ms | 79 × `200`, the rest `429` | 21 MiB |
| fresh address on every request | every heartbeat `200`; late creations: 176 × `201`, 470 × `503` | 2.4 / 18 ms | 19 817 × `200`, 181 124 × `503` | 35 MiB |
| session-creation flood, rotating addresses | every heartbeat `200`; late creations: 30 × `201`, 1 372 × `503` | 2.3 / 3.7 ms | 869 sessions, 95 935 × `503` | 46 MiB |
| slow bodies from one address | every heartbeat `200` | — | 109 318 × `429`, 8 sockets cut at body timeout | 25 MiB |

Existing sessions are admitted on their own per-session budget, so anonymous
floods do not starve them. What a rotating attacker **does** obtain is the
relay-wide creation budget: players who try to open a session during the
flood get `503` with `Retry-After` until it stops.

### Spatial store with the `net` column (5 000 presences, 500 in the hot zone)

| Measure | 0.2.0, heavy load |
|---|---|
| Presence update, single thread | 15 600 ops/s |
| — spread / hot-zone players p50 | 43 / 148 µs |
| SQLite pages | ≈ 443 B per player (≈ 410 before: network fingerprint and its index) |
| Purge of 5 000 expired players | 17.2 ms |

Density (all players within 64 blocks), update p50 / p95:

| Attempted | no cell cap | cell cap 64 (admitted) |
|---|---|---|
| 1 000 | — | 216 µs p50 (977 admitted) |
| 3 000 | 535 / 911 µs | 316 / 1 833 µs (1 635 admitted) |

With the cap, a cell holds at most 64 presences, so the neighbour search of a
caller examines at most 5 × 5 × 3 × 64 = 4 800 rows whatever the crowd; the
p95 above is load noise.

### HTTP capacity, limits lifted (`bench_http.py`)

| Scenario | Throughput | Statuses | Server RSS |
|---|---|---|---|
| 100 clients, closed loop | 9 350 req/s | all `200` | 20 MiB |
| 1 000 clients, one request every 0.5 s | 1 900 req/s (presence p50 3.4 ms, p95 163 ms) | all `200` | — |

The extra admission work (fingerprint, session lookup before the body) stays
within the load noise of the earlier heavy-load runs.

### Reading the numbers

- The server is one process and one thread. With a heartbeat every 10 s, a few
  thousand req/s means room for tens of thousands of idle sessions; with
  presence at one update per second, a few thousand active players per relay
  on comparable hardware. A small VPS core is slower than this laptop: measure
  on the target machine with the same scripts.
- Beyond that, the next steps are sharding by realm/profile across processes
  or a different store; this version does not attempt it.

## 2026-10-11 — relay 0.4.0 durable archive (`bench_archive.py`), same machine, load ≈ 13

`python3 bench/bench_archive.py --check`: on-disk archive, WAL,
`synchronous=FULL` for every measured operation (the fill alone uses NORMAL).
120 000 clips spread over 92 days, 5 000 owners (one heavy owner with 1 clip in
50), 30 % in one 96-block square, the rest over 20 000 × 20 000 blocks,
surface and caves. Simulated clock; nothing leaves the machine.

| Measure | Result |
|---|---|
| Fill | 120 000 clips in 16.3 s, 71.9 MB on disk (≈ 600 B per clip) |
| Lookup, hot square, p50 / p99 | 0.30 / 0.46 ms; 2 000 / 2 000 served, 1 593 distinct accounts |
| Lookup, dispersed, p50 / p99 | 0.09 / 0.21 ms; 1 078 / 2 000 served |
| Age of served clips p10 / p50 / p90 | 9 / 47 / 83 days (old visits still appear) |
| Index entries read per lookup | ≤ 800 (8 per cell from a random pivot) |
| Query plans | `clip_cell` (realm, profile, cx, cz, cy, shuffle), `clip_created`, `clip_owner`; no table scan |
| Private deletion of the heavy owner | 2 426 clips in 88 ms, then WAL truncated |
| Expiry purge a month later | 192 batches of ≤ 200 rows in 2.9 s, batch p99 27 ms |

The default purge batch is 200 rows (`NOCTIS_ARCHIVE_PURGE_BATCH`) so that one
pass stays far below the request deadlines of the single event loop.

## 2026-10-11 — relay 0.5.0 zone retention (`bench_archive.py`), same machine, load ≈ 5–7

`python3 bench/bench_archive.py --check`: production retention defaults (zone
quota 4096, 24 clips per zone per UTC day, 92-day floor), on-disk archive, WAL,
`synchronous=FULL` for every measured operation (the fill alone uses NORMAL).
365 simulated days: a busy 96-block square (9 zones) receives its 24 clips per
zone every day, fills after about 171 days and then replaces its own oldest
clips; 40 000 sparse clips are dispersed over 20 000 × 20 000 blocks (surface
and caves, outside the busy zones) and are never replaced, 10 % of them with
the fixed 92-day lifetime of legacy clips. 5 000 owners, one heavy owner with
1 clip in 50. Simulated clock; nothing leaves the machine.

| Measure | Result |
|---|---|
| Fill | 76 864 clips in 19.0 s, 57.3 MB on disk (≈ 750 B per clip) |
| Busy zones | 78 840 / 78 840 offered clips admitted; every zone at 4 096 clips, 171 days of history |
| Admission with replacement, p50 / p99 | 0.14 / 7.5 ms (p99: WAL checkpoints); sparse admission 0.09 / 0.17 ms |
| Lookup, busy square, p50 / p99 | 0.29 / 0.45–0.50 ms over three runs; 2 000 / 2 000 served, 1 586 distinct accounts, ages p10 / p50 / p90 16 / 85 / 152 days |
| Lookup, dispersed, p50 / p99 | 0.06 / 0.17 ms; 583 / 2 000 served, ages p50 165 days |
| Lookup at the oldest sparse clips, p50 / p99 | 0.08 / 0.20 ms; 300 / 300 served, 292 older than 92 days (p50 364 days) |
| Index entries read | ≤ 800 per lookup (8 per cell from a random pivot), ≤ 16 per replacement |
| Query plans | `clip_cell`, `clip_zone`, `clip_expires`, `clip_created`, `clip_owner`, primary keys, `owner_empty`, `cell_empty`; no table scan |
| Private deletion of the heavy owner | 1 510 clips in 72 ms, then WAL truncated |
| Purge of fixed lifetimes | 15 batches of ≤ 200 rows in 0.4 s, batch p99 25 ms; zone-retention clips untouched |

With `--sparse 120000` (156 864 clips, 124 MB, load ≈ 5, check passed):
busy-square lookup p50 / p99 0.37 / 0.55 ms, dispersed 0.14 / 0.29 ms, oldest
sparse 0.13 / 0.32 ms with 283 / 300 served clips older than 92 days; deletion
of the heavy owner 3 127 clips in 170 ms; 45 purge batches, p99 30 ms. Admission
latency includes `fsync`, cheap on this SSD; expect more on a VPS disk. A
sparse clip placed in a busy zone takes a slot of that zone's `cellDaily`
budget like any other owner: the anti-abuse quota then refuses the zone's last
clip of the day, which is expected and not a retention failure, so the
benchmark keeps sparse clips outside the busy zones.
