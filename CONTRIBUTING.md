# Contributing a city

Thanks for wanting to add your city. The whole process is one command plus a visual check, and I review every PR by looking at the same preview you do.

## Process

1. Run the generator (see the [README](README.md) for source options):

   ```bash
   python3 seed_generator.py "Your City" CC
   ```

2. Open `previews/your_city_cc.html` in a browser and check the result against the standards below. Iterate with `--admin-level`, `--max-regions`, `--clusters`, or a better data source until it looks right. `--dry-run` lets you iterate without writing seed files.

3. Run `python3 build.py --check` to confirm everything validates and `region_seeds.json` is in sync.

4. Open a PR containing the new `cities/<slug>.json`, the preview HTML, the rebuilt `region_seeds.json`, and the bumped `VERSION`. **Include a screenshot of the preview in the PR description.** That screenshot is the main review artifact.

## Curation standards

A good seed makes "I have walked this part of town" feel true. Before submitting, check your preview against these:

- **5 to 25 regions.** Fewer than 5 makes coverage feel coarse; more than 25 turns the map into confetti. Big cities should use clustering or a higher admin level rather than shipping 80 neighborhoods.
- **Walkable granularity.** A region should be coverable in a few walks. If one region swallows half the city, pick a deeper admin level or a better source.
- **Real local names.** Region names should be what residents call those areas. Clustered regions take the name of their most central member; rename in the JSON if the generator picked a poor label.
- **No gaping holes or wild overlaps.** Small gaps between polygons are fine. Regions stacked on top of each other or big unassigned chunks of the urban core are not.
- **City proper, not the metro area.** Seeds cover the administrative city. Suburbs that are their own municipality can be their own seed later.

## Data sources and licensing

- Prefer OSM. It needs no extra arguments and its licensing is uniform.
- If OSM districts for your city are missing or poor, find the city's own open data portal. GeoJSON endpoints (ArcGIS FeatureServer) and Shapefiles both work.
- For non-OSM sources you must pass `--source-license` and `--source-attribution` (or fill them in the city JSON). PRs with `"license": "unknown"` will not be merged, because the app redistributes this data.
- Only use sources that permit redistribution. When unsure, link the portal's terms page in the PR.

## Updating an existing city

Rerun the generator with the same city name and country code; it replaces the existing file and bumps the version. Explain in the PR what improved (better source, better clustering, boundary changes).
