# citywalker-seeds

Region seeds for [CityWalker](https://citywalker.app), an Android app for exploring your city on foot. Each seed divides a city into a handful of walkable regions (districts, neighborhoods, boroughs) so the app can track which parts of a city you have covered.

The interesting part for non-CityWalker users: `seed_generator.py` is a standalone tool that turns any city's open data into clustered, named, walkable regions. Point it at a city name (OpenStreetMap), a GeoJSON URL (ArcGIS or any open data portal), or a local Shapefile, and it produces clean region polygons plus an interactive HTML preview. No API keys, no paid services.

## Disclaimer

Region boundaries are approximate and intended for exploration tracking, not authoritative administrative use. Where a city has too many districts for practical use, the generator clusters them into larger walkable zones.

## How the app uses this repo

The app downloads [`region_seeds.json`](region_seeds.json) from this repo's `main` branch and caches it for 24 hours, falling back to a bundled copy. That file is a built artifact: the source of truth is the per-city files in [`cities/`](cities/), assembled by [`build.py`](build.py). Do not edit `region_seeds.json` by hand.

## Adding a city

You need Python 3.10+. No required dependencies; `shapely` (better cluster polygons) and `pyshp` (Shapefile input) are optional.

The simplest path uses OpenStreetMap admin boundaries:

```bash
python3 seed_generator.py "Mainz" DE
```

This finds the city relation on Nominatim, probes admin levels for its districts, downloads each polygon from Overpass (politely, with caching and resume support), clusters districts with k-means if there are more than 20, and writes:

- `cities/mainz_de.json`, the seed with source metadata
- `previews/mainz_de.html`, an interactive Leaflet map to eyeball the result
- `region_seeds.json`, rebuilt with the catalog version bumped

Open the preview in a browser before committing. If the regions look wrong, common fixes:

```bash
# Force a specific OSM admin level for districts
python3 seed_generator.py "Tokyo" JP --admin-level 7

# Control clustering
python3 seed_generator.py "Sacramento" US --max-regions 12
python3 seed_generator.py "Berlin" DE --clusters 10

# Preview only, write nothing
python3 seed_generator.py "Oslo" NO --dry-run
```

OSM admin boundaries are sometimes missing or poor. Many cities publish better neighborhood data themselves, and the generator can use it directly:

```bash
# GeoJSON from a city open data portal (ArcGIS FeatureServer shown here)
python3 seed_generator.py "Sacramento" US \
  --geojson-url "https://services5.arcgis.com/.../query?where=1%3D1&outFields=*&f=geojson&outSR=4326" \
  --name-field NAME \
  --source-license "CC-BY-4.0" \
  --source-attribution "City of Sacramento Open Data"

# Local Shapefile (UTM coordinates are reprojected to WGS84)
python3 seed_generator.py "Ingolstadt" DE \
  --shapefile path/to/Bezirke_Polygon.shp --name-field bezname --utm-zone 32 \
  --source-license "DL-DE/BY-2-0" \
  --source-attribution "Stadt Ingolstadt Open Data"
```

When you use a non-OSM source, record its license and attribution. The build fails without them.

Then validate and open a PR (see [CONTRIBUTING.md](CONTRIBUTING.md)):

```bash
python3 build.py --check
```

## Seed format

Each file in `cities/` looks like this:

```jsonc
{
  "name": "Mainz",
  "countryCode": "DE",
  "regions": [
    {
      "name": "Altstadt",
      "osmId": 12345,            // null for clustered or non-OSM regions
      "adminLevel": 9,           // null for clustered or non-OSM regions
      "isPointFallback": false,
      "boundingBox": { "minLat": 49.98, "maxLat": 50.01, "minLng": 8.26, "maxLng": 8.29 },
      "encodedPolyline": "..."   // Google encoded polyline of the region outline
    }
  ],
  "source": {
    "method": "osm",             // osm | geojson | shapefile | manual
    "license": "ODbL-1.0",
    "attribution": "© OpenStreetMap contributors"
  }
}
```

`build.py` validates every file (schema, polyline decodes, bounding box matches the polygon) and assembles `region_seeds.json`, stripping the `source` block to keep the app download small. The catalog version in [`VERSION`](VERSION) is bumped automatically by the generator; the app uses it to detect updates.

## License

Code is MIT. City data derived from OpenStreetMap is © OpenStreetMap contributors, under the [ODbL](https://opendatacommons.org/licenses/odbl/1-0/). Cities built from municipal open data carry their own license and attribution in their `source` block. See [LICENSE](LICENSE).
