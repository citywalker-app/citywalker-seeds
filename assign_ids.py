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
# least SAME_REGION_SHARE of the other. A region keeps its id by name only if the old and
# new outlines overlap by at least NAME_MIN_OVERLAP (generic names like "Zone 1" can move).
# An old region is split when two or more new regions each lie mostly (SPLIT_SHARE) inside
# it and each takes at least PART_MIN_SHARE of it: then the old id is retired and every
# part gets a new id with "splitFrom", so a challenge on the old id keeps its whole area and
# one made later on a part covers only that part ("an id never loses a split part").
# A dropped region is mergedInto the region holding MERGE_SHARE of it.
SAME_REGION_SHARE = 0.6
NAME_MIN_OVERLAP  = 0.25
SPLIT_SHARE       = 0.5
PART_MIN_SHARE    = 0.1
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
    new_city with "id" on the city and every region, "splitFrom" on split parts and an
    updated "retired" list, carried over from old_city (the file being replaced, or None).
    A carried region keeps its "splitFrom"; retired entries are kept as they were.
    Key order: "id" first, "retired" last (only when it has entries).
    """
    old_regions = [r for r in (old_city or {}).get("regions", []) if "id" in r]
    retired = [dict(t) for t in (old_city or {}).get("retired", [])]
    used = {r["id"] for r in old_regions} | {t["id"] for t in retired}
    next_id = [max(used, default=0) + 1]
    new_regions = new_city["regions"]
    n = len(new_regions)
    ids = [None] * n
    split_from = {}
    old_by_id = {r["id"]: r for r in old_regions}
    taken = set()        # old ids carried to a new region
    gone = set()         # old ids retired in this run

    def fresh():
        v = next_id[0]
        next_id[0] += 1
        return v

    old_shapes = {r["id"]: _shape(r) for r in old_regions}
    new_shapes = [_shape(r) for r in new_regions]

    # 1. Splits: an old region with two or more new regions mostly inside it.
    for o in old_regions:
        oshape = old_shapes[o["id"]]
        parts = [i for i in range(n)
                 if ids[i] is None
                 and _share(new_shapes[i], oshape) >= SPLIT_SHARE
                 and (new_shapes[i].intersection(oshape).area / oshape.area if oshape.area > 0 else 0) >= PART_MIN_SHARE]
        if len(parts) < 2:
            continue
        gone.add(o["id"])
        retired.append({"id": o["id"], "mergedInto": None})
        for i in parts:
            ids[i] = fresh()
            split_from[i] = o["id"]
        log(f"    id {o['id']}: '{o['name']}' split into " +
            ", ".join(f"{ids[i]} '{new_regions[i]['name']}'" for i in parts) + f"; {o['id']} retired")

    # 2. Same name and overlapping: same district.
    old_by_name = {}
    for r in old_regions:
        if r["id"] not in gone:
            old_by_name.setdefault(_key(r["name"]), []).append(r)
    for i, r in enumerate(new_regions):
        if ids[i] is not None:
            continue
        for o in old_by_name.get(_key(r["name"]), []):
            if o["id"] in taken:
                continue
            a, b = _share(new_shapes[i], old_shapes[o["id"]]), _share(old_shapes[o["id"]], new_shapes[i])
            if max(a, b) >= NAME_MIN_OVERLAP:
                ids[i] = o["id"]
                taken.add(o["id"])
                break
            log(f"    '{r['name']}': same name as {o['id']} but a different place; not the same district")

    # 3. Renamed or redrawn: an unmatched new region mostly the same area as an unmatched old one.
    pairs = []
    for i in range(n):
        if ids[i] is not None:
            continue
        for o in old_regions:
            if o["id"] in taken or o["id"] in gone:
                continue
            a, b = _share(new_shapes[i], old_shapes[o["id"]]), _share(old_shapes[o["id"]], new_shapes[i])
            if min(a, b) >= SAME_REGION_SHARE:
                pairs.append((min(a, b), i, o))
    for _, i, o in sorted(pairs, key=lambda p: -p[0]):
        if ids[i] is None and o["id"] not in taken:
            ids[i] = o["id"]
            taken.add(o["id"])
            log(f"    id {o['id']}: '{o['name']}' is now '{new_regions[i]['name']}' (same area)")

    # 4. New districts: a fresh id.
    for i, r in enumerate(new_regions):
        if ids[i] is None:
            ids[i] = fresh()
            if old_city is not None:
                log(f"    id {ids[i]}: new '{r['name']}'")

    # 5. Old districts with no successor: retired, merged into the district now holding most of them.
    for o in old_regions:
        if o["id"] in taken or o["id"] in gone:
            continue
        shares = [(_share(old_shapes[o["id"]], new_shapes[i]), ids[i]) for i in range(n)]
        share, into = max(shares, default=(0.0, None))
        merged = into if share >= MERGE_SHARE else None
        retired.append({"id": o["id"], "mergedInto": merged})
        log(f"    id {o['id']}: '{o['name']}' retired, " +
            (f"merged into {merged}" if merged is not None else "no successor (challenges on it end)"))

    regions = []
    for i, r in enumerate(new_regions):
        out = {"id": ids[i]}
        out.update({k: v for k, v in r.items() if k not in ("id", "splitFrom")})
        if i in split_from:
            out["splitFrom"] = split_from[i]
        elif ids[i] in old_by_id and "splitFrom" in old_by_id[ids[i]]:
            out["splitFrom"] = old_by_id[ids[i]]["splitFrom"]   # a carried region keeps its origin
        regions.append(out)
    city = {"id": (old_city or {}).get("id", stem)}
    city.update({k: v for k, v in new_city.items() if k not in ("id", "regions", "retired")})
    city["regions"] = regions
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
