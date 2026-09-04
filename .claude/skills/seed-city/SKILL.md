---
name: seed-city
description: Add or update a CityWalker region seed for a city — generate polygons, verify the preview, check the no-overlap invariant against the existing catalog, validate, commit VERSION + cities/ + region_seeds.json together, push immediately (which ships to users), then regenerate and push the public cities page in citywalker-website. Use when asked to "seed a city", "add a seed", "add <city> to seeds", "reseed <city>", or similar.
---

# seed-city

Adds or updates a seed in `citywalker-seeds`. Pushing `main` ships to every
user's app on the next 24-hour cache refresh, so this skill's job is to make
sure what ships is correct.

## Before you start: is this city eligible?

**Only seed cities that hit `too_large` on the Overpass proxy.** A city that
clean-downloads without hitting the size limit does not need a seed —
seeding it just adds catalog noise and risks the overlap invariant below.
Active walker count is not a reason to seed.

If the request came from `check-radius-downloads` output, the eligibility
check is already done. Otherwise confirm the city is in the too_large list
before proceeding.

## Choosing a data source

Try sources in this order. The order is about cost, not quality — OSM works
for most cities and needs zero research, so only sink time into finding a
portal when OSM demonstrably fails.

1. **OSM (default).** No flags, uniform licensing:
   `python3 seed_generator.py "City" CC`. Try this first every time.

2. **OSM with knobs**, before switching sources. If the preview has too few
   / too many regions, wrong-shape districts, or names that don't match
   what residents call them:
   - `--admin-level N` — probe 6, 7, 8, 9 for the right district layer
   - `--max-regions N` or `--clusters N` — control count for big cities
   - `--dry-run` — iterate without writing files
   This fixes most cities.

2.5. **OSM via Geofabrik, not Overpass** — same source, same license
   (ODbL-1.0), different fetch mechanism. Switch to this when the
   "Finding districts" step comes back with a large count (roughly
   >100-150) or you already know you'll need more than one city from the
   same country — one bulk download serves all of them. Overpass fetches
   one relation's polygon per HTTP request, which turns into 429/504 retry
   pain at scale (Ankara's 559 mahalles would have taken 30-60+ minutes
   fetched one at a time via Overpass; the pipeline below took under a
   minute). Only fixes the *fetch speed* problem — if OSM genuinely lacks
   the boundaries at any admin level (confirmed for Uberaba, BR — see the
   Brazil note below), Geofabrik hits the same empty result, just faster.

   ```bash
   # 1. One-time bulk download of the country extract (~600MB for Turkey;
   #    varies a lot by country size — check download.geofabrik.de first).
   curl -sL "https://download.geofabrik.de/<region>/<country>-latest.osm.pbf" \
     -o /tmp/country.osm.pbf

   # 2. Filter to administrative boundary relations only (fast, one pass).
   osmium tags-filter /tmp/country.osm.pbf r/boundary=administrative \
     -o /tmp/admin_all.osm.pbf --no-progress -O

   # 3. Filter to the specific admin level that holds neighbourhoods for
   #    this country (varies — Turkey's mahalle level is 8; probe with a
   #    small osmium export + grep if unsure).
   osmium tags-filter /tmp/admin_all.osm.pbf r/admin_level=8 \
     -o /tmp/admin8.osm.pbf --no-progress -O

   # 4. Export to GeoJSON.
   osmium export /tmp/admin8.osm.pbf -o /tmp/admin8_all.geojson \
     --geometry-types=polygon -O --no-progress
   ```

   Then in Python: load the GeoJSON, keep only features where
   `properties.admin_level == "8"` and `properties.boundary ==
   "administrative"` (the export also pulls in incidental referenced
   objects — coastlines, unrelated closed ways — that aren't real admin
   boundaries and need filtering out). Clip to the target city's urban
   bbox by feature centroid (get district-level bboxes from Nominatim,
   union + pad them — a whole-province/state bbox is usually too broad
   and pulls in irrelevant rural districts). Write the filtered
   FeatureCollection to a local file, then feed it in like any other
   GeoJSON source:

   ```bash
   python3 seed_generator.py "City" CC \
     --geojson-url "file:///tmp/city_neighbourhoods.geojson" \
     --name-field name \
     --clusters N \
     --source-license "ODbL-1.0" \
     --source-attribution "© OpenStreetMap contributors (via Geofabrik <Country> extract)"
   ```

   **Don't forget `--source-license`/`--source-attribution` here** — the
   geojson method defaults to `"unknown"` unless passed explicitly, even
   though this is the same OSM data the plain `osm` method uses.

   Cross-validate against any districts already fetched via plain Overpass
   for the same city (their names should be a subset of the Geofabrik
   pull) before trusting the bulk extraction — confirmed this way for
   Ankara's Çankaya/Keçiören districts.

