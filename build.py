#!/usr/bin/env python3
"""
build.py — validate cities/*.json and assemble region_seeds.json.

The CityWalker app downloads region_seeds.json from this repo's main branch,
so the built file is committed alongside the per-city sources. Run this after
adding or editing a city file:

  python3 build.py            # validate + write region_seeds.json
  python3 build.py --check    # validate + verify region_seeds.json is in sync (CI)

Per-city files carry a "source" block (method, license, attribution) that is
kept out of the built file: the app does not need it and the download stays
small.
"""

import argparse
import json
import sys
from pathlib import Path

from seed_generator import decode_polyline

ROOT         = Path(__file__).parent
CITIES_DIR   = ROOT / "cities"
VERSION_PATH = ROOT / "VERSION"
SEEDS_PATH   = ROOT / "region_seeds.json"

BBOX_TOLERANCE = 1e-3  # degrees; polyline encoding rounds to 1e-5

REQUIRED_CITY_KEYS   = {"name", "countryCode", "regions", "source"}
REQUIRED_REGION_KEYS = {"name", "boundingBox", "encodedPolyline"}
REQUIRED_SOURCE_KEYS = {"method", "license", "attribution"}
SOURCE_METHODS       = {"osm", "geojson", "shapefile", "manual"}

# Keys stripped from the built file — metadata for contributors, not the app.
CONTRIB_ONLY_KEYS = {"source"}


def validate_city(path, city, errors):
    def err(msg):
        errors.append(f"{path.name}: {msg}")

    missing = REQUIRED_CITY_KEYS - city.keys()
    if missing:
        err(f"missing keys: {sorted(missing)}")
        return

    cc = city["countryCode"]
    if not (isinstance(cc, str) and len(cc) == 2 and cc.isupper()):
        err(f"countryCode must be a 2-letter uppercase ISO code, got {cc!r}")

    source = city["source"]
    missing = REQUIRED_SOURCE_KEYS - source.keys()
    if missing:
        err(f"source missing keys: {sorted(missing)}")
    elif source["method"] not in SOURCE_METHODS:
        err(f"source.method must be one of {sorted(SOURCE_METHODS)}, got {source['method']!r}")

    regions = city["regions"]
    if not regions:
        err("regions is empty")
    for r in regions:
        rname = r.get("name", "<unnamed>")
        missing = REQUIRED_REGION_KEYS - r.keys()
        if missing:
            err(f"region '{rname}' missing keys: {sorted(missing)}")
            continue

        bb = r["boundingBox"]
        if not all(k in bb for k in ("minLat", "maxLat", "minLng", "maxLng")):
            err(f"region '{rname}' has incomplete boundingBox")
            continue
        if not (-90 <= bb["minLat"] <= bb["maxLat"] <= 90
                and -180 <= bb["minLng"] <= bb["maxLng"] <= 180):
            err(f"region '{rname}' boundingBox out of range: {bb}")

        encoded = r["encodedPolyline"]
        if encoded is None:
            continue  # bbox-only seed; the app supports these
        try:
            pts = decode_polyline(encoded)
        except Exception as e:
            err(f"region '{rname}' polyline does not decode: {e}")
            continue
        if len(pts) < 3:
            err(f"region '{rname}' polyline has only {len(pts)} points")
            continue
        lats = [p[0] for p in pts]
        lngs = [p[1] for p in pts]
        drift = max(abs(min(lats) - bb["minLat"]), abs(max(lats) - bb["maxLat"]),
                    abs(min(lngs) - bb["minLng"]), abs(max(lngs) - bb["maxLng"]))
        if drift > BBOX_TOLERANCE:
            err(f"region '{rname}' boundingBox disagrees with polyline by {drift:.5f}°")


def build():
    errors = []

    try:
        version = int(VERSION_PATH.read_text().strip())
    except (FileNotFoundError, ValueError) as e:
        sys.exit(f"❌  VERSION file missing or not an integer: {e}")

    paths = sorted(CITIES_DIR.glob("*.json"))
    if not paths:
        sys.exit("❌  No city files found in cities/")

    cities = []
    for path in paths:
        try:
            city = json.loads(path.read_text())
        except json.JSONDecodeError as e:
            errors.append(f"{path.name}: invalid JSON: {e}")
            continue
        validate_city(path, city, errors)
        cities.append({k: v for k, v in city.items() if k not in CONTRIB_ONLY_KEYS})

    if errors:
        print(f"❌  {len(errors)} validation error(s):")
        for e in errors:
            print(f"    {e}")
        sys.exit(1)

    names = [(c["name"], c["countryCode"]) for c in cities]
    dupes = {n for n in names if names.count(n) > 1}
    if dupes:
        sys.exit(f"❌  Duplicate city entries: {sorted(dupes)}")

    return {"version": version, "cities": cities}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="Verify region_seeds.json matches the build output (CI)")
    args = parser.parse_args()

    seeds = build()
    rendered = json.dumps(seeds, indent=2, ensure_ascii=False)

    if args.check:
        if not SEEDS_PATH.exists():
            sys.exit("❌  region_seeds.json missing — run: python3 build.py")
        if SEEDS_PATH.read_text().rstrip("\n") != rendered:
            sys.exit("❌  region_seeds.json is out of sync with cities/ — run: python3 build.py")
        print(f"✅  region_seeds.json in sync — v{seeds['version']}, {len(seeds['cities'])} cities")
        return

    SEEDS_PATH.write_text(rendered)
    total_regions = sum(len(c["regions"]) for c in seeds["cities"])
    print(f"✅  region_seeds.json — v{seeds['version']}, "
          f"{len(seeds['cities'])} cities, {total_regions} regions")


if __name__ == "__main__":
    main()
