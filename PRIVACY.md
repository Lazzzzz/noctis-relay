# Privacy notice — revision 4

This describes what the **noctis-relay software** does with data. Each relay is
run by an independent operator whose name and contact are published at
`/v1/info`. The current game client makes no request before consent and shows
these details after connecting; players should read their chosen operator's
notice before enabling it. The operator, not the
software authors, is responsible for running the relay lawfully (for example
under the GDPR in the EU). Using this software does not by itself make a relay
compliant with any regulation.

Revision 4 (relay 0.4.0) adds the optional **durable archive** of echoes (off
unless the operator sets `NOCTIS_ARCHIVE_PATH`): a sample of clips is kept on
disk for up to 92 days with a hash of a private owner key, and can be deleted
by its owner (below). It also filters insults and slurs out of echo chat.
An operator may also publish fictional demonstration clips containing no real
player data; the mod displays those finite test identities with its built-in skin.
Revision 3 (relay 0.3.0) adds the optional **echoes** capability (off by
default): opted-in players' Minecraft account UUID, position, short movement
clips, block actions and, separately, chat lines (below). Revision 2 (relay
0.2.0) added a keyed, in-memory fingerprint of the client network to each
session and replaced addresses by such fingerprints in the abuse-limit tables.
Revision 1 stored no network information with sessions. A client must ask
again for consent when the revision it was accepted under changes.

## Français

**Données de session.** Après votre accord explicite dans Noctis, le jeu ouvre
une session temporaire. Le relais conserve, uniquement en mémoire vive :
l'empreinte SHA-256 d'un jeton aléatoire, une échéance, les options
(« présence », « échos », « chat ») éventuellement demandées, les dates des
derniers envois et l'empreinte du réseau qui a utilisé la session en dernier
(voir ci-dessous). L'UUID du compte Minecraft n'est reçu qu'avec la capacité
« échos » ; des coordonnées ne le sont qu'avec « présence » ou « échos ». Sans
ces capacités, ni l'un ni les autres ne sont enregistrés. Le pseudo, le skin,
la graine du monde et l'adresse IP en clair ne sont jamais enregistrés dans la
base.

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

**Échos (optionnel, désactivé par défaut).** Seulement si l'opérateur active
`NOCTIS_ECHOES` et si le jeu la demande avec votre accord : au plus un
« clip » par session, remplacé par le suivant. Il contient l'**UUID de votre
compte Minecraft** (sans pseudo, skin ni jeton de connexion), la dimension, un
profil de terrain opaque (jamais la graine), l'identifiant aléatoire de la
partie en cours (le relais n'en garde qu'une empreinte SHA-256 tronquée), une
position d'origine, au plus 6 secondes de mouvements (positions relatives,
orientation, accroupi/sprint/nage/geste) et au plus 8 poses ou casses de blocs
(position relative, identifiant de bloc). Le relais le stocke tel que validé,
re-sérialisé, et le remet une seule fois à chaque autre joueur volontaire
proche (48 blocs par défaut, même dimension et profil, autre partie), sous un identifiant
opaque différent par observateur ; il note quel observateur a reçu quel clip
pour ne pas le resservir. L'observateur reçoit l'UUID : son jeu obtient le
pseudo et le skin auprès du service officiel de Mojang, pas du relais. Le
relais ne vérifie pas l'identité du compte ni la vérité des mouvements :
le jeu traite les échos comme purement décoratifs.

**Chat des échos (optionnel, désactivé par défaut, choix séparé).** Seulement
si l'opérateur active `NOCTIS_ECHO_CHAT` et que vous avez accepté ce choix
distinct : avec un clip, au plus 2 lignes de 64 caractères que vous avez
écrites dans le chat public sur Noctis depuis le clip précédent et après avoir
activé ce choix ; jamais de commandes, messages privés, messages d'autres
joueurs ni historique antérieur. Les liens, répétitions et doublons sont écartés ; une
empreinte tronquée de la dernière ligne reste avec la session pour écarter les
doublons. Les lignes ne sont remises qu'aux observateurs qui ont aussi activé
le chat.

