# Privacy notice — revision 2

This describes what the **noctis-relay software** does with data. Each relay is
run by an independent operator whose name and contact are published at
`/v1/info`. The current game client makes no request before consent and shows
these details after connecting; players should read their chosen operator's
notice before enabling it. The operator, not the
software authors, is responsible for running the relay lawfully (for example
under the GDPR in the EU). Using this software does not by itself make a relay
compliant with any regulation.

Revision 2 (relay 0.2.0) adds a keyed, in-memory fingerprint of the client
network to each session and replaces addresses by such fingerprints in the
abuse-limit tables. Revision 1 stored no network information with sessions.

## Français

**Données de session.** Après votre accord explicite dans Noctis, le jeu ouvre
une session temporaire. Le relais conserve, uniquement en mémoire vive :
l'empreinte SHA-256 d'un jeton aléatoire, une échéance, l'option « présence »
éventuellement demandée et l'empreinte du réseau qui a utilisé la session en
dernier (voir ci-dessous). Aucun identifiant de compte, pseudo, UUID, skin,
graine de monde ni adresse IP n'est enregistré dans la base.

**Adresse IP.** Elle est nécessaire au fonctionnement du réseau. Le relais en
dérive le « réseau client » (l'adresse IPv4, ou le préfixe /64 d'une adresse
IPv6) et, pour limiter les abus, n'en garde qu'une **empreinte à clé** (BLAKE2b
tronqué à 128 bits) : la clé est tirée au hasard au démarrage, ne quitte jamais
la mémoire et disparaît au redémarrage. Cette empreinte sert à plafonner les
sessions actives et le débit par réseau. Elle est associée à la session tant
que celle-ci vit, est remplacée — sans historique — quand la session est
utilisée depuis un autre réseau, et disparaît avec la session. Les tables de
débit et de connexions en cours, bornées, contiennent les mêmes empreintes
(ainsi que celle du bloc IPv4 /24 ou IPv6 /48 lors d'une création de session).
Les compteurs de débit restent jusqu’à éviction LRU ou redémarrage ; les
compteurs de connexions sont retirés à leur libération. L'adresse en clair n'existe que le temps de traiter
une connexion ou une requête, pour la liste de blocage de l'opérateur ; elle
n'est écrite ni sur disque ni dans aucun journal. Limite : quelqu'un qui
lirait la mémoire du processus en fonctionnement pourrait, avec la clé,
retrouver par essais l'adresse IPv4 correspondant à une empreinte. Le relais
n'écrit aucun journal d'accès. Un reverse proxy ou l'hébergeur choisi par
l'opérateur peut avoir ses propres journaux.

**Présence (expérimental, désactivé par défaut).** Seulement si l'opérateur
l'active et si le jeu la demande avec votre accord : une position (dimension,
profil de génération opaque, identifiant aléatoire de chargement de monde,
coordonnées) est partagée avec les autres joueurs volontaires proches, sous un
identifiant opaque différent pour chaque observateur. Ces données sont
déclarées par le jeu : le relais ne peut pas vérifier qu'elles sont vraies, et
un client modifié peut en inventer.

**Durée.** Sessions et positions expirent après `retentionSeconds` (30 s par
défaut, 120 s au maximum) sans renouvellement, sont purgées en continu et
disparaissent toutes au redémarrage du relais. Rien n'est écrit sur disque.

**Retrait.** Désactiver l'option dans Noctis ferme la session ; à défaut, elle
expire d'elle-même.

## English

**Session data.** After your explicit consent in Noctis, the game opens a
temporary session. The relay keeps, in memory only: the SHA-256 of a random
token, an expiry time, the optional "presence" flag and the fingerprint of the
network that last used the session (below). No account id, name, UUID, skin,
world seed or IP address is stored in the database.

**IP address.** It is required for networking. The relay derives the "client
network" from it (the IPv4 address, or the /64 prefix of an IPv6 address) and,
to limit abuse, keeps only a **keyed fingerprint** of it (BLAKE2b truncated to
128 bits): the key is random at startup, never leaves memory and is gone after
a restart. The fingerprint caps live sessions and request rates per network.
It is attached to the session while the session lives, is overwritten —
without history — when the session is used from another network, and
disappears with the session. The bounded rate and in-progress tables hold the
same fingerprints (plus that of the IPv4 /24 or IPv6 /48 block when a session
is created). Rate entries remain until LRU eviction or restart; concurrency entries
are removed when released. The plain address only exists while a
connection or request is handled, for the operator's block list; it is never
written to disk or to any log. Limit: someone able to read the running
process memory could use the key to recover by trial the IPv4 address behind
a fingerprint. The relay writes no access log. A reverse proxy or hosting
provider chosen by the operator may keep its own logs.

**Presence (experimental, off by default).** Only if the operator enables it
and the game requests it with your consent: a position (dimension, opaque
generation profile, random world-load id, coordinates) is shared with other
opted-in players nearby, under an opaque handle that differs per observer.
These values are declared by the game: the relay cannot verify them and a
modified client can invent them.

**Retention.** Sessions and positions expire after `retentionSeconds` (30 s by
default, 120 s maximum) without renewal, are purged continuously and are all
lost when the relay restarts. Nothing is written to disk.

**Withdrawal.** Turning the option off in Noctis closes the session; otherwise
it simply expires.
