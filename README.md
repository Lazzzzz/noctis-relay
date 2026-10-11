# noctis-relay

[Français](#français) · [English](#english)

## Français

Relais auto-hébergeable pour **Noctis** : il ouvre des sessions temporaires
pour les joueurs qui ont explicitement accepté la fonction en jeu. Par défaut,
il ne fournit que le jeton de session, le maintien en vie et la fermeture.
Deux capacités optionnelles, désactivées par défaut, s'activent par
l'opérateur :

- `echoes` (`NOCTIS_ECHOES=1`) : de courts clips de mouvements (UUID du compte
  Minecraft, position, ≤ 6 s de mouvements, ≤ 8 poses/casses de blocs) que les
  joueurs volontaires proches d'**autres** parties rejouent brièvement, de façon
  purement décorative ; sous-capacité `chat` (`NOCTIS_ECHO_CHAT=1`) pour ≤ 2
  lignes de chat public par clip, avec un accord séparé du joueur ;
- archive durable (`NOCTIS_ARCHIVE_PATH`, avec `echoes`) : un échantillon de
  clips gardé sur disque 92 jours, rejoué après le départ de son auteur et
  supprimable par lui seul grâce à une clé de propriétaire privée ; le chat
  des échos passe par un filtre FR/EN hors ligne ;
- `presence` (expérimental) : mise en relation de positions proches, sans
  gameplay promis.

Activer `echoes` change les données traitées : lire [PRIVACY.md](PRIVACY.md)
(révision 4) et publier sa propre notice avant.

- Python ≥ 3.11, bibliothèque standard uniquement (aucune dépendance).
- SQLite **en mémoire** pour les sessions et les clips en direct : tout
  disparaît au redémarrage. Seule l'archive optionnelle est écrite sur disque
  (fichier 0600, WAL ; un fichier inconnu ou abîmé arrête le relais sans
  jamais être réinitialisé). Voir [PRIVACY.md](PRIVACY.md).
- Un seul processus, une seule boucle asyncio qui possède la connexion SQLite :
  aucune concurrence d'écriture.
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
python3 bench/bench_echoes.py                  # échos : index et purge, sans HTTP
python3 bench/bench_archive.py --check         # archive : 120 000 clips, recherche, oubli, purge
NOCTIS_ECHOES=1 NOCTIS_ECHO_CHAT=1 NOCTIS_CREATE_BURST=8 python3 -m noctis_relay &
python3 bench/smoke_echoes.py                  # échos de bout en bout sur HTTP
```

Les tests lancent un vrai serveur sur un port local éphémère : contrat,
expiration, révocation, droits, corps invalides (`NaN`, booléens, clés en
trop, doublons, tailles), en-têtes, lenteurs, limites de débit, quotas,
concurrence, plan SQLite et exactitude de la recherche spatiale, ainsi que des
scénarios hostiles (`tests/test_abuse.py`) : rotation d'adresses via proxy de
confiance, quotas actifs, libération par expiration ou `DELETE`, jeton rejoué
depuis un autre réseau, sockets et corps lents, budgets globaux, blocage
IP/CIDR, coordonnées extrêmes et zones denses. `tests/test_echoes.py` couvre
les clips : schéma strict, téléportations et actions hors de portée refusées,
textes de chat dangereux, exclusion de sa propre session et de la même partie,
remise unique par observateur, chat réservé aux sessions qui l'ont, retrait,
expiration de la session source, cadences, taille des corps et plafond par
cellule.

### Sécurité, en bref

Corps ≤ 1 Kio (≤ 6 Kio pour l'envoi d'un clip d'écho), JSON strict, noms d'en-têtes conformes HTTP, en-têtes bornés,
délais d'en-tête/corps/keep-alive, pas de `chunked`, jetons de 256 bits stockés
hachés, aucun journal d'accès, `X-Forwarded-For` ignoré sauf proxy déclaré,
aucune redirection, aucun en-tête CORS, image Docker non-root en lecture seule.

Contre les abus (tout est configurable, voir [.env.example](.env.example) et
[PROTOCOL.md](PROTOCOL.md#errors-and-limits)) : débit et créations par réseau
(IPv4 ou /64 IPv6) et par bloc (/24, /48), au plus 16 sessions actives par
réseau, session rattachée au réseau qui l'utilise (un jeton transmis compte
pour son utilisateur), budget par session, budgets globaux de requêtes, de
créations, de présence et d'échos (`503` + `Retry-After`) qui ne pénalisent pas les
sessions existantes, sockets par pair direct et requêtes en cours par client
réel derrière le proxy, liste de blocage IP/CIDR de l'opérateur rechargée par
`SIGHUP`, plafonds de positions et de clips par cellule, cadences minimales
d'envoi et de recherche d'échos par session. Les tables ne contiennent que des
empreintes à clé éphémère, bornées et purgées sans balayage complet. Ce n'est
**pas** une protection DDoS réseau, et ni la présence ni les échos
n'authentifient un compte, une position ou un mouvement (voir
[PROTOCOL.md](PROTOCOL.md#echoes)) : les clients doivent les traiter comme des
données décoratives non fiables.

## English

Self-hostable relay for **Noctis**: it opens temporary sessions for players who
explicitly opted in from the game. By default it only provides consent-gated
session tokens, heartbeats and closing. Two optional capabilities, off by
default, are enabled by the operator:

- `echoes` (`NOCTIS_ECHOES=1`): short movement clips (Minecraft account UUID,
  position, ≤ 6 s of movement, ≤ 8 block placements/breaks) that opted-in
  players of **other** games nearby briefly replay, purely cosmetically; a
  `chat` sub-capability (`NOCTIS_ECHO_CHAT=1`) carries ≤ 2 public chat lines
  per clip under a separate player choice;
- durable archive (`NOCTIS_ARCHIVE_PATH`, with `echoes`): a sample of clips
  kept on disk for 92 days, replayed after its author left and deletable by
  them alone with a private owner key; echo chat goes through an offline
  FR/EN filter;
- `presence` (experimental): matching of nearby positions, with no gameplay
  promised.

Enabling `echoes` changes the data you handle: read [PRIVACY.md](PRIVACY.md)
(revision 4) and publish your own notice first.

- Python ≥ 3.11, standard library only (no dependencies).
- **In-memory** SQLite for sessions and live clips: all gone after a restart.
  Only the optional archive is written to disk (mode 0600, WAL; an unknown or
  damaged file stops the relay and is never reset). See
  [PRIVACY.md](PRIVACY.md).
- One process, one asyncio loop owning the SQLite connection: no writer
  contention.
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
python3 bench/bench_echoes.py                  # echoes: index and purge, no HTTP
python3 bench/bench_archive.py --check         # archive: 120,000 clips, lookups, forget, purge
NOCTIS_ECHOES=1 NOCTIS_ECHO_CHAT=1 NOCTIS_CREATE_BURST=8 python3 -m noctis_relay &
python3 bench/smoke_echoes.py                  # echoes end to end over HTTP
```

Tests start a real server on an ephemeral local port and cover the contract,
expiry, revocation, permissions, invalid bodies (`NaN`, booleans, extra or
duplicate keys, sizes), headers, slow clients, rate limits, quotas,
concurrency, the SQLite query plan and spatial-search correctness, plus hostile
scenarios (`tests/test_abuse.py`): address rotation through the trusted proxy,
live-session caps, release by expiry or `DELETE`, tokens replayed from another
network, slow sockets and bodies, relay-wide budgets, IP/CIDR blocking, extreme
coordinates and dense areas. `tests/test_echoes.py` covers clips: strict
schema, teleports and out-of-reach actions refused, unsafe chat text,
exclusion of one's own session and of the same game, single delivery per
observer, chat only for sessions holding it, withdrawal, expiry of the source
session, pacing, body sizes and the per-cell cap.

### Security summary

Bodies ≤ 1 KiB (≤ 6 KiB for an echo clip upload), strict JSON, RFC-valid header names, bounded headers,
header/body/keep-alive deadlines, no chunked encoding, 256-bit tokens stored
hashed, no access logs, `X-Forwarded-For` ignored unless from a declared proxy,
no redirects, no CORS headers, non-root read-only Docker image.

Abuse limits (all configurable, see [.env.example](.env.example) and
[PROTOCOL.md](PROTOCOL.md#errors-and-limits)): request and creation rates per
network (IPv4 or IPv6 /64) and per block (/24, /48), at most 16 live sessions
per network, sessions attributed to the network that uses them (a handed-over
token counts for its user), a per-session budget, relay-wide request, creation,
presence and echo budgets (`503` + `Retry-After`) that never penalise existing
sessions, sockets per direct peer and in-progress requests per real client
behind the proxy, an operator IP/CIDR block list reloaded on `SIGHUP`, caps of
positions and clips per cell, and minimum per-session intervals between echo
uploads and lookups. Tables only hold ephemeral keyed fingerprints, are
bounded and are pruned without full sweeps. This is **not** network-level DDoS
protection, and neither presence nor echoes authenticate an account, a
position or a movement (see [PROTOCOL.md](PROTOCOL.md#echoes)): clients must
treat them as untrusted, cosmetic data.

## Configuration

All settings are environment variables; see [.env.example](.env.example).
`NOCTIS_OPERATOR_NAME` and `NOCTIS_OPERATOR_CONTACT` are required. Invalid
values stop the relay at startup.

## License

For removable fictional action clips using the ordinary API, see
[the demonstration tool](tools/README.md).

MIT, see [LICENSE](LICENSE). This repository contains only the relay; the
Noctis game client is a separate, non-free project.
