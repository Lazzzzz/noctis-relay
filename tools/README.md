# Fictional Noctis visitors

`seed_previews.py` publishes eight short, fictional action clips using the
ordinary relay API: looking, walking, running, jumping, mining, building,
soul torches and walking away. The mod recognises their finite test UUIDs and
renders them with its built-in Steve skin. Real player UUIDs continue to use
Minecraft's verified profile and original skin.

Run from the relay repository, with Python 3.11 or later:

```sh
python3 -B tools/seed_previews.py \
  --endpoint https://your-relay.example \
  --profile noctis-t1-3fa32a7adc115836 \
  --x 0.5 --y 100 --z 0.5 \
  --owner-file /private/location/noctis-demo-owner.key
```

Choose coordinates on the **actual surface of the matching Noctis terrain**;
the sample Y above is illustrative. The default profile matches the mod's
shared terrain with uniform family weights. Different terrain profiles remain
isolated. Each clip lasts at most six seconds; its cosmetic traces can last
longer in the client. No blocks, entities or Minecraft saves are changed.

The owner file is created with mode `600` and contains 32 random bytes. Retain
it privately: it derives one owner secret for each fictional account, allowing
removal later. Never put it in Git or distribute it with a modpack. The tool
does not print it, session tokens or request bodies. It never uploads chat.

Network, session, spatial and archive limits still apply. The tool honours
`Retry-After` with bounded retries; seeding can take several minutes. It reports
each successful archival admission, revokes every temporary source session,
and exits with status 2 if sampling or capacity refused any archive. Seeding
never removes other visits: a zone below its quota keeps all its clips, and a
full zone only replaces its own clips older than the announced age floor. Add
`--scene construit` to publish one scene.

Remove these visitors from the same relay with the same private owner file:

```sh
python3 -B tools/seed_previews.py \
  --endpoint https://your-relay.example \
  --owner-file /private/location/noctis-demo-owner.key --remove
```

Clips seeded on relay 0.4.0 keep, after the archive migration of relay 0.5.0,
the fixed 92-day lifetime of their former notice. Replace them under the
current notice with the same owner file, profile and coordinates:

```sh
python3 -B tools/seed_previews.py \
  --endpoint https://your-relay.example \
  --profile noctis-t1-3fa32a7adc115836 \
  --x 0.5 --y 100 --z 0.5 \
  --owner-file /private/location/noctis-demo-owner.key --renew
```

For each scene, `--renew` deletes its earlier clips and then publishes it
again; it exits with status 2 if a scene was deleted but not archived again,
which a later run repairs.

Remote endpoints must use HTTPS; loopback HTTP is available for local tests.
Redirects are refused. The tool requires a relay of privacy revision 5 whose
archive announces no maximum age below 92 days (`maxAgeDays` null or at least
92). Its fictional owners acknowledge revision 5, so their clips follow the
zone quota. It adds no privileged endpoint or administration bypass.

In the mod, `/astro noctis preview publier` explicitly publishes a fictional
scene near the current observer on their approved relay and terrain profile.
`/astro noctis preview retirer` queues private deletion of the fictional scenes
published by that game. Ordinary local preview scenes stay offline.
