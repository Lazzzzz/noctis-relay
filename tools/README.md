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
and exits with status 2 if sampling or capacity refused any archive. Existing
unexpired visits are never evicted to make room. Add `--scene construit` to
publish one scene.

Remove these visitors from the same relay with the same private owner file:

```sh
python3 -B tools/seed_previews.py \
  --endpoint https://your-relay.example \
  --owner-file /private/location/noctis-demo-owner.key --remove
```

Remote endpoints must use HTTPS; loopback HTTP is available for local tests.
Redirects are refused. The tool requires a compatible relay retaining archives
for at least 92 days. It adds no privileged endpoint or administration bypass.

In the mod, `/astro noctis preview publier` explicitly publishes a fictional
scene near the current observer on their approved relay and terrain profile.
`/astro noctis preview retirer` queues private deletion of the fictional scenes
published by that game. Ordinary local preview scenes stay offline.
