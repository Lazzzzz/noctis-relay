# noctis-relay

[Français](#français) · [English](#english)

## Français

Relais auto-hébergeable pour **Noctis** : il ouvre des sessions temporaires
pour les joueurs qui ont explicitement accepté la fonction en jeu. Cette
première version ne transmet ni coordonnées, ni skin, ni mouvements, ni blocs :
elle fournit l'accord, le jeton de session, le maintien en vie et la fermeture.
Une capacité `presence` expérimentale, désactivée par défaut, prépare la mise en
relation de joueurs proches (même dimension, même profil de génération, mêmes
coordonnées) ; elle ne promet aucun gameplay.

- Python ≥ 3.11, bibliothèque standard uniquement (aucune dépendance).
- SQLite **en mémoire** : rien n'est écrit sur disque, tout disparaît au
  redémarrage. Voir [PRIVACY.md](PRIVACY.md).
- Un seul processus, une seule boucle asyncio qui possède la connexion SQLite :
  aucune concurrence d'écriture, pas besoin de WAL.
- Protocole : [PROTOCOL.md](PROTOCOL.md). Mesures : [BENCHMARKS.md](BENCHMARKS.md).
  Mise en ligne sur un VPS (OVH ou autre) : [DEPLOY.md](DEPLOY.md).

### Démarrage local

```sh
NOCTIS_OPERATOR_NAME="Mon relais" NOCTIS_OPERATOR_CONTACT="moi@example.org" \
  python3 -m noctis_relay          # écoute sur 127.0.0.1:8787
curl http://127.0.0.1:8787/v1/info
```

Avec Docker : `cp .env.example .env`, éditer l'opérateur, puis
`docker compose up -d --build` (publié seulement sur `127.0.0.1:8787`).

### Tests

```sh
python3 -m unittest discover -s tests -t . -v
python3 bench/bench_spatial.py && python3 bench/bench_http.py
python3 bench/bench_abuse.py --attack rotate   # players under hostile load
```

Les tests lancent un vrai serveur sur un port local éphémère : contrat,
expiration, révocation, droits, corps invalides (`NaN`, booléens, clés en
trop, doublons, tailles), en-têtes, lenteurs, limites de débit, quotas,
concurrence, plan SQLite et exactitude de la recherche spatiale, ainsi que des
scénarios hostiles (`tests/test_abuse.py`) : rotation d'adresses via proxy de
confiance, quotas actifs, libération par expiration ou `DELETE`, jeton rejoué
depuis un autre réseau, sockets et corps lents, budgets globaux, blocage
IP/CIDR, coordonnées extrêmes et zones denses.

### Sécurité, en bref

Corps ≤ 1 Kio, JSON strict, noms d'en-têtes conformes HTTP, en-têtes bornés,
délais d'en-tête/corps/keep-alive, pas de `chunked`, jetons de 256 bits stockés
hachés, aucun journal d'accès, `X-Forwarded-For` ignoré sauf proxy déclaré,
aucune redirection, aucun en-tête CORS, image Docker non-root en lecture seule.

Contre les abus (tout est configurable, voir [.env.example](.env.example) et
[PROTOCOL.md](PROTOCOL.md#errors-and-limits)) : débit et créations par réseau
(IPv4 ou /64 IPv6) et par bloc (/24, /48), au plus 16 sessions actives par
réseau, session rattachée au réseau qui l'utilise (un jeton transmis compte
pour son utilisateur), budget par session, budgets globaux de requêtes, de
créations et de présence (`503` + `Retry-After`) qui ne pénalisent pas les
sessions existantes, sockets par pair direct et requêtes en cours par client
réel derrière le proxy, liste de blocage IP/CIDR de l'opérateur rechargée par
`SIGHUP`, plafond de positions par cellule. Les tables ne contiennent que des
empreintes à clé éphémère, bornées et purgées sans balayage complet. Ce n'est
**pas** une protection DDoS réseau, et la présence n'authentifie ni joueur ni
position (voir [PROTOCOL.md](PROTOCOL.md#experimental-presence)).

## English

Self-hostable relay for **Noctis**: it opens temporary sessions for players who
explicitly opted in from the game. This first version transmits no coordinates,
skins, movements or blocks: it provides consent-gated session tokens,
heartbeats and closing. An experimental `presence` capability, off by default,
prepares matching of nearby players (same dimension, same generation profile,
same coordinates); it promises no gameplay.

- Python ≥ 3.11, standard library only (no dependencies).
- **In-memory** SQLite: nothing is written to disk and everything is gone after
  a restart. See [PRIVACY.md](PRIVACY.md).
- One process, one asyncio loop owning the SQLite connection: no writer
  contention, no WAL needed.
- Protocol: [PROTOCOL.md](PROTOCOL.md). Measurements:
  [BENCHMARKS.md](BENCHMARKS.md). VPS deployment (OVH or other):
  [DEPLOY.md](DEPLOY.md).

### Run locally

```sh
NOCTIS_OPERATOR_NAME="My relay" NOCTIS_OPERATOR_CONTACT="me@example.org" \
  python3 -m noctis_relay          # listens on 127.0.0.1:8787
curl http://127.0.0.1:8787/v1/info
```

With Docker: `cp .env.example .env`, set the operator, then
`docker compose up -d --build` (published on `127.0.0.1:8787` only).

### Tests

```sh
python3 -m unittest discover -s tests -t . -v
python3 bench/bench_spatial.py && python3 bench/bench_http.py
python3 bench/bench_abuse.py --attack rotate   # players under hostile load
```

Tests start a real server on an ephemeral local port and cover the contract,
expiry, revocation, permissions, invalid bodies (`NaN`, booleans, extra or
duplicate keys, sizes), headers, slow clients, rate limits, quotas,
concurrency, the SQLite query plan and spatial-search correctness, plus hostile
scenarios (`tests/test_abuse.py`): address rotation through the trusted proxy,
live-session caps, release by expiry or `DELETE`, tokens replayed from another
network, slow sockets and bodies, relay-wide budgets, IP/CIDR blocking, extreme
coordinates and dense areas.

### Security summary

Bodies ≤ 1 KiB, strict JSON, RFC-valid header names, bounded headers,
header/body/keep-alive deadlines, no chunked encoding, 256-bit tokens stored
hashed, no access logs, `X-Forwarded-For` ignored unless from a declared proxy,
no redirects, no CORS headers, non-root read-only Docker image.

Abuse limits (all configurable, see [.env.example](.env.example) and
[PROTOCOL.md](PROTOCOL.md#errors-and-limits)): request and creation rates per
network (IPv4 or IPv6 /64) and per block (/24, /48), at most 16 live sessions
per network, sessions attributed to the network that uses them (a handed-over
token counts for its user), a per-session budget, relay-wide request, creation
and presence budgets (`503` + `Retry-After`) that never penalise existing
sessions, sockets per direct peer and in-progress requests per real client
behind the proxy, an operator IP/CIDR block list reloaded on `SIGHUP`, and a
cap of positions per cell. Tables only hold ephemeral keyed fingerprints, are
bounded and are pruned without full sweeps. This is **not** network-level DDoS
protection, and presence authenticates neither players nor positions (see
[PROTOCOL.md](PROTOCOL.md#experimental-presence)).

## Configuration

All settings are environment variables; see [.env.example](.env.example).
`NOCTIS_OPERATOR_NAME` and `NOCTIS_OPERATOR_CONTACT` are required. Invalid
values stop the relay at startup.

## License

MIT, see [LICENSE](LICENSE). This repository contains only the relay; the
Noctis game client is a separate, non-free project.