3. **City open-data portal (GeoJSON).** Only if OSM is still bad after
   step 2 — missing admin boundaries, huge holes, or districts that just
   aren't what residents recognize. Look for an ArcGIS FeatureServer on
   the city's open data site (usually
   `services*.arcgis.com/.../FeatureServer/N/query?where=1%3D1&outFields=*&f=geojson&outSR=4326`).
   Requires `--source-license` and `--source-attribution`, and the license
   must permit redistribution — the app ships this data.

4. **Local Shapefile.** Last resort, when the portal only offers Shapefile
   downloads. Same license/attribution requirement, plus `--utm-zone N` if
   the projection isn't WGS84.

### Brazil specifically: default to IBGE, not OSM/Geofabrik

Confirmed for Uberaba, BR (2026-08): OSM has **no bairro-level boundaries
at all** for at least some Brazilian cities — only 2 rural "distrito"
subdivisions at admin_level 9, nothing at admin_level 10. Geofabrik doesn't
help here since it's the same underlying OSM data, just fetched
differently — it would return the same empty result, faster.

Instead, IBGE (Brazil's national statistics/mapping agency) publishes an
official 2022 Census "bairros" mesh, one Shapefile per state, covering
every municipality:
`https://geoftp.ibge.gov.br/organizacao_do_territorio/malhas_territoriais/malhas_de_setores_censitarios__divisoes_intramunicipais/censo_2022/bairros/shp/UF/<UF>_bairros_CD2022.zip`
(e.g. `MG_bairros_CD2022.zip`). This is Brazil's equivalent of a Geofabrik
country extract — one small download (a few MB) per state, covering every
city in it, avoiding both Overpass rate limits and OSM's coverage gaps at
once.

Processing:
1. Download and unzip the state's shapefile.
2. It's already geographic (SIRGAS 2000 ≈ WGS84 — no UTM reprojection
   needed, unlike the `--shapefile` flag's default UTM assumption).
3. Filter records by `CD_MUN` (IBGE's 7-digit municipality code) to the
   target city, using `NM_BAIRRO` as the region name.
4. Convert to GeoJSON with `pyshp` (`pip install pyshp` into the repo's
   venv if missing) and feed via `--geojson-url file://...` same as above.

License: Brazil's National Open Data Policy (Decree No. 8,777/2016) plus
the Access to Information Law (12,527/2011) — no explicit CC-BY stamp on
the file itself, but no restriction either (contrast with e.g. Turkey's
HGM, which explicitly restricts to non-commercial use — that one is a
real blocker). Treated as low-risk and used for Uberaba on that basis;
this is a judgment call, not a certainty — flag it again if reused.
Suggested attribution: `"IBGE (Instituto Brasileiro de Geografia e
Estatística) — 2022 Census bairros mesh, published under Brazil's
National Open Data Policy (Decree No. 8,777/2016)"`, license string
`"BR-PNDA-Decree-8777-2016"`.

Check OSM first anyway (step 1) for large/capital cities — coverage
varies a lot by city size, and a big city like Brasília may well have
decent OSM neighbourhood data where a smaller city like Uberaba doesn't.

## Steps

1. **Dry-run and confirm the area with the user.** Before writing any files,
   run with `--dry-run` and open the preview. Show the user:
   - How many regions were found and their names
   - The bbox / extent of the largest region (flag if it seems too big or
     includes non-walkable areas like mountains/parks)
   - The admin level used

   **Wait for the user to confirm the area looks correct before proceeding.**
   Do not skip this step even if the region count is in range.

   ```bash
   python3 seed_generator.py "City" CC --dry-run
   open previews/<slug>.html
   ```

2. **Generate the seed.** From `/Users/pritipatki/AndroidStudioProjects/citywalker-seeds`, using the source you picked above:

   ```bash
   python3 seed_generator.py "City Name" CC
   ```

   For non-OSM sources, pass `--geojson-url` / `--shapefile` with
   `--source-license` and `--source-attribution`. The build fails without
   them. See README for the full flag list.

3. **Review the preview.** Open `previews/<slug>.html` in a browser. Check
   against the curation standards in CONTRIBUTING.md:
   - 5–25 regions (cluster or raise `--admin-level` if more)
   - Real local names (rename in the JSON if the generator picked poorly)
   - Walkable granularity, no gaping holes or wild overlaps *within* the city
   - City proper, not the metro area

   Iterate with `--admin-level`, `--max-regions`, `--clusters`, or switch to
   a better data source. Use `--dry-run` to iterate without touching files.

4. **Check the no-overlap invariant against the catalog.** This is the one
   check the build does not do for you. The Android app's `findCity` picks
   the **first** catalog entry whose `radiusKm` circle contains the user, so
   any two cities whose circles overlap ship a wrong-city bug for anyone in
   the intersection.

   Run this from the repo root:

   ```bash
   python3 - <<'PY'
   import json, math
   d = json.load(open('region_seeds.json'))
   def km(a, b):
       R = 6371.0
       la1, lo1, la2, lo2 = map(math.radians, [a['centerLat'], a['centerLng'], b['centerLat'], b['centerLng']])
       h = math.sin((la2-la1)/2)**2 + math.cos(la1)*math.cos(la2)*math.sin((lo2-lo1)/2)**2
       return 2*R*math.asin(math.sqrt(h))
   cs = d['cities']
   for i, a in enumerate(cs):
       for b in cs[i+1:]:
           dist = km(a, b)
           if dist < a['radiusKm'] + b['radiusKm']:
               overlap = a['radiusKm'] + b['radiusKm'] - dist
               print(f"OVERLAP {overlap:5.1f}km  {a['name']} ({a['radiusKm']}km) ↔ {b['name']} ({b['radiusKm']}km)  centers {dist:.1f}km apart")
   PY
   ```

   If the new city overlaps anything, shrink its `radiusKm` in the city
   JSON, or shrink the neighbor's, so the circles just touch. Rerun the
   check. Do not commit until it prints nothing new. (Pre-existing overlaps
   in the catalog are known — only care about ones involving the city you
   just added or updated.)

5. **Validate and confirm sync.** This is what CI runs:

   ```bash
   python3 build.py --check
   ```

   If it complains that `region_seeds.json` is out of sync, run
   `python3 build.py` (no `--check`) to rebuild.

6. **Commit all three artifacts together.** The staged set must include
   `cities/<slug>.json`, `region_seeds.json`, **and** `VERSION`. Missing
   `VERSION` fails CI's sync check on the PR.

   ```bash
   git add cities/<slug>.json region_seeds.json VERSION previews/<slug>.html
   git commit -m "Add <City> seed"   # or "Reseed <City>: <reason>"
   ```

7. **Push immediately.** Every seed commit gets pushed as soon as it is
   made — do not batch seeds and push later, and do not leave a seed
   commit sitting unpushed. This ships to every user on their next
   24-hour region-seed cache refresh; there is no staging.

   ```bash
   git push origin main
   ```

   This is the one repo where pushing without a separate check-in is
   expected: the commit rules above (eligibility, overlap, `build.py
   --check`, three-file stage) are the review gate, so once they pass the
   push follows in the same step.

8. **Regenerate the public cities page.** The website's city list at
   `citywalker.app/cities/` is built from this repo's `region_seeds.json`
   but lives in the `citywalker-website` repo, so it does not update on
   its own. After every push here, regenerate and push it:

   ```bash
   cd /Users/pritipatki/AndroidStudioProjects/citywalker-website/cities
   python3 generate_index.py            # rebuilds index.html from region_seeds.json
   python3 generate_index.py --check    # must print OK
   ```

   If `--check` still reports stale after regenerating, the new country's
   ISO code is missing from the `COUNTRY` map near the top of
   `generate_index.py` — add it, then regenerate.

   ```bash
   cd /Users/pritipatki/AndroidStudioProjects/citywalker-website
   git add cities/index.html cities/generate_index.py
   git commit -m "Regenerate cities index for <City>"
   git push origin main   # Cloudflare Pages deploys within a minute or two
   ```

## Related

- [[feedback_seed_selection_rule]] — only seed too_large cities
- [[seed_no_overlap_invariant]] — why step 3 matters
- [[feedback_seed_commit_includes_VERSION]] — why step 5 stages three files
- [[citywalker_seeds_repo]] — repo layout; pushing main ships to users
- `check-radius-downloads` skill (in citywalker-pipeline) — finds the
  candidate cities this skill acts on
