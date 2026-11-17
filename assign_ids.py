#!/usr/bin/env python3
"""
assign_ids.py: give cities and regions their permanent ids (see README, "Ids").

  venv/bin/python3 assign_ids.py          # add missing ids to cities/*.json in place

City id: the file name without .json, set once. Region id: a whole number unique within
its city, never reused; a region without one gets the next number after the highest the
city has ever used (its regions and its "retired" list). Existing ids are never touched.

Edits the text rather than re-dumping the JSON, so a file keeps its own formatting and
the diff is only the new "id" lines.

carry_ids() is what seed_generator.py uses when it writes a city: a new city gets fresh
ids, and a reseed keeps the old ones (by name, then by area), so challenges on it still
find their districts. It prints what it decided; read that before committing.
"""

import json
import re
import sys
from pathlib import Path

CITIES_DIR = Path(__file__).parent / "cities"


def used_ids(city):
    ids = [r["id"] for r in city.get("regions", []) if "id" in r]
    ids += [r["id"] for r in city.get("retired", []) if "id" in r]
    return ids


def assign(path):
    raw = path.read_text(encoding="utf-8")
    city = json.loads(raw)
    changed = False

    if "id" not in city:
        if not raw.startswith("{\n"):
            sys.exit(f"{path.name}: expected the file to start with '{{' on its own line")
        indent = re.match(r"\{\n(\s*)", raw).group(1)
        raw = "{\n" + f'{indent}"id": {json.dumps(path.stem)},\n' + raw[2:]
        changed = True

    missing = [i for i, r in enumerate(city["regions"]) if "id" not in r]
    if missing:
        next_id = max(used_ids(city), default=0) + 1
        # Region objects in order: '{' followed by "name" (the city object now starts with "id").
        starts = list(re.finditer(r'\{(\s*)"name"\s*:', raw))
        # A region that already has an id starts with "id", so only the missing ones match.
        if len(starts) != len(missing):
            sys.exit(f"{path.name}: found {len(starts)} region starts for {len(missing)} regions without an id")
        out, last = [], 0
        for m in starts:
            ws = m.group(1)
            out.append(raw[last:m.start()])
            out.append("{" + ws + f'"id": {next_id},' + ws + '"name":')
            next_id += 1
            last = m.end()
        out.append(raw[last:])
        raw = "".join(out)
        changed = True

    if changed:
        ids = [r.get("id") for r in json.loads(raw)["regions"]]  # must still parse
        if None in ids or len(set(ids)) != len(ids):
            sys.exit(f"{path.name}: region ids missing or repeated after assignment")
        path.write_text(raw, encoding="utf-8")
    return changed


# A new region takes an old one's id (a rename or a redrawn border) when each covers at
# least this much of the other; a split-off part names its origin when this much of it lay
# in the old region; a dropped region is mergedInto the region holding this much of it.
SAME_REGION_SHARE = 0.6
SPLIT_SHARE       = 0.5
MERGE_SHARE       = 0.5


def _shape(region):
    """The region's outline as a shapely polygon (its box for a bbox-only region)."""
    from shapely.geometry import Polygon, box
    from seed_generator import decode_polyline
    if region.get("encodedPolyline"):
        pts = decode_polyline(region["encodedPolyline"])
        if len(pts) >= 3:
            poly = Polygon([(lng, lat) for lat, lng in pts])
            return poly if poly.is_valid else poly.buffer(0)
    bb = region["boundingBox"]
    return box(bb["minLng"], bb["minLat"], bb["maxLng"], bb["maxLat"])


def _share(a, b):
    """How much of a lies in b, 0 to 1."""
    return a.intersection(b).area / a.area if a.area > 0 else 0.0


def _key(name):
    return " ".join(name.casefold().split())


