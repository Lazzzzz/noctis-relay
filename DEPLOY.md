# Deploy on a VPS (OVH or any Linux host)

[Français](#français) · [English](#english)

## Français

Prérequis : un VPS Linux récent (Debian/Ubuntu), Docker Engine avec le plugin
Compose, ce dépôt cloné, puis `cp .env.example .env` et l'opérateur renseigné.

### 1. Essai temporaire par IP, en HTTP

À réserver aux essais : le jeton circule en clair.

```sh
# .env : NOCTIS_PUBLISH=0.0.0.0
docker compose up -d --build
curl http://<IP_DU_VPS>:8787/healthz
```

Ouvrir le port 8787 dans le pare-feu du VPS (et dans le pare-feu réseau OVH
s'il est activé), le refermer après l'essai, puis remettre
`NOCTIS_PUBLISH=127.0.0.1`. Docker publie ses ports avant les règles `ufw` :
c'est `NOCTIS_PUBLISH` qui contrôle réellement l'exposition.

Dans `config/astralyth-noctis-client.toml`, définir
`relayUrl = "http://<IP_DU_VPS>:8787"` et `allowInsecureHttp = true` pour cet
essai. L’adresse et HTTP ne sont pas modifiables en jeu.

### 2. HTTPS avec un domaine

1. Créer un enregistrement DNS `A` (et `AAAA` si IPv6) vers l'IP du VPS.
2. Dans `.env` : `NOCTIS_DOMAIN=relay.example.org`, `NOCTIS_PUBLISH=127.0.0.1`.
3. Ouvrir 80 et 443, fermer 8787.
4. `docker compose --profile https up -d --build`
5. `curl https://relay.example.org/v1/info`

Caddy obtient et renouvelle le certificat automatiquement, n'écrit pas de
journal d'accès et remplace tout `X-Forwarded-For` reçu par l'adresse réelle ;
le relais ne fait confiance à cet en-tête que depuis le conteneur Caddy.

**IPv6, à vérifier avant d'ouvrir le service.** Le réseau Docker du compose est
en IPv4 ; selon la configuration de Docker (`userland-proxy`), les clients
arrivant en IPv6 peuvent apparaître sous l'adresse de la passerelle Docker. Ils
partageraient alors un seul « réseau » : même débit et, surtout, au plus
`NOCTIS_MAX_SESSIONS_PER_NETWORK` (16) sessions actives pour tous les joueurs
IPv6. Solutions : activer IPv6 dans Docker, placer Caddy sur l'hôte, ou ne pas
publier d'enregistrement `AAAA`. Contrôle : ouvrir deux sessions depuis deux
accès IPv6 différents puis vérifier, avec un réglage temporaire
`NOCTIS_MAX_SESSIONS_PER_NETWORK=1`, que la seconde est acceptée.

### Limites contre les abus

Les valeurs par défaut (voir `.env.example` et la section *Errors and limits*
de [PROTOCOL.md](PROTOCOL.md#errors-and-limits)) visent un relais
communautaire : 16 sessions actives par réseau (IPv4 ou /64 IPv6), budgets
globaux de 2 000 requêtes, 50 créations et 1 000 positions par seconde, 32
sockets par pair direct, 8 requêtes en cours par client réel. Derrière un NAT
d'établissement ou un CGNAT, relever `NOCTIS_MAX_SESSIONS_PER_NETWORK`,
`NOCTIS_REQUEST_RATE` et `NOCTIS_CREATE_RATE`. Tout réglage à `0` désactive la
limite correspondante. Les réponses `429`/`503` portent `Retry-After`.

**Blocage par l'opérateur.** `NOCTIS_DENY_NETWORKS` (liste d'adresses ou CIDR)
dans `.env`, appliqué au redémarrage, ou un fichier rechargeable sans
redémarrage. Pour le fichier, ajouter au service `relay` du compose :

```yaml
    volumes:
      - ./deploy/operator-config:/etc/noctis:ro
```

puis `NOCTIS_DENY_FILE=/etc/noctis/deny.txt` dans `.env` (une entrée par ligne,
`#` pour commenter ; créer `deploy/operator-config/deny.txt` avant le
démarrage). Le dossier est monté pour que le remplacement du fichier par un
éditeur reste visible dans le conteneur.
Après modification : `docker compose kill -s HUP relay`. Un fichier invalide
est refusé et l'ancienne liste reste active ; le journal indique seulement le
nombre de réseaux chargés ou le numéro de ligne fautif, jamais les adresses.
Un pair direct bloqué est coupé sans réponse ; un client bloqué derrière Caddy
reçoit `403 forbidden`. Il n'existe aucune API d'administration.

**Ce que le relais ne fait pas.** Ces limites protègent le processus et les
sessions existantes contre un client ou un petit groupe malveillant ; elles ne
constituent **pas** une protection DDoS réseau. Un attaquant disposant de
nombreux réseaux peut épuiser les budgets globaux de création (les nouvelles
sessions reçoivent alors `503`) ou saturer la bande passante du VPS. Le
filtrage volumétrique relève de l'hébergeur (anti-DDoS OVH, pare-feu réseau)
ou d'un service en amont.

**Caddy (recommandation non validée ici).** Pour couper plus tôt les
connexions lentes côté Caddy, on peut placer en tête de
`deploy/Caddyfile` un bloc d'options global
`{ servers { timeouts { read_header 5s read_body 10s idle 60s } } }`. Ce réglage n'a pas été testé avec ce dépôt ; le relais applique
de toute façon ses propres délais et limites par client réel.

### Archive durable (optionnelle)

À activer seulement après avoir publié la notice correspondante
([PRIVACY.md](PRIVACY.md), révision 5).

```sh
install -d -m 700 -o 10001 -g 10001 ./data   # seul chemin inscriptible du conteneur
# .env : NOCTIS_ECHOES=1  NOCTIS_ARCHIVE_PATH=/data/archive.db
docker compose up -d --build
```

Le conteneur reste en lecture seule et non root (uid 10001) ; `./data` est
monté sur `/data` (`deploy/compose.traefik.yaml` monte
`../data` de même). Le fichier est créé en 0600 avec ses compagnons
`-wal`/`-shm`, ignorés par Git. Un fichier au schéma inconnu ou illisible
arrête le relais (code 2) : il n'est **jamais** réinitialisé ; le déplacer à
la main pour repartir d'une archive vide.

Rétention par zone (révision 5) : une zone de `NOCTIS_CELL_SIZE` ×
`NOCTIS_CELL_HEIGHT` × `NOCTIS_CELL_SIZE` blocs (32 × 16 × 32) garde au plus
`NOCTIS_ARCHIVE_CELL_MAX` clips (4096). Une zone pleine remplace ses propres
clips d'au moins `NOCTIS_ARCHIVE_FLOOR_DAYS` jours (92), les plus anciens
d'abord, sinon refuse le nouveau ; une zone calme garde ses clips sans limite
de durée, sauf `NOCTIS_ARCHIVE_MAX_DAYS` (0 = aucune, sinon ≥ le plancher).
Plafonds globaux par défaut : 4 millions de clips et 16 Gio
(`NOCTIS_ARCHIVE_MAX_CLIPS`, `NOCTIS_ARCHIVE_MAX_MB`) ; atteints, ils refusent
les nouveaux clips sans jamais effacer une autre zone. Un quota inférieur à
`NOCTIS_ARCHIVE_CELL_DAILY` × plancher (24 × 92 = 2208) est accepté mais
signalé au démarrage : une zone fréquentée refuserait alors des clips jusqu'à
ce que les siens atteignent le plancher. `NOCTIS_ARCHIVE_DAYS` (92) n'est plus
que la durée fixe des clips migrés ou envoyés sans accord de la révision 5.
Les sauvegardes de `./data` conservent des données que les joueurs ont pu
supprimer : les chiffrer et ne les garder que le temps de leur rotation.

**Mise à jour 0.4.0 → 0.5.0.** Publier d'abord la notice révision 5 et un
client qui la présente. Arrêter le relais (`docker compose stop relay`) et
copier `./data/archive.db` (avec `-wal`/`-shm` s'ils existent) hors de
`./data`, en 0600 : 0.5.0 migre le fichier au schéma 2 en une transaction au
démarrage, et 0.4.0 refuse ensuite ce fichier ; revenir en arrière demande
cette copie. Les clips existants gardent leur échéance de création + 92 jours.
Pour garder les visiteurs fictifs au-delà, les renouveler après le démarrage
avec `tools/seed_previews.py --renew` (voir [tools/README.md](tools/README.md)).
Les clips peuvent maintenant porter l'équipement visible (`gear`, désactivable
par `NOCTIS_ECHO_GEAR=0`) ; le corps d'envoi passe à 16 Kio
(`NOCTIS_ECHO_MAX_BODY_BYTES=16384`, maximum).

### Exploitation

- Mise à jour : `git pull && docker compose --profile https up -d --build`.
  Le redémarrage efface les sessions : les clients en recréent une.
- Journaux : `docker compose logs relay` (démarrage, erreurs internes,
  rechargement de la liste de blocage ; ni IP ni jeton). Journaux Docker
  limités à 3 × 1 Mio.
- L'hébergeur et le système peuvent conserver leurs propres journaux réseau ;
  l'opérateur reste responsable de son information des joueurs et de ses
  obligations légales.

## English

Requirements: a recent Linux VPS (Debian/Ubuntu), Docker Engine with the
Compose plugin, this repository cloned, then `cp .env.example .env` with the
operator filled in.

### 1. Temporary plain-HTTP test by IP

Tests only: the token travels in clear text.

```sh
# .env: NOCTIS_PUBLISH=0.0.0.0
docker compose up -d --build
curl http://<VPS_IP>:8787/healthz
```

Open port 8787 in the VPS firewall (and in the OVH network firewall if
enabled), close it after the test and set `NOCTIS_PUBLISH=127.0.0.1` again.
Docker publishes ports ahead of `ufw` rules: `NOCTIS_PUBLISH` is what actually
controls exposure. In `config/astralyth-noctis-client.toml`, set
`relayUrl = "http://<VPS_IP>:8787"` and `allowInsecureHttp = true` for this
test. Neither setting is editable in game.

### 2. HTTPS with a domain

1. Create a DNS `A` record (and `AAAA` for IPv6) pointing at the VPS.
2. In `.env`: `NOCTIS_DOMAIN=relay.example.org`, `NOCTIS_PUBLISH=127.0.0.1`.
3. Open 80 and 443, close 8787.
4. `docker compose --profile https up -d --build`
5. `curl https://relay.example.org/v1/info`

Caddy obtains and renews the certificate, writes no access log and replaces
any incoming `X-Forwarded-For` with the real peer address; the relay trusts
that header only from the Caddy container. Known limit: the compose network
is IPv4.

**IPv6, check before opening the service.** Depending on the Docker setup
(`userland-proxy`), IPv6 clients may appear as the Docker gateway. They would
then share a single "network": one rate budget and, above all, at most
`NOCTIS_MAX_SESSIONS_PER_NETWORK` (16) live sessions for every IPv6 player.
Fixes: enable IPv6 in Docker, run Caddy on the host, or do not publish an
`AAAA` record. Check: with a temporary `NOCTIS_MAX_SESSIONS_PER_NETWORK=1`,
open sessions from two different IPv6 accesses and verify the second one is
accepted.

### Abuse limits

Defaults (see `.env.example` and *Errors and limits* in
[PROTOCOL.md](PROTOCOL.md#errors-and-limits)) target a community relay: 16 live
sessions per network (IPv4 or IPv6 /64), relay-wide budgets of 2 000 requests,
50 creations and 1 000 presence updates per second, 32 sockets per direct peer,
8 in-progress requests per real client. Behind a campus NAT or CGNAT, raise
`NOCTIS_MAX_SESSIONS_PER_NETWORK`, `NOCTIS_REQUEST_RATE` and
`NOCTIS_CREATE_RATE`. Setting a limit to `0` disables it. `429`/`503` answers
carry `Retry-After`.

**Operator blocking.** `NOCTIS_DENY_NETWORKS` (addresses or CIDRs) in `.env`,
applied at restart, or a file reloadable without restart. For the file, add to
the compose `relay` service:

```yaml
    volumes:
      - ./deploy/operator-config:/etc/noctis:ro
```

then `NOCTIS_DENY_FILE=/etc/noctis/deny.txt` in `.env` (one entry per line,
`#` for comments; create `deploy/operator-config/deny.txt` before startup).
Mount the directory so replacing the file with an editor remains visible
inside the container. After editing
it: `docker compose kill -s HUP relay`. An invalid file is rejected and the
previous list stays active; the log only shows how many networks were loaded
or the faulty line number, never addresses. A blocked direct peer is dropped
without an answer; a blocked client behind Caddy gets `403 forbidden`. There is
no administration API.

**What the relay does not do.** These limits protect the process and existing
sessions against one malicious client or a small group; they are **not**
network-level DDoS protection. An attacker with many networks can exhaust the
relay-wide creation budget (new sessions then get `503`) or saturate the VPS
bandwidth. Volumetric filtering belongs to the host (OVH anti-DDoS, network
firewall) or an upstream service.

**Caddy (recommendation, not validated here).** To drop slow connections
earlier at Caddy, a global options block can be put at the top of
`deploy/Caddyfile`: `{ servers { timeouts { read_header 5s read_body 10s idle 60s } } }`. This setting
was not tested with this repository; the relay applies its own deadlines and
per-real-client limits regardless.

### Durable archive (optional)

Enable it only after publishing the matching notice
([PRIVACY.md](PRIVACY.md), revision 5).

```sh
install -d -m 700 -o 10001 -g 10001 ./data   # the container's only writable path
# .env: NOCTIS_ECHOES=1  NOCTIS_ARCHIVE_PATH=/data/archive.db
docker compose up -d --build
```

The container stays read-only and non-root (uid 10001); `./data` is mounted on
`/data` (`deploy/compose.traefik.yaml` mounts
`../data` the same way).
The file is created with mode 0600 together with its `-wal`/`-shm` companions,
which Git ignores. A file with an unknown schema or that cannot be read stops
the relay (exit code 2): it is **never** reset; move it away by hand to start
an empty archive.

Zone retention (revision 5): a zone of `NOCTIS_CELL_SIZE` ×
`NOCTIS_CELL_HEIGHT` × `NOCTIS_CELL_SIZE` blocks (32 × 16 × 32) keeps at most
`NOCTIS_ARCHIVE_CELL_MAX` clips (4096). A full zone replaces its own clips at
least `NOCTIS_ARCHIVE_FLOOR_DAYS` days old (92), oldest first, otherwise
refuses the new one; a quiet zone keeps its clips without time limit, unless
`NOCTIS_ARCHIVE_MAX_DAYS` is set (0 = none, otherwise ≥ the floor). Default
relay-wide caps: 4 million clips and 16 GiB (`NOCTIS_ARCHIVE_MAX_CLIPS`,
`NOCTIS_ARCHIVE_MAX_MB`); once reached they refuse new clips and never erase
another zone. A quota below `NOCTIS_ARCHIVE_CELL_DAILY` × floor (24 × 92 =
2208) is accepted with a startup warning: a busy zone would then refuse clips
until its own reach the floor. `NOCTIS_ARCHIVE_DAYS` (92) is now only the
fixed lifetime of migrated clips and of clips sent without the revision 5
acknowledgement. Backups of `./data` keep data players may have deleted:
encrypt them and keep them only for their rotation period.

**Upgrading 0.4.0 → 0.5.0.** First publish the revision 5 notice and a client
that shows it. Stop the relay (`docker compose stop relay`) and copy
`./data/archive.db` (with `-wal`/`-shm` if present) outside `./data`, mode
0600: 0.5.0 migrates the file to schema 2 in one transaction at startup, and
0.4.0 then refuses it; going back needs that copy. Existing clips keep their
expiry of creation + 92 days. To keep fictional visitors beyond it, renew them
after startup with `tools/seed_previews.py --renew` (see
[tools/README.md](tools/README.md)). Clips may now carry visible equipment
(`gear`, turned off by `NOCTIS_ECHO_GEAR=0`); the upload body grows to 16 KiB
(`NOCTIS_ECHO_MAX_BODY_BYTES=16384`, the maximum).

### Operations

- Update: `git pull && docker compose --profile https up -d --build`. A restart
  drops all sessions; clients simply create new ones.
- Logs: `docker compose logs relay` (startup, internal errors, block-list
  reloads; no IPs or tokens). Docker logs are capped at 3 × 1 MiB.
- The host and OS may keep their own network logs; the operator remains
  responsible for informing players and for legal obligations.

## Existing Traefik / Coolify proxy

When ports 80/443 already belong to Traefik, use
[deploy/compose.traefik.yaml](deploy/compose.traefik.yaml) rather than starting
a second HTTPS proxy. This template is based on the official OVH deployment.
It does not reconfigure or restart the existing proxy.

1. Fill `.env` with the operator and `NOCTIS_TRUSTED_PROXIES` set to the
   **exact address/CIDR** of the proxy on the chosen Docker network. Set
   `NOCTIS_PROXY_NETWORK` if its name differs from `proxy-network`.
2. Create `deploy/config/deny-networks.txt` from
   [the empty example](deploy/deny-networks.example.txt). Keep actual block
   rules out of Git.
3. `docker compose --env-file .env -f deploy/compose.traefik.yaml up -d --build`
4. Adapt [traefik.dynamic.example.yaml](deploy/traefik.dynamic.example.yaml):
   domain, entrypoint names, certificate resolver and backend container name.
   Install this **new** file in the proxy’s watched dynamic directory.
5. Verify `/healthz` and `/v1/info` with certificate verification enabled, then
   set `relayUrl` in the client configuration.

The template binds the application port to loopback, caps the relay at one
CPU/256 MiB, and mounts the block-list directory read-only. HTTP redirects to
HTTPS; the existing proxy obtains and renews the certificate. Check its logging
policy and real-client forwarding before public use. The official service uses
`NOCTIS_SESSION_BINDING=strict` and leaves presence disabled.
