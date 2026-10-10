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

Côté jeu, saisir `http://<IP_DU_VPS>:8787` comme adresse du relais et autoriser
explicitement « HTTP distant » pour cet essai.

### 2. HTTPS avec un domaine

1. Créer un enregistrement DNS `A` (et `AAAA` si IPv6) vers l'IP du VPS.
2. Dans `.env` : `NOCTIS_DOMAIN=relay.example.org`, `NOCTIS_PUBLISH=127.0.0.1`.
3. Ouvrir 80 et 443, fermer 8787.
4. `docker compose --profile https up -d --build`
5. `curl https://relay.example.org/v1/info`

Caddy obtient et renouvelle le certificat automatiquement, n'écrit pas de
journal d'accès et remplace tout `X-Forwarded-For` reçu par l'adresse réelle ;
le relais ne fait confiance à cet en-tête que depuis le conteneur Caddy.
Limite connue : le réseau Docker du compose est en IPv4 ; selon la
configuration de Docker, les clients arrivant en IPv6 peuvent apparaître sous
l'adresse de la passerelle Docker et partager une même limite de débit. Activer
IPv6 dans Docker ou placer Caddy sur l'hôte si l'IPv6 compte.

### Exploitation

- Mise à jour : `git pull && docker compose --profile https up -d --build`.
  Le redémarrage efface les sessions : les clients en recréent une.
- Journaux : `docker compose logs relay` (démarrage, erreurs internes ; ni IP
  ni jeton). Journaux Docker limités à 3 × 1 Mio.
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
controls exposure. In the game, use `http://<VPS_IP>:8787` as relay address and
explicitly allow "Remote HTTP" for this test.

### 2. HTTPS with a domain

1. Create a DNS `A` record (and `AAAA` for IPv6) pointing at the VPS.
2. In `.env`: `NOCTIS_DOMAIN=relay.example.org`, `NOCTIS_PUBLISH=127.0.0.1`.
3. Open 80 and 443, close 8787.
4. `docker compose --profile https up -d --build`
5. `curl https://relay.example.org/v1/info`

Caddy obtains and renews the certificate, writes no access log and replaces
any incoming `X-Forwarded-For` with the real peer address; the relay trusts
that header only from the Caddy container. Known limit: the compose network
is IPv4; depending on the Docker setup, IPv6 clients may appear as the Docker
gateway and share one rate-limit bucket. Enable IPv6 in Docker or run Caddy on
the host if IPv6 matters.

### Operations

- Update: `git pull && docker compose --profile https up -d --build`. A restart
  drops all sessions; clients simply create new ones.
- Logs: `docker compose logs relay` (startup and internal errors; no IPs or
  tokens). Docker logs are capped at 3 × 1 MiB.
- The host and OS may keep their own network logs; the operator remains
  responsible for informing players and for legal obligations.