**Archive durable (optionnelle, désactivée par défaut).** Seulement si
l'opérateur définit `NOCTIS_ARCHIVE_PATH` et que le relais l'annonce
(`archive` dans `/v1/info`) : votre jeu envoie alors, avec votre accord aux
échos, une **clé de propriétaire** aléatoire propre à ce relais et à ce compte,
gardée dans un fichier privé de votre jeu et jamais affichée. Le relais n'en
conserve que l'empreinte SHA-256 tronquée. Une partie de vos clips est
gardée **sur disque** : au plus un toutes les 5 minutes et 48 par jour,
24 par jour et par cellule de 32 × 16 × 32 blocs, ni les clips immobiles sans action. Une
entrée contient l'empreinte de la clé, l'UUID du compte, la dimension, le
profil de terrain, l'empreinte de la partie (SHA-256 tronqué de son
identifiant, stable d'un redémarrage à l'autre pour ne jamais vous montrer
vos voisins de partie), la position, la date, les mouvements, les actions et,
si vous avez accepté le chat, ses lignes filtrées. Ni adresse IP, ni empreinte
de réseau, ni jeton. Ces clips peuvent être rejoués à d'autres joueurs
volontaires proches, **même après votre déconnexion**, pendant
`NOCTIS_ARCHIVE_DAYS` (92 jours par défaut) ; chaque observateur ne reçoit un
même clip qu'une fois par session d'observateur (pendant 6 heures). Le fichier est créé avec les
droits 0600, en WAL avec suppression sécurisée des pages libérées.

**Suppression privée.** Refuser les échos en jeu demande la suppression de
**toutes** vos archives sur ce relais ; désactiver le chat demande celle de
leurs lignes de chat. Le jeu envoie la clé à `POST /v1/archive/forget`,
réessaie jusqu'à confirmation (y compris hors de Noctis ou après un
redémarrage), n'ouvre aucune nouvelle session vers ce relais pour ce compte
d'ici là, puis remplace la clé après une suppression complète. Le relais
supprime les clips concernés dans une transaction puis tronque le journal
WAL ; ses sauvegardes éventuelles relèvent de l'opérateur. L'UUID seul ne
permet ni de supprimer, ni de lister des archives : seule la clé le permet.
Changer d'adresse de relais ne transmet jamais la clé de l'ancien relais au
nouveau.

**Filtre du chat.** Les lignes contenant des insultes ou injures françaises ou
anglaises connues (variantes de casse, accents, lettres masquées ou espacées,
« leetspeak ») sont écartées entières, à l'envoi puis de nouveau à la
remise. Ce filtre hors ligne n'est pas une garantie : contexte, autres langues
et contournements créatifs passent.

**Durée.** Sessions et positions expirent après `retentionSeconds` (30 s par
défaut, 120 s au maximum) sans renouvellement, sont purgées en continu et
disparaissent toutes au redémarrage du relais. Un clip expire après
`NOCTIS_ECHO_TTL` (60 s par défaut, 120 s au maximum) et la trace de remise
après la même durée. Sans archive, rien n'est écrit sur disque ; avec, voir
ci-dessus : les clips archivés sont purgés en continu après
`NOCTIS_ARCHIVE_DAYS`. Ce qu'un autre joueur a déjà vu ne peut pas être
repris.

**Retrait.** Désactiver l'option, quitter Noctis, changer de compte ou de relais
ferme la session, ce qui supprime aussitôt son clip, son chat et ses traces de
remise (`DELETE /v1/session` ou `DELETE /v1/echoes`) ; à défaut, tout expire
de soi-même. Les archives se retirent par la suppression privée ci-dessus.

## English

**Session data.** After your explicit consent in Noctis, the game opens a
temporary session. The relay keeps, in memory only: the SHA-256 of a random
token, an expiry time, the optional capabilities requested ("presence",
"echoes", "chat"), the times of the last uploads and the fingerprint of the
network that last used the session (below). The Minecraft account UUID is only
received with the "echoes" capability; coordinates only with "presence" or
"echoes". Without those capabilities, neither is stored. Names, skins, world
seeds and plain IP addresses are never stored in the database.

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

