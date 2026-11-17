#!/usr/bin/env python3
"""
build.py — validate cities/*.json and assemble region_seeds.json.

The CityWalker app downloads region_seeds.json from this repo's main branch,
so the built file is committed alongside the per-city sources. Run this after
adding or editing a city file:

  python3 build.py            # validate + write region_seeds.json
  python3 build.py --check    # validate + verify region_seeds.json is in sync (CI)

Every city has a permanent "id" (its file name when first assigned) and every
region a permanent whole-number "id", unique within its city and never reused
(assign_ids.py gives new ones). Challenges find their area by these ids, so a
rebuild may not change or drop a published id: a region that goes away moves to
the city's "retired" list with "mergedInto" (the region that took it over, or
null), and a region split off another names it in "splitFrom".

A rebuild may not drop a city or region name the current region_seeds.json
already has either (apps before the ids match by name): the apps match challenges (Complete a City Together) to regions
by name, so a renamed or removed region makes every challenge on it
uncompletable. Pass --allow-removed-regions to drop names on purpose. With
--check (CI) the names are compared with region_seeds.json at --published-ref
(default origin/main), since the file on disk is already the new build there.

Per-city files carry a "source" block (method, license, attribution) that is
kept out of the built file: the app does not need it and the download stays
small.
"""

import argparse
import json
import math
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

from seed_generator import decode_polyline, encode_polyline, _haversine_km
from simplify_outlines import simplify_regions, DEFAULT_TOLERANCE_M, OutlinesNotSettled

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

REQUIRED_CITY_KEYS   = {"id", "name", "countryCode", "centerLat", "centerLng", "radiusKm",
                        "regions", "source"}
REQUIRED_REGION_KEYS = {"id", "name", "boundingBox", "encodedPolyline"}
CITY_ID_PATTERN      = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
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


def is_region_id(v):
    return isinstance(v, int) and not isinstance(v, bool) and v >= 1


def validate_region_ids(city, err):
    """Region ids: whole numbers, unique within the city, never reused; retired and splitFrom consistent."""
    ids = [r.get("id") for r in city.get("regions", [])]
    for r in city.get("regions", []):
        if "id" in r and not is_region_id(r["id"]):
            err(f"region '{r.get('name')}' id must be a whole number from 1, got {r['id']!r}")
    dupes = sorted({i for i in ids if ids.count(i) > 1 and i is not None}, key=str)
    if dupes:
        err(f"region ids used twice: {dupes}")
    # Phones match their downloaded rows to the catalog by name, so two regions of one city
    # with the same name can't be told apart there.
    names = [" ".join(str(r.get("name", "")).casefold().split()) for r in city.get("regions", [])]
    same = sorted({r.get("name") for r, k in zip(city.get("regions", []), names) if names.count(k) > 1})
    if same:
        err(f"region names used twice in this city: {same}")

    retired = city.get("retired", [])
    if not isinstance(retired, list):
        err("retired must be a list")
        retired = []
    retired_ids = []
    for t in retired:
        if not (isinstance(t, dict) and is_region_id(t.get("id")) and "mergedInto" in t):
            err(f"retired entries need a whole-number id and mergedInto (a region id or null), got {t!r}")
            continue
        retired_ids.append(t["id"])
    for t in retired:
        if not isinstance(t, dict) or "mergedInto" not in t:
            continue
        if "splitFrom" in t:
            src = t["splitFrom"]
            if not is_region_id(src) or src == t.get("id") or (src not in ids and src not in retired_ids):
                err(f"retired region {t.get('id')} splitFrom {src!r} is not another region (current or retired)")
        # Into a current region, or into a retired one (apps follow the chain).
        into = t["mergedInto"]
        if into is not None and (into == t.get("id") or (into not in ids and into not in retired_ids)):
            err(f"retired region {t.get('id')} is mergedInto {into!r}, which is not another region "
                f"(current or retired) of this city")
    if len(set(retired_ids)) != len(retired_ids):
        err(f"retired ids listed twice: {sorted(retired_ids)}")
    reused = sorted(set(retired_ids) & set(i for i in ids if i is not None))
    if reused:
        err(f"retired region ids reused by current regions (ids are never reused): {reused}")

    for r in city.get("regions", []):
        if "splitFrom" not in r:
            continue
        src = r["splitFrom"]
        if not is_region_id(src) or src == r.get("id") or (src not in ids and src not in retired_ids):
            err(f"region '{r.get('name')}' splitFrom {src!r} is not another region (current or retired) of this city")


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

    if not (isinstance(city["id"], str) and CITY_ID_PATTERN.match(city["id"])):
        err(f"id must be lowercase letters, digits, '_' or '-', got {city['id']!r}")
    validate_region_ids(city, err)

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


