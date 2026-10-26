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
import math
import sys
from pathlib import Path

from seed_generator import decode_polyline, encode_polyline, _haversine_km
from simplify_outlines import simplify_regions, DEFAULT_TOLERANCE_M

ROOT         = Path(__file__).parent
CITIES_DIR   = ROOT / "cities"
VERSION_PATH = ROOT / "VERSION"
SEEDS_PATH   = ROOT / "region_seeds.json"

BBOX_TOLERANCE = 1e-3  # degrees; polyline encoding rounds to 1e-5

# iOS up to 1.2.1 drops a downloaded catalog over 5 MiB without a word and keeps the one
# it has. The catalog passed that at v176 (2026-09-30), and no change reached iOS for a
# week. Later iOS builds take 25 MiB; until the old ones are gone, a build over this fails.
IOS_LEGACY_MAX_BYTES = 5 * 1024 * 1024

# The apps test a city's circle before its outlines, so an outline point outside the circle
# can never match by coordinate. radiusKm has two decimals and older seeds rounded it to
# the nearest rather than up, so a point can sit up to 5 m outside; hence the slack.
CIRCLE_TOLERANCE_KM = 0.01

# Outlines of two different cities may touch but not overlap: where they do, the apps take
# whichever city comes first in the catalog. Neighbours share a border that is rounded to
# 1e-5 degrees on each side, so slivers under a hectare are let through.
OUTLINE_OVERLAP_MAX_KM2 = 0.01

REQUIRED_CITY_KEYS   = {"name", "countryCode", "centerLat", "centerLng", "radiusKm",
                        "regions", "source"}
REQUIRED_REGION_KEYS = {"name", "boundingBox", "encodedPolyline"}
REQUIRED_SOURCE_KEYS = {"method", "license", "attribution"}
SOURCE_METHODS       = {"osm", "geojson", "shapefile", "manual"}

# Keys stripped from the built file — metadata for contributors, not the app.
CONTRIB_ONLY_KEYS = {"source"}

# Outlines in cities/ keep every point their source had; the built file carries them
# thinned so that no border moves by more than this (see simplify_outlines.py). Nine
# tenths of the catalog is outline points, and half of them sit on straight stretches.
OUTLINE_TOLERANCE_M = DEFAULT_TOLERANCE_M


def bbox_ring(bb):
    """The corners of a region's bounding box as (lat, lng) points."""
    return [(bb["minLat"], bb["minLng"]), (bb["minLat"], bb["maxLng"]),
            (bb["maxLat"], bb["maxLng"]), (bb["maxLat"], bb["minLng"])]


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

    center = (city["centerLat"], city["centerLng"], city["radiusKm"])
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in center):
        err(f"centerLat/centerLng/radiusKm must be numbers, got {center!r}")
        center = None
    worst_outside = None  # (km past the circle, region name)

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
            # bbox-only seed; the app supports these, and matches a point against the box
            # itself, so the box is the outline for the checks below.
            pts = bbox_ring(bb)
        else:
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

        if center:
            reach = max(_haversine_km(center[0], center[1], la, ln) for la, ln in pts)
            outside = reach - center[2]
            if outside > CIRCLE_TOLERANCE_KM and (worst_outside is None or outside > worst_outside[0]):
                worst_outside = (outside, rname)

    if worst_outside:
        err(f"radiusKm {center[2]} does not cover the outlines: region '{worst_outside[1]}' "
            f"reaches {worst_outside[0]:.2f} km past the circle, so the apps cannot match "
            f"points there by coordinate. Recompute centre and radius from the outlines; "
            f"circles of neighbouring cities may overlap")


def check_outline_overlaps(cities, errors):
    """Fails any two cities whose district outlines overlap."""
    try:
        from shapely.geometry import Polygon
        from shapely.ops import unary_union
        from shapely.validation import make_valid
    except ImportError:
        sys.exit("❌  build.py needs shapely to compare city outlines: run it as "
                 "venv/bin/python3 build.py, or pip install -r requirements.txt")

    outlines = []
    for city in cities:
        parts = []
        for r in city["regions"]:
            if r["encodedPolyline"] is None:
                pts = bbox_ring(r["boundingBox"])  # the apps match a bbox-only region by its box
            else:
                pts = decode_polyline(r["encodedPolyline"])
            # Many stored rings touch themselves once rounded to 1e-5; make_valid repairs
            # that without moving the border.
            parts.append(make_valid(Polygon([(ln, la) for la, ln in pts])))
        if parts:
            outlines.append((f"{city['name']} ({city['countryCode']})", unary_union(parts)))

    for i, (name_a, a) in enumerate(outlines):
        for name_b, b in outlines[i + 1:]:
            if not a.intersects(b):
                continue
            shared = a.intersection(b)
            if shared.is_empty or shared.area == 0:
                continue
            lat = shared.representative_point().y
            km2 = shared.area * 111.32 * 111.32 * math.cos(math.radians(lat))
            if km2 > OUTLINE_OVERLAP_MAX_KM2:
                errors.append(f"outlines of {name_a} and {name_b} overlap by {km2:.2f} km²: "
                              f"a point there matches whichever city is first in the catalog")


def simplify_outlines(cities, tolerance_m=OUTLINE_TOLERANCE_M):
    """
    Thins every outline in place for the built file; bounding boxes are left alone.
    All cities go in together so a border two regions (or two cities) share vertex for
    vertex stays shared after thinning. Returns (points before, points after).
    """
    regions = [r for c in cities for r in c["regions"] if r["encodedPolyline"] is not None]
    rings = [[tuple(p) for p in decode_polyline(r["encodedPolyline"])] for r in regions]
    thinned = simplify_regions(rings, tolerance_m)
    for r, ring in zip(regions, thinned):
        r["encodedPolyline"] = encode_polyline(ring)
        # boundingBox stays as the source has it: thinning only drops points, so the box
        # still holds the outline, and the apps download a region's streets by its box, so
        # a changed box would miss the proxy's cache for every region at once.
    return sum(len(r) for r in rings), sum(len(r) for r in thinned)


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
        if "unknown" in (city.get("source", {}).get("license"),
                         city.get("source", {}).get("attribution")):
            print(f"⚠️   {path.name}: source license/attribution unknown — "
                  f"required before this city can be merged")
        cities.append({k: v for k, v in city.items() if k not in CONTRIB_ONLY_KEYS})

    if not errors:
        points_before, points_after = simplify_outlines(cities)
        print(f"ℹ️   outlines thinned to {OUTLINE_TOLERANCE_M:g} m: "
              f"{points_before:,} points → {points_after:,}")
        check_outline_overlaps(cities, errors)

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
    # No whitespace: the apps parse it and nobody reads it, and indentation was 7% of the
    # bytes. The apps compare their stored outlines with these exactly and take any
    # difference as a reseed of the region, so the thinning above must stay deterministic:
    # the same sources give the same bytes, and a change of tolerance reseeds every city.
    rendered = json.dumps(seeds, ensure_ascii=False, separators=(",", ":"))
    size = len(rendered.encode("utf-8"))
    if size > IOS_LEGACY_MAX_BYTES:
        sys.exit(f"❌  region_seeds.json would be {size:,} bytes, over the {IOS_LEGACY_MAX_BYTES:,} "
                 f"older iOS apps accept — they would silently keep their old catalog")
    if size > IOS_LEGACY_MAX_BYTES - 300 * 1024:
        print(f"⚠️   region_seeds.json is {size:,} bytes, "
              f"{IOS_LEGACY_MAX_BYTES - size:,} under the older iOS limit")

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