**Echoes (optional, off by default).** Only if the operator enables
`NOCTIS_ECHOES` and the game requests it with your consent: at most one "clip"
per session, replaced by the next one. It holds your **Minecraft account
UUID** (no name, skin or login token), the dimension, an opaque terrain profile
(never the seed), the random id of the running game (the relay keeps only a
truncated SHA-256 of it), an origin position, at most 6 seconds of movement
(relative positions, orientation, crouch/sprint/swim/swing flags) and at most
8 block placements or breaks (relative position, block id). The relay stores
it as validated and re-serialised, and serves it once to each other opted-in
player nearby (48 blocks by default, same dimension and profile, another game) under an
opaque handle that differs per observer; it records which observer received
which clip so as not to serve it again. The observer receives the UUID: their
game fetches the name and skin from Mojang's official service, not from the
relay. The relay verifies neither the account nor the movements; the game
treats echoes as purely cosmetic.

**Echo chat (optional, off by default, separate choice).** Only if the operator
enables `NOCTIS_ECHO_CHAT` and you accepted this distinct choice: at most 2
lines of 64 characters, sent with a clip, that you typed in public chat on
Noctis since the previous clip and after enabling this choice; never
commands, private messages, other players' messages or earlier history. Links, repeats and
duplicates are dropped; a truncated fingerprint of your last line stays with
the session to drop duplicates. Lines are only served to observers who also
enabled chat.

**Durable archive (optional, off by default).** Only if the operator sets
`NOCTIS_ARCHIVE_PATH` and the relay announces it (`archive` in `/v1/info`):
with your echoes consent, your game then sends a random **owner key** specific
to this relay and account, kept in a private file of your game and never
shown. The relay keeps only its truncated SHA-256. Part of your clips is kept
**on disk**: at most one every 5 minutes and 48 a day, 24 a day per 32 × 16 × 32-block
area, and no idle clip without actions. An entry holds the key hash, the
account UUID, the dimension, the terrain profile, the game fingerprint
(truncated SHA-256 of its id, stable across relay restarts so that you are
never shown players of your own game), the position, the date, the movements,
the actions and, if you accepted chat, its filtered lines. No IP address,
network fingerprint or token. These clips may be replayed to other opted-in
players nearby **even after you left**, for `NOCTIS_ARCHIVE_DAYS` (92 days by
default); a given observer session receives a given clip at most once (for
6 hours). The
file is created with mode 0600, in WAL mode with secure deletion of freed
pages.

**Private deletion.** Declining echoes in game requests the deletion of
**all** your archives on this relay; turning chat off requests that of their
chat lines. The game sends the key to `POST /v1/archive/forget`, retries until
confirmed (also outside Noctis or after a restart), opens no new session to
that relay for that account meanwhile, then replaces the key after a full
deletion. The relay deletes the clips in one transaction then truncates its
WAL journal; any backups are the operator's responsibility. The UUID alone
can neither delete nor list archives: only the key can. Changing the relay
address never sends the former relay's key to the new one.

**Chat filter.** Lines containing known French or English insults or slurs
(case, accent, masked or spaced letters and leetspeak variants) are dropped
whole, when uploaded and again when served. This offline filter is not a
guarantee: context, other languages and creative evasion get through.

**Retention.** Sessions and positions expire after `retentionSeconds` (30 s by
default, 120 s maximum) without renewal, are purged continuously and are all
lost when the relay restarts. A clip expires after `NOCTIS_ECHO_TTL` (60 s by
default, 120 s maximum) and its delivery records after the same time. Without
an archive nothing is written to disk; with one, see above: archived clips are
purged continuously after `NOCTIS_ARCHIVE_DAYS`. What another player has
already seen cannot be taken back.

**Withdrawal.** Turning the option off, leaving Noctis, or changing account or
relay closes the session, which immediately deletes its clip, chat lines and
delivery records (`DELETE /v1/session` or `DELETE /v1/echoes`); otherwise
everything simply expires. Archives are removed by the private deletion above.