def simplify_outlines(cities, errors, tolerance_m=OUTLINE_TOLERANCE_M):
    """
    Thins every outline in place for the built file; bounding boxes are left alone.
    All cities go in together so a border two regions (or two cities) share vertex for
    vertex stays shared after thinning. Returns (points before, points after), or None
    after adding to errors if some outlines would still cross themselves.
    """
    owned = [(c, r) for c in cities for r in c["regions"] if r["encodedPolyline"] is not None]
    regions = [r for _, r in owned]
    rings = [[tuple(p) for p in decode_polyline(r["encodedPolyline"])] for r in regions]
    try:
        thinned = simplify_regions(rings, tolerance_m)
    except OutlinesNotSettled as e:
        for i in e.rings:
            city, region = owned[i]
            errors.append(f"{city['name']} / {region['name']}: outline still crosses itself "
                          f"after thinning; see simplify_outlines.MAX_PASSES")
        return None
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
        counts = simplify_outlines(cities, errors)
        if counts:
            print(f"ℹ️   outlines thinned to {OUTLINE_TOLERANCE_M:g} m: "
                  f"{counts[0]:,} points → {counts[1]:,}")
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
    ids = [c["id"] for c in cities]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        sys.exit(f"❌  City ids used twice: {sorted(dupes)}")

    return {"version": version, "cities": cities}


def region_names(seeds):
    """
    {(city name, countryCode): Counter of region names} of a built catalog.
    Counted, not a set: some cities have two regions of the same name (Kurobe),
    and dropping one of them must still count as a drop.
    """
    return {(c["name"], c.get("countryCode")): Counter(r["name"] for r in c["regions"])
            for c in seeds["cities"]}


def names_list(counter):
    """'A, B (2)': each name once, with its count when it is above one."""
    return ", ".join(f"{n} ({k})" if k > 1 else n for n, k in sorted(counter.items()))


def check_published_names(old_seeds, new_seeds):
    """
    Names in [old_seeds] (the catalog users have) that [new_seeds] drops, as
    printable lines, plus warnings for regions added to cities that had some.
    Challenges find their area by city name + country and region names, so a
    dropped name leaves those challenges unable to find their streets, and a
    region added to a city changes the target of whole-city challenges.
    """
    old, new = region_names(old_seeds), region_names(new_seeds)
    removed, added = [], []
    for city, names in sorted(old.items(), key=lambda kv: (kv[0][0], kv[0][1] or "")):
        label = f"{city[0]} ({city[1]})"
        if city not in new:
            removed.append(f"{label}: city no longer in the catalog")
            continue
        gone = names - new[city]
        if gone:
            removed.append(f"{label}: regions {names_list(gone)} no longer in the catalog")
        extra = new[city] - names
        if extra:
            added.append(f"{label}: new regions {names_list(extra)}")
    return removed, added


