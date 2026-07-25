#!/usr/bin/env python3
"""
backfill_center_radius.py — add centerLat/centerLng/radiusKm to city files
that lack them.

seed_generator.py has never written these fields (git history shows they only
exist on the ~24 cities carried over from the original pre-automation catalog
split). Without them, the app's RegionSeedLoader.findCity skips coordinate
matching entirely and falls back to exact name-string matching only — less
robust than a coordinate-radius check (fails on any name mismatch, aliasing
gap, or ambiguous multi-country city name). See Sheffield 2026-07-24 investigation.

Center is the midpoint of every region polygon point's bounding box; radius is
the max haversine distance from that center to any polygon point (not the
degree-box approximation used elsewhere in this repo — this walks actual
polygon points, so it's exact rather than an ellipse-diagonal estimate).

Usage:
    python3 backfill_center_radius.py             # backfill all cities missing the fields
    python3 backfill_center_radius.py --dry-run    # report what would change, write nothing
    python3 backfill_center_radius.py "Sheffield"  # backfill one city by name (still skips
                                                    # it if it already has the fields, unless --force)
    python3 backfill_center_radius.py --force      # recompute + overwrite even if already present
"""
import argparse
import json
import math
from pathlib import Path

from seed_generator import decode_polyline

ROOT       = Path(__file__).parent
CITIES_DIR = ROOT / "cities"


def haversine_km(lat1, lng1, lat2, lng2):
    r = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlng / 2) ** 2
    return r * 2 * math.asin(math.sqrt(a))


def region_points(region):
    """All (lat, lng) points for a region, from its polyline or bounding box —
    same two sources toRegionCandidate() accepts on the app side."""
    if region.get("encodedPolyline"):
        return decode_polyline(region["encodedPolyline"])
    bb = region.get("boundingBox")
    if bb:
        return [
            (bb["minLat"], bb["minLng"]), (bb["maxLat"], bb["minLng"]),
            (bb["maxLat"], bb["maxLng"]), (bb["minLat"], bb["maxLng"]),
        ]
    return []


def compute_center_radius(city):
    """Returns (centerLat, centerLng, radiusKm), or None if the city has no
    region with usable geometry at all."""
    points = []
    for region in city.get("regions", []):
        points.extend(region_points(region))
    if not points:
        return None

    lats = [p[0] for p in points]
    lngs = [p[1] for p in points]
    center_lat = (min(lats) + max(lats)) / 2
    center_lng = (min(lngs) + max(lngs)) / 2

    radius_km = max(haversine_km(center_lat, center_lng, lat, lng) for lat, lng in points)
    return round(center_lat, 6), round(center_lng, 6), round(radius_km, 2)


# A small safety margin so shrunk circles don't just barely touch (floating-point
# rounding could leave a hairline overlap) — matches the seed-city skill's
# "shrink so the circles just touch" intent with a little headroom.
OVERLAP_MARGIN_KM = 0.05
MIN_RADIUS_KM = 0.5  # never shrink a city's coverage circle to near-nothing


def resolve_new_overlaps(all_cities):
    """all_cities: list of dicts with name/lat/lng/radius/is_new (mutated in place
    on the 'radius' key for is_new entries). Shrinks radii on newly-backfilled
    ("is_new") cities just enough to remove any overlap that involves at least
    one of them — existing (already-shipped) cities' radii are never touched,
    since RegionSeedLoader.findCity's coordinate match doesn't check city name at
    all, so a *new* overlap silently introduced by this script would be a real
    wrong-city bug that didn't exist before the field was added.

    Overlaps between two already-existing cities are left alone (out of scope —
    they were already live before this script ran) but returned separately so
    the caller can report them.

    Runs to a fixed point: shrinking one pair can't un-overlap a third city
    (shrinking only ever reduces radius), so a single pass over all pairs,
    repeated until nothing changes, is sufficient and always terminates.
    """
    pre_existing_overlaps = []
    changed = True
    while changed:
        changed = False
        for i, a in enumerate(all_cities):
            for b in all_cities[i + 1:]:
                dist = haversine_km(a["lat"], a["lng"], b["lat"], b["lng"])
                overlap = a["radius"] + b["radius"] - dist
                if overlap <= 0:
                    continue
                if not a["is_new"] and not b["is_new"]:
                    if (a["name"], b["name"], round(overlap, 2)) not in pre_existing_overlaps:
                        pre_existing_overlaps.append((a["name"], b["name"], round(overlap, 2)))
                    continue

                shrink_total = overlap + OVERLAP_MARGIN_KM
                if a["is_new"] and b["is_new"]:
                    # Split the reduction between both, proportional to their current
                    # size, but never below MIN_RADIUS_KM.
                    share_a = shrink_total * (a["radius"] / (a["radius"] + b["radius"]))
                    share_b = shrink_total - share_a
                    a["radius"] = max(MIN_RADIUS_KM, a["radius"] - share_a)
                    b["radius"] = max(MIN_RADIUS_KM, b["radius"] - share_b)
                elif a["is_new"]:
                    a["radius"] = max(MIN_RADIUS_KM, a["radius"] - shrink_total)
                else:
                    b["radius"] = max(MIN_RADIUS_KM, b["radius"] - shrink_total)
                changed = True
    return pre_existing_overlaps


