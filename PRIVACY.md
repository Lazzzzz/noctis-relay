# Privacy notice — revision 1

This describes what the **noctis-relay software** does with data. Each relay is
run by an independent operator whose name and contact are published at
`/v1/info`. The current game client makes no request before consent and shows
these details after connecting; players should read their chosen operator's
notice before enabling it. The operator, not the
software authors, is responsible for running the relay lawfully (for example
under the GDPR in the EU). Using this software does not by itself make a relay
compliant with any regulation.

## Français

**Données de session.** Après votre accord explicite dans Noctis, le jeu ouvre
une session temporaire. Le relais conserve, uniquement en mémoire vive :
l'empreinte SHA-256 d'un jeton aléatoire, une échéance et l'option « présence »
éventuellement demandée. Aucun identifiant de compte, pseudo, UUID, skin,
graine de monde ni adresse IP n'est enregistré dans la base.

**Adresse IP.** Elle est nécessaire au fonctionnement du réseau. Le relais la
garde seulement en mémoire, dans une table bornée de limitation de débit, et ne
l'écrit dans aucun journal. Le relais n'écrit aucun journal d'accès. Un
reverse proxy ou l'hébergeur choisi par l'opérateur peut avoir ses propres
journaux.

**Présence (expérimental, désactivé par défaut).** Seulement si l'opérateur
l'active et si le jeu la demande avec votre accord : une position (dimension,
profil de génération opaque, identifiant aléatoire de chargement de monde,
coordonnées) est partagée avec les autres joueurs volontaires proches, sous un
identifiant opaque différent pour chaque observateur.

**Durée.** Sessions et positions expirent après `retentionSeconds` (30 s par
défaut, 120 s au maximum) sans renouvellement, sont purgées en continu et
disparaissent toutes au redémarrage du relais. Rien n'est écrit sur disque.

**Retrait.** Désactiver l'option dans Noctis ferme la session ; à défaut, elle
expire d'elle-même.

## English

**Session data.** After your explicit consent in Noctis, the game opens a
temporary session. The relay keeps, in memory only: the SHA-256 of a random
token, an expiry time and the optional "presence" flag. No account id, name,
UUID, skin, world seed or IP address is stored in the database.

**IP address.** It is required for networking. The relay keeps it only in
memory, in a bounded rate-limiting table, and never writes it to a log. The
relay writes no access log. A reverse proxy or hosting provider chosen by the
operator may keep its own logs.

**Presence (experimental, off by default).** Only if the operator enables it
and the game requests it with your consent: a position (dimension, opaque
generation profile, random world-load id, coordinates) is shared with other
opted-in players nearby, under an opaque handle that differs per observer.

**Retention.** Sessions and positions expire after `retentionSeconds` (30 s by
default, 120 s maximum) without renewal, are purged continuously and are all
lost when the relay restarts. Nothing is written to disk.

**Withdrawal.** Turning the option off in Noctis closes the session; otherwise
it simply expires.
