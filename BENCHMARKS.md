# Benchmarks

Reproduce with:

```sh
python3 bench/bench_spatial.py            # SQLite store only, in process
python3 bench/bench_http.py               # real relay process, closed loop
python3 bench/bench_http.py --clients 2000 --hot 300 --interval 1 --seconds 15
```

The HTTP benchmark runs client and server on the same machine and shares one
source address (its rate limits are raised for that reason only). Numbers are
indicative for one host; they are **not** a promise of world-scale capacity.

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

### Reading the numbers

- The server is one process and one thread. With a heartbeat every 10 s, a few
  thousand req/s means room for tens of thousands of idle sessions; with
  presence at one update per second, a few thousand active players per relay
  on comparable hardware. A small VPS core is slower than this laptop: measure
  on the target machine with the same scripts.
- Beyond that, the next steps are sharding by realm/profile across processes
  or a different store; this version does not attempt it.