def main():
    parser = argparse.ArgumentParser(description="Backfill centerLat/centerLng/radiusKm on city files")
    parser.add_argument("city", nargs="?", help="Only backfill this city (matched against the JSON 'name' field, case-insensitive). Omit to process every city.")
    parser.add_argument("--dry-run", action="store_true", help="Report what would change, write nothing")
    parser.add_argument("--force", action="store_true", help="Recompute and overwrite even if the fields are already present")
    args = parser.parse_args()

    all_paths = sorted(CITIES_DIR.glob("*.json"))
    target_paths = all_paths
    if args.city:
        target = args.city.lower()
        target_paths = [p for p in all_paths if json.loads(p.read_text()).get("name", "").lower() == target]
        if not target_paths:
            print(f"No city file with name '{args.city}' found in {CITIES_DIR}")
            return 1
    target_set = set(target_paths)

    # Build the full catalog's center/radius state up front — overlap resolution
    # needs every city (targets AND everything else already shipped), not just
    # the ones this run will touch, since a newly-backfilled city can overlap an
    # untouched existing one too.
    all_cities, no_geometry_paths = [], []
    for path in all_paths:
        city = json.loads(path.read_text())
        has_fields = all(city.get(k) is not None for k in ("centerLat", "centerLng", "radiusKm"))
        is_target = path in target_set
        will_recompute = is_target and (not has_fields or args.force)

        if will_recompute:
            result = compute_center_radius(city)
            if result is None:
                no_geometry_paths.append(path)
                continue
            lat, lng, radius = result
        elif has_fields:
            lat, lng, radius = city["centerLat"], city["centerLng"], city["radiusKm"]
        else:
            continue  # not a target and has no fields — not part of the coordinate-match catalog at all

        all_cities.append({
            "path": path, "name": city["name"], "lat": lat, "lng": lng,
            "radius": radius, "is_new": will_recompute, "had_fields": has_fields,
        })

    pre_existing_overlaps = resolve_new_overlaps(all_cities)

    if pre_existing_overlaps:
        print("Pre-existing overlaps between already-shipped cities (not touched by this script, already live):")
        for name_a, name_b, overlap in pre_existing_overlaps:
            print(f"  ⚠️   {name_a} <-> {name_b}: {overlap}km overlap")
        print()

    updated, skipped = 0, 0
    for entry in all_cities:
        if not entry["is_new"]:
            if entry["path"] in target_set:
                skipped += 1
            continue

        path = entry["path"]
        city = json.loads(path.read_text())
        action = "would set" if args.dry_run else "set"
        marker = "~" if entry["had_fields"] else "+"
        print(f"{marker}  {path.name}: {action} centerLat={entry['lat']} centerLng={entry['lng']} radiusKm={round(entry['radius'], 2)}")

        if not args.dry_run:
            city["centerLat"] = entry["lat"]
            city["centerLng"] = entry["lng"]
            city["radiusKm"] = round(entry["radius"], 2)
            # Keep key order stable-ish: centerLat/Lng/radiusKm placed right after
            # the identity fields, matching the legacy hand-curated files' layout.
            ordered = {}
            for key in ("name", "countryCode"):
                if key in city:
                    ordered[key] = city.pop(key)
            ordered["centerLat"] = city.pop("centerLat")
            ordered["centerLng"] = city.pop("centerLng")
            ordered["radiusKm"] = city.pop("radiusKm")
            ordered.update(city)
            path.write_text(json.dumps(ordered, indent=2, ensure_ascii=False) + "\n")
        updated += 1

    for path in no_geometry_paths:
        print(f"⚠️   {path.name}: no region has usable geometry, skipping")

    verb = "Would update" if args.dry_run else "Updated"
    print(f"\n{verb} {updated}, skipped {skipped} (already had fields), {len(no_geometry_paths)} had no usable geometry.")
    if not args.dry_run and updated:
        print("Run `python3 build.py` to rebuild region_seeds.json, then commit cities/*.json + region_seeds.json + VERSION together.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
