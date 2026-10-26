# citywalker-seeds

Public repo. Region seeds for CityWalker — each seed divides a city into
walkable regions (districts, boroughs, clusters) so the app can show which
parts the user has covered.

The Android app downloads `region_seeds.json` from this repo's `main` branch
every 24 hours and caches it, with a bundled fallback. **Pushing to `main`
ships to every user on their next refresh — there is no staging.**

## Layout

- `cities/<slug>.json` — per-city source of truth, hand-editable.
- `region_seeds.json` — built artifact assembled by `build.py`. Never edit
  by hand.
- `VERSION` — bumped by the generator; CI checks it's in sync with the
  built file.
- `previews/<slug>.html` — Leaflet preview for each city, the main review
  artifact for PRs.
- `seed_generator.py` — the main tool. See README for full flag list.
- `build.py` — validates cities/*.json and rebuilds region_seeds.json.
- `simplify_outlines.py` — thins outlines for the built file (see below).

## Common commands

```bash
python3 seed_generator.py "Mainz" DE          # OSM, default
python3 seed_generator.py "Tokyo" JP --admin-level 7
python3 seed_generator.py "City" CC --dry-run
venv/bin/python3 build.py --check              # what CI runs (needs shapely)
venv/bin/python3 build.py                      # rebuild region_seeds.json
```

## Invariants

- **A city's circle covers all of its outlines.** The apps test
  `centerLat`/`centerLng`/`radiusKm` first and the district outlines second,
  so an outline point outside the circle can never match by coordinate.
  Leave the three fields as `seed_generator.py` writes them. Circles of
  neighbouring cities may overlap: the outline test picks the city. (The
  old "no overlapping radii" rule was retired on 2026-10-13. It dated from
  when the circle was the only test, and shrinking circles to obey it cut
  off real city area.) Checked by `build.py`.
- **Outlines of two cities never overlap.** Where they do, the apps take
  whichever city comes first in the catalog. Checked by `build.py`, which
  needs shapely for it: run it as `venv/bin/python3 build.py`.
- **Only seed too_large cities.** Cities that clean-download without
  hitting the Overpass size limit do not need seeds. Adding one just
  bloats the catalog. See
  [[feedback_seed_selection_rule]].
- **Every seed commit stages three files together**: `cities/<slug>.json`,
  `region_seeds.json`, and `VERSION`. Missing `VERSION` fails CI's sync
  check. See [[feedback_seed_commit_includes_VERSION]].
- **The built outlines are thinned; the sources are not.** `build.py` drops
  outline points that lie within 2 m of the border through their neighbours
  (`simplify_outlines.py`), which halves the catalog: 5.24 MB → 2.90 MB at
  v196. A border two regions share is thinned once and both get the same
  points, so the apps still merge shared borders exactly. Each outline keeps
  its four extreme points, so the box the apps download by (and the proxy
  caches by) is unchanged. If an outline would still cross itself after
  thinning, the build fails and names it. Edit `cities/` with full detail
  and let the build thin it. Do not change the tolerance lightly: the apps
  compare stored outlines with the catalog byte for byte, so a different
  tolerance reads as a reseed of every city (one-time re-persist of regions
  on every phone). The same goes for the thinning arithmetic: it uses only
  `+ - * /` so every machine builds the same bytes. `math.cos`, `hypot` or
  `sqrt` differ in the last bit between macOS and Linux, which made CI's sync
  check fail on every push from v197 to v198. shapely is pinned in
  `requirements.txt` for the same reason: its validity check decides which
  outlines keep their points. Bump it on purpose, and only if
  `build.py --check` still passes on the new version.
- **Non-OSM sources need license + attribution.** The build rejects
  `"license": "unknown"` because the app redistributes this data.

## Skills in this repo

- `seed-city` — end-to-end skill for adding or updating a seed, including
  the commit rules.