def carry_ids(old_city, new_city, stem, log=print):
    """
    new_city with "id" on the city and every region, "splitFrom" on split-off parts and an
    updated "retired" list, carried over from old_city (the file being replaced, or None).
    Key order: "id" first, "retired" last (only when it has entries).
    """
    old_regions = [r for r in (old_city or {}).get("regions", []) if "id" in r]
    retired = list((old_city or {}).get("retired", []))
    used = {r["id"] for r in old_regions} | {t["id"] for t in retired}
    next_id = max(used, default=0) + 1
    new_regions = new_city["regions"]
    ids = [None] * len(new_regions)
    taken = set()

    # 1. Same name: same district.
    old_by_name = {}
    for r in old_regions:
        old_by_name.setdefault(_key(r["name"]), r)
    for i, r in enumerate(new_regions):
        o = old_by_name.get(_key(r["name"]))
        if o is not None and o["id"] not in taken:
            ids[i] = o["id"]
            taken.add(o["id"])

    old_shapes = {r["id"]: _shape(r) for r in old_regions}
    new_shapes = [_shape(r) for r in new_regions]

    # 2. Renamed or redrawn: an unmatched new region mostly the same area as an unmatched old one.
    pairs = []
    for i, r in enumerate(new_regions):
        if ids[i] is not None:
            continue
        for o in old_regions:
            if o["id"] in taken:
                continue
            a, b = _share(new_shapes[i], old_shapes[o["id"]]), _share(old_shapes[o["id"]], new_shapes[i])
            if min(a, b) >= SAME_REGION_SHARE:
                pairs.append((min(a, b), i, o))
    for _, i, o in sorted(pairs, key=lambda p: -p[0]):
        if ids[i] is None and o["id"] not in taken:
            ids[i] = o["id"]
            taken.add(o["id"])
            log(f"    id {o['id']}: '{o['name']}' is now '{new_regions[i]['name']}' (same area)")

    # 3. New districts: a fresh id; a split-off part names the old district it came from.
    split_from = {}
    for i, r in enumerate(new_regions):
        if ids[i] is not None:
            continue
        ids[i] = next_id
        next_id += 1
        best = max(old_regions, key=lambda o: _share(new_shapes[i], old_shapes[o["id"]]), default=None)
        if best is not None and _share(new_shapes[i], old_shapes[best["id"]]) >= SPLIT_SHARE:
            split_from[i] = best["id"]
            log(f"    id {ids[i]}: new '{r['name']}', split from {best['id']} '{best['name']}'")
        elif old_city is not None:
            log(f"    id {ids[i]}: new '{r['name']}'")

    # 4. Old districts with no successor: retired, merged into the district now holding most of them.
    for o in old_regions:
        if o["id"] in taken:
            continue
        shares = [(_share(old_shapes[o["id"]], new_shapes[i]), ids[i]) for i in range(len(new_regions))]
        share, into = max(shares, default=(0.0, None))
        merged = into if share >= MERGE_SHARE else None
        retired.append({"id": o["id"], "mergedInto": merged})
        log(f"    id {o['id']}: '{o['name']}' retired, " +
            (f"merged into {merged}" if merged is not None else "no successor (challenges on it end)"))
    # A retired region merged into one that is itself gone now points at its successor.
    succ = {t["id"]: t["mergedInto"] for t in retired}
    for t in retired:
        seen = set()
        while t["mergedInto"] is not None and t["mergedInto"] in succ and t["mergedInto"] not in seen:
            seen.add(t["mergedInto"])
            t["mergedInto"] = succ[t["mergedInto"]]

    regions = []
    for i, r in enumerate(new_regions):
        out = {"id": ids[i]}
        out.update({k: v for k, v in r.items() if k not in ("id", "splitFrom")})
        if i in split_from:
            out["splitFrom"] = split_from[i]
        regions.append(out)
    city = {"id": (old_city or {}).get("id", stem)}
    city.update({k: v for k, v in new_city.items() if k not in ("id", "regions", "retired")})
    city["regions"] = regions
    # Keep "regions" where the generator put it (before "source").
    ordered = {}
    for k in ["id"] + [k for k in new_city if k not in ("id", "retired")]:
        ordered[k] = city[k]
    if retired:
        ordered["retired"] = sorted(retired, key=lambda t: t["id"])
    return ordered


def main():
    changed = [p.name for p in sorted(CITIES_DIR.glob("*.json")) if assign(p)]
    print(f"ids added to {len(changed)} city files")


if __name__ == "__main__":
    main()