def check_published_ids(old_seeds, new_seeds):
    """
    Ids in [old_seeds] that [new_seeds] changes or drops, as printable lines. A published
    region id must stay a region or move to its city's "retired" list; a retired id must
    stay retired; a city id must stay. Cities or regions published before they had ids are
    skipped (the first build with ids has nothing to compare).
    """
    new_by_id = {c["id"]: c for c in new_seeds["cities"] if "id" in c}
    problems = []
    for old in old_seeds["cities"]:
        if "id" not in old:
            continue
        label = f"{old['name']} ({old.get('countryCode')}) [{old['id']}]"
        new = new_by_id.get(old["id"])
        if new is None:
            problems.append(f"{label}: city id no longer in the catalog")
            continue
        regions = {r["id"]: r for r in new["regions"] if "id" in r}
        retired = {t["id"]: t for t in new.get("retired", [])}
        for r in old["regions"]:
            if "id" not in r:
                continue
            if r["id"] not in regions and r["id"] not in retired:
                problems.append(f"{label}: region {r['id']} ({r['name']}) dropped without a "
                                f"'retired' entry saying where it went")
            elif r["id"] in regions and regions[r["id"]].get("splitFrom") != r.get("splitFrom"):
                # Older challenges on the origin cover this part through splitFrom.
                problems.append(f"{label}: region {r['id']} ({r['name']}) splitFrom changed from "
                                f"{r.get('splitFrom')!r} to {regions[r['id']].get('splitFrom')!r}")
        for t in old.get("retired", []):
            if t["id"] not in retired:
                problems.append(f"{label}: retired region {t['id']} no longer listed as retired")
            elif retired[t["id"]].get("splitFrom") != t.get("splitFrom"):
                problems.append(f"{label}: retired region {t['id']} splitFrom changed from "
                                f"{t.get('splitFrom')!r} to {retired[t['id']].get('splitFrom')!r}")
            elif retired[t["id"]].get("mergedInto") != t.get("mergedInto"):
                problems.append(f"{label}: retired region {t['id']} mergedInto changed from "
                                f"{t.get('mergedInto')!r} to {retired[t['id']].get('mergedInto')!r}")
    return problems


def load_published(ref):
    """
    (catalog, where it came from), or (None, where it should have come from).
    [ref] None reads region_seeds.json on disk; otherwise that file at a git ref.
    """
    if ref is None:
        source = "region_seeds.json"
        try:
            return json.loads(SEEDS_PATH.read_text()), source
        except (OSError, json.JSONDecodeError):
            return None, source
    source = f"region_seeds.json at {ref}"
    try:
        text = subprocess.run(["git", "show", f"{ref}:region_seeds.json"], cwd=ROOT,
                              capture_output=True, text=True, check=True).stdout
        return json.loads(text), source
    except (OSError, subprocess.CalledProcessError, json.JSONDecodeError):
        return None, source


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="Verify region_seeds.json matches the build output (CI)")
    parser.add_argument("--allow-removed-regions", action="store_true",
                        help="Let the build drop city or region names the current "
                             "region_seeds.json has (breaks challenges on them)")
    parser.add_argument("--published-ref", default="origin/main",
                        help="With --check: the git ref whose region_seeds.json users have "
                             "(CI passes the PR's base, or the commit before a push)")
    args = parser.parse_args()

    seeds = build()

    # The names users already have: in CI from git (the file on disk is the new
    # build there), otherwise the region_seeds.json this build is about to replace.
    published, source = load_published(args.published_ref if args.check else None)
    if published is None:
        if not args.allow_removed_regions:
            sys.exit(f"❌  Can't read the published catalog ({source}), so dropped city or region\n"
                     f"    names can't be checked. Fix that, or rerun with --allow-removed-regions.")
        print(f"⚠️   Not checking for dropped names: can't read {source}")
    else:
        id_problems = check_published_ids(published, seeds)
        if id_problems:
            print(f"❌  This build changes ids that {source} has:")
            for line in id_problems:
                print(f"    {line}")
            sys.exit("    Challenges find their area by these ids. A region that goes away moves to\n"
                     "    the city's 'retired' list (with mergedInto); a split-off part names its\n"
                     "    origin in 'splitFrom'. Ids never change.")
        removed, added = check_published_names(published, seeds)
        for line in added:
            print(f"⚠️   {line}: whole-city challenges on it now have more streets to walk")
        if removed:
            print(f"❌  This build drops names that {source} has:" if not args.allow_removed_regions
                  else f"⚠️   Dropping names on purpose (--allow-removed-regions), compared with {source}:")
            for line in removed:
                print(f"    {line}")
            if not args.allow_removed_regions:
                sys.exit("    Challenges are matched to regions by name, so these would break.\n"
                         "    Keep the old names, or rerun with --allow-removed-regions if it's intended.")
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
