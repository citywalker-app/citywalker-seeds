---
name: seed-city
description: Add or update a CityWalker region seed for a city — generate polygons, verify the preview, validate, commit VERSION + cities/ + region_seeds.json together, push immediately (which ships to users), then regenerate and push the public cities page in citywalker-website. Use when asked to "seed a city", "add a seed", "add <city> to seeds", "reseed <city>", or similar.
---

# seed-city

Adds or updates a seed in `citywalker-seeds`. Pushing `main` ships to every
user's app on the next 24-hour cache refresh, so this skill's job is to make
sure what ships is correct.

## Before you start: is this city eligible?

**Policy since 2026-10-27: seed every unseeded city that hit the too-large
screen** (a radius fallback in Aptabase, or a full-city build by the
too-large pipeline in the proxy cache), **even when the whole city downloads
fine.** A seed gives region picking and a per-region coverage breakdown that
a full-city download doesn't, and both apps move existing users onto a new
seed's regions on their own (full-city downloads are re-split into regions
on the phone, radius cities get regions plus an auto-download). There is no
path-count gate any more: don't run the old Overpass `out count` or
Nominatim checks to decide eligibility.

Still skip or ask:
- **Counties, districts and other non-city areas** (Somerset, Norfolk, New
  Castle County, Rhondda Cynon Taf...): hold them and ask the user. Seeds are
  meant to be the city proper.
- **Small towns that can't make 5+ sensible walkable regions** (Bariri,
  Guamal, Voss): these usually tripped only the 100 km² area rule
  (`NominatimService.MAX_CITY_AREA_KM2`). Report them instead of forcing a
  seed.
- **The seed's outline must match the boundary the full-city download used**
  (the city's own boundary, not a wider metro). If a seed's outer regions
  reach past it, Android marks partly filled border regions downloaded, and
  iOS refuses to re-split a city whose streets fall in under half the seed's
  regions (`coversTooLittleOfSeed`). Keep rural parts as large regions
  rather than dropping them when the city boundary includes them (Ottawa,
  Winnipeg).

## No live OSM calls without asking

**Don't call Overpass or nominatim.openstreetmap.org (and so don't run
`seed_generator.py` without `--geojson-url`/`--shapefile`, which calls both)
unless the user OKs it for that city.** The public mirrors are often
overloaded and the user has asked to keep our traffic off them. A Geofabrik
country extract (one file download, processed offline with osmium) is fine
where it is that country's precedent (Turkey). Don't download other extracts
just to estimate walkability.

## Choosing a data source

**Start from the country's precedent.** Before picking a source, list how
existing seeds in that country were made (`source` in `cities/*_<cc>.json`)
and follow the non-OSM one if there is one: US city portals, UK ONS wards
(Medway, Leeds, Bradford), CA city open data (Calgary, Ottawa, Winnipeg), BR
IBGE, MX SEPOMEX (Mérida, Guadalajara), KR SGIS via vuski/admdongkor, NO SSB
grunnkretser, JP CODH Geoshape, AU state/city open data (Sydney, Brisbane,
Hobart), TR Geofabrik. Only where a country has nothing but direct-OSM
precedents (ES, FI, HU, RU, SE...) look for an open-data alternative first,
then ask the user before using OSM. The list below is the older cost order,
for when OSM is OK'd.

**Record the real source.** After generating, replace `source.url` in
`cities/<slug>.json` with the public upstream URL plus a short note on any
filtering or grouping (see São José dos Campos). A local `file://` path means
nothing to a reader, and the public credits page links this URL.

**Licences.** The licence must allow redistribution. If a portal states none,
stop and ask the user (Dallas, Las Vegas, Virginia Beach, Houston). Record
the attribution the licence asks for: it is shown on
citywalker.app/data-sources/.

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

4. **Leave the circle as generated.** `seed_generator.py` writes
   `centerLat`/`centerLng`/`radiusKm` so the circle covers every outline
   point. Do not shrink it, even when it overlaps a neighbouring city's
   circle: the apps only match a city when the point is also inside one of
   its district outlines, so overlapping circles are harmless. A circle
   that is too small is not: the apps test it first, and any part of the
   city outside it can no longer be matched by coordinate.

   What must not overlap is the **outlines** of two different cities,
   because there the apps take whichever city is first in the catalog. If
   the new city's districts spill into a neighbouring seed city, fix the
   outlines (a better source, or drop the district that belongs to the
   neighbour). The next step fails on both problems.

5. **Validate and confirm sync.** This is what CI runs:

   ```bash
   venv/bin/python3 build.py --check
   ```

   It also fails when a city's outlines reach past its own circle, or when
   the outlines of two cities overlap. If it complains that `region_seeds.json` is out of sync, run
   `venv/bin/python3 build.py` (no `--check`) to rebuild.

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
   expected: the commit rules above (eligibility, `build.py
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
   cd ..
   python3 data-sources/generate.py          # credits page, from cities/*.json source blocks
   python3 data-sources/generate.py --check  # must print OK
   ```

   If the data sources page shows a licence as its raw id, add it to
   `LICENCES` in `data-sources/generate.py` with a display name and link.

   If `--check` still reports stale after regenerating, the new country's
   ISO code is missing from the `COUNTRY` map near the top of
   `generate_index.py` — add it, then regenerate.

   ```bash
   cd /Users/pritipatki/AndroidStudioProjects/citywalker-website
   git add cities/index.html cities/generate_index.py data-sources/index.html data-sources/generate.py
   git commit -m "Regenerate cities index for <City>"
   git push origin main   # Cloudflare Pages deploys within a minute or two
   ```

   Stage only these files by name. The website checkout often holds other
   unpublished work, and the pipeline's daily report commits and pushes
   `main` from the same checkout, so anything committed there goes live on
   its next run (it shipped the data sources page on 2026-10-28 before its
   own push).

## Related

- [[feedback_seed_policy_and_overpass]]: seed every too-large city, even ones that download in full; ask before live OSM calls (replaces the old "only seed too_large" rule)
- [[seed_no_overlap_invariant]]: the circle rule this replaced, retired 2026-10-13, and why step 4 says not to shrink
- [[feedback_seed_commit_includes_VERSION]]: why step 6 stages three files
- [[citywalker_seeds_repo]] — repo layout; pushing main ships to users
- `check-radius-downloads` skill (in citywalker-pipeline) — finds the
  candidate cities this skill acts on
