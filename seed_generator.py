#!/usr/bin/env python3
"""
seed_generator.py — generate CityWalker region seeds for any city.

Queries Nominatim for the city relation, fetches child admin districts from
Overpass, clusters them if there are too many, and appends the result to
region_seeds.json. Always writes an HTML preview you can open in a browser
to verify the polygons before committing.

Usage:
  python3 seed_generator.py "Mainz" DE
  python3 seed_generator.py "Sacramento" US
  python3 seed_generator.py "Sacramento" US --max-regions 12
  python3 seed_generator.py "Tokyo" JP --admin-level 7
  python3 seed_generator.py "Sacramento" US --dry-run
"""

import argparse
import json
import math
import os
import random
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

SEEDS_PATH   = Path(__file__).parent / "region_seeds.json"
PREVIEW_DIR  = Path(__file__).parent / "previews"
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
NOMINATIM    = "https://nominatim.openstreetmap.org"

# ── HTTP helpers ────────────────────────────────────────────────────────────

def _get(url, headers=None):
    req = urllib.request.Request(url, headers={
        "User-Agent": "CityWalker-seed-generator", **(headers or {})
    })
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())

def _post(url, body, retries=3, pause=15):
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=body.encode(), method="POST", headers={
                "User-Agent": "CityWalker-seed-generator",
                "Content-Type": "text/plain",
            })
            with urllib.request.urlopen(req, timeout=45) as r:
                return json.loads(r.read())
        except Exception as e:
            if attempt < retries - 1:
                print(f"    retry {attempt + 1}/{retries - 1}: {e}", file=sys.stderr)
                time.sleep(pause)
            else:
                raise

# ── Nominatim ───────────────────────────────────────────────────────────────

def find_city(name, country_code):
    """Return (rel_id, admin_level, display_name) for the best city match."""
    params = urllib.parse.urlencode({
        "q": name, "countrycodes": country_code.lower(),
        "format": "json", "limit": 8,
    })
    results = _get(f"{NOMINATIM}/search?{params}")
    rel_id = display_name = None
    for r in results:
        if r.get("osm_type") == "relation" and r.get("class") == "boundary":
            rel_id       = int(r["osm_id"])
            display_name = r.get("display_name", name)
            break
    if rel_id is None:
        for r in results:
            if r.get("osm_type") == "relation":
                rel_id       = int(r["osm_id"])
                display_name = r.get("display_name", name)
                break
    if rel_id is None:
        raise ValueError(f"No OSM relation found for '{name}' in {country_code.upper()}")

    # Fetch the actual admin_level tag from Overpass — Nominatim search results
    # don't reliably include it, which causes the district probe to start at the
    # wrong level (e.g. finding county children instead of city neighbourhoods).
    time.sleep(1)
    data = _post(OVERPASS_URL, f'[out:json];rel({rel_id});out tags;')
    tags = next((e.get("tags", {}) for e in data["elements"] if e.get("type") == "relation"), {})
    admin_level = tags.get("admin_level")
    return rel_id, admin_level, display_name

def _admin_level_from_type(place_type):
    return {"city": "8", "town": "8", "municipality": "8",
            "county": "6", "state": "4"}.get(place_type)

# ── Overpass ────────────────────────────────────────────────────────────────

def find_districts(rel_id, city_admin_level, forced_level=None):
    """
    Probe admin levels from city_level+1 up to 10 until we find children.
    Returns (list_of_relation_elements, found_admin_level).
    """
    if forced_level:
        levels = [forced_level]
    elif city_admin_level:
        al = int(city_admin_level)
        levels = list(range(al + 1, 12))
    else:
        levels = list(range(6, 12))

    for level in levels:
        q = (f'[out:json];'
             f'rel["admin_level"="{level}"]["boundary"="administrative"]'
             f'(area:{3600000000 + rel_id});out body;')
        data = _post(OVERPASS_URL, q)
        els = [e for e in data["elements"] if e.get("type") == "relation"]
        if len(els) >= 2:
            print(f"  admin_level {level}: {len(els)} districts")
            return els, level
        time.sleep(2)
    return [], None

def fetch_polygon(rel_id):
    """Return the outer polygon for a single relation as [(lat, lng), ...]."""
    data = _post(OVERPASS_URL, f'[out:json];rel({rel_id});out body;>;out skel qt;')
    return _build_ring(data["elements"])

def _build_ring(elements):
    nodes    = {e["id"]: (e["lat"], e["lon"]) for e in elements if e["type"] == "node"}
    ways     = {e["id"]: e["nodes"]           for e in elements if e["type"] == "way"}
    rels     = [e for e in elements if e["type"] == "relation"]
    if not rels:
        return []
    rel   = rels[0]
    outer = [m["ref"] for m in rel.get("members", [])
             if m["type"] == "way" and m.get("role", "") in ("outer", "")]
    segs  = [list(ways[w]) for w in outer if w in ways]
    if not segs:
        return []
    ring = list(segs.pop(0))
    while segs:
        last = ring[-1]
        matched = False
        for i, seg in enumerate(segs):
            if seg[0] == last:
                ring += seg[1:]; segs.pop(i); matched = True; break
            if seg[-1] == last:
                ring += list(reversed(seg))[1:]; segs.pop(i); matched = True; break
        if not matched:
            break
    return [nodes[n] for n in ring if n in nodes]

# ── Geometry ────────────────────────────────────────────────────────────────

def centroid(points):
    if not points:
        return (0.0, 0.0)
    return (sum(p[0] for p in points) / len(points),
            sum(p[1] for p in points) / len(points))

def _dist2(a, b):
    return (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2

def kmeans(points, k, max_iter=150, seed=42):
    """K-means++ on (lat, lng) tuples. Returns list of cluster indices."""
    random.seed(seed)
    # k-means++ init
    centers = [points[random.randrange(len(points))]]
    while len(centers) < k:
        dists = [min(_dist2(p, c) for c in centers) for p in points]
        total = sum(dists)
        r     = random.random() * total
        cumul = 0.0
        for i, d in enumerate(dists):
            cumul += d
            if cumul >= r:
                centers.append(points[i]); break
        else:
            centers.append(points[-1])

    assignments = [0] * len(points)
    for _ in range(max_iter):
        new = [min(range(k), key=lambda j: _dist2(p, centers[j])) for p in points]
        if new == assignments:
            break
        assignments = new
        for j in range(k):
            pts = [points[i] for i, a in enumerate(assignments) if a == j]
            if pts:
                centers[j] = centroid(pts)
    return assignments

def convex_hull(points):
    """Graham scan. Returns ordered list of (lat, lng)."""
    pts = sorted(set(points), key=lambda p: (p[1], p[0]))
    if len(pts) < 3:
        return pts

    def cross(O, A, B):
        return (A[1] - O[1]) * (B[0] - O[0]) - (A[0] - O[0]) * (B[1] - O[1])

    lower = []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper = []
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]

# ── Polyline encoding / decoding ────────────────────────────────────────────

def encode_polyline(coords):
    out, prev_lat, prev_lng = [], 0, 0
    for lat, lng in coords:
        for cur, prev in ((round(lat * 1e5), prev_lat), (round(lng * 1e5), prev_lng)):
            v = cur - prev
            v = ~(v << 1) if v < 0 else v << 1
            while v >= 0x20:
                out.append(chr((0x20 | (v & 0x1f)) + 63)); v >>= 5
            out.append(chr(v + 63))
        prev_lat, prev_lng = round(lat * 1e5), round(lng * 1e5)
    return "".join(out)

def decode_polyline(encoded):
    coords, index, lat, lng = [], 0, 0, 0
    while index < len(encoded):
        for is_lng in (False, True):
            result = shift = 0
            while True:
                b = ord(encoded[index]) - 63; index += 1
                result |= (b & 0x1f) << shift; shift += 5
                if b < 0x20:
                    break
            delta = ~(result >> 1) if result & 1 else result >> 1
            if is_lng:
                lng += delta; coords.append([lat / 1e5, lng / 1e5])
            else:
                lat += delta
    return coords

# ── HTML preview ────────────────────────────────────────────────────────────

COLORS = [
    "#e6194b","#3cb44b","#4363d8","#f58231","#911eb4",
    "#42d4f4","#f032e6","#bfef45","#469990","#9A6324",
    "#800000","#aaffc3","#808000","#000075","#a9a9a9",
    "#ffe119","#dcbeff","#fabed4","#ffd8b1","#fffac8",
]

def write_preview(city_name, country_code, regions, original_count):
    PREVIEW_DIR.mkdir(exist_ok=True)
    slug = city_name.lower().replace(" ", "_")
    path = PREVIEW_DIR / f"{slug}_{country_code.lower()}.html"

    features = []
    for i, r in enumerate(regions):
        coords = decode_polyline(r["encodedPolyline"])
        lngs = [c[1] for c in coords]
        lats = [c[0] for c in coords]
        area = (max(lngs) - min(lngs)) * (max(lats) - min(lats))
        features.append({
            "type": "Feature",
            "properties": {"name": r["name"], "color": COLORS[i % len(COLORS)], "area": area},
            "geometry": {"type": "Polygon",
                         "coordinates": [[[c[1], c[0]] for c in coords]]}
        })
    # Render largest first so smaller regions sit on top and aren't buried
    features.sort(key=lambda f: f["properties"]["area"], reverse=True)

    geojson = json.dumps({"type": "FeatureCollection", "features": features})
    cluster_note = (f'<span class="note">⚡ clustered from {original_count} districts</span>'
                    if original_count else "")

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Seeds — {city_name}, {country_code.upper()}</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
  #map {{ height: 100vh; }}
  #panel {{
    position: absolute; top: 12px; left: 50%; transform: translateX(-50%);
    background: white; padding: 10px 20px 12px; border-radius: 10px;
    box-shadow: 0 2px 12px rgba(0,0,0,.18); z-index: 1000; text-align: center;
    min-width: 260px;
  }}
  #panel h2 {{ font-size: 15px; font-weight: 600; color: #111; }}
  #panel .sub {{ font-size: 12px; color: #666; margin-top: 3px; }}
  .note {{ color: #888; font-size: 11px; }}
  #legend {{
    position: absolute; bottom: 24px; right: 12px;
    background: white; padding: 8px 12px; border-radius: 8px;
    box-shadow: 0 2px 8px rgba(0,0,0,.15); z-index: 1000;
    max-height: 60vh; overflow-y: auto; font-size: 12px;
  }}
  .legend-item {{ display: flex; align-items: center; gap: 6px; padding: 2px 0; cursor: pointer; }}
  .legend-swatch {{ width: 12px; height: 12px; border-radius: 2px; flex-shrink: 0; }}
</style>
</head>
<body>
<div id="panel">
  <h2 id="title">{city_name}, {country_code.upper()} &mdash; {len(regions)} regions</h2>
  <div class="sub" id="sub">hover a region &middot; click to focus {cluster_note}</div>
</div>
<div id="map"></div>
<div id="legend"></div>
<script>
var map = L.map('map', {{zoomControl: true}});
L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
  attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'
}}).addTo(map);

var data = {geojson};
var layers = {{}};

var legend = document.getElementById('legend');
var geoLayer = L.geoJSON(data, {{
  style: function(f) {{
    return {{
      color: f.properties.color, weight: 2, opacity: 1,
      fillColor: f.properties.color, fillOpacity: 0.2
    }};
  }},
  onEachFeature: function(f, layer) {{
    var name = f.properties.name;
    layers[name] = layer;
    layer.bindTooltip(name, {{sticky: true}});
    layer.on('mouseover', function() {{
      layer.bringToFront();
      layer.setStyle({{fillOpacity: 0.45, weight: 3}});
      document.getElementById('sub').textContent = name;
    }});
    layer.on('mouseout', function() {{
      layer.setStyle({{fillOpacity: 0.2, weight: 2}});
      document.getElementById('sub').textContent = 'hover a region · click to focus';
    }});
    layer.on('click', function() {{
      map.fitBounds(layer.getBounds(), {{padding: [40, 40]}});
    }});

    var item = document.createElement('div');
    item.className = 'legend-item';
    item.innerHTML =
      '<div class="legend-swatch" style="background:' + f.properties.color + '"></div>' +
      '<span>' + name + '</span>';
    item.addEventListener('click', function() {{
      map.fitBounds(layer.getBounds(), {{padding: [40, 40]}});
    }});
    legend.appendChild(item);
  }}
}}).addTo(map);
map.fitBounds(geoLayer.getBounds(), {{padding: [20, 20]}});
</script>
</body>
</html>"""

    path.write_text(html)
    return path

# ── Main ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Generate CityWalker region seeds for any city.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("city",    help="City name, e.g. 'Sacramento'")
    parser.add_argument("country", help="ISO country code, e.g. 'US'")
    parser.add_argument("--max-regions", type=int, default=20,
                        help="Cluster if district count exceeds this (default 20)")
    parser.add_argument("--clusters",    type=int, default=None,
                        help="Force exact number of clusters (overrides auto)")
    parser.add_argument("--admin-level", type=int, default=None,
                        help="Force a specific OSM admin level for districts")
    parser.add_argument("--dry-run", action="store_true",
                        help="Generate preview only; don't write region_seeds.json")
    args = parser.parse_args()

    # ── 1. Find city ──────────────────────────────────────────────────────
    print(f"\n🔍  {args.city}, {args.country.upper()}")
    rel_id, admin_level, display_name = find_city(args.city, args.country)
    print(f"    {display_name}  (rel:{rel_id}  al:{admin_level})")
    time.sleep(1)

    # ── 2. Find districts ─────────────────────────────────────────────────
    print(f"\n🗂   Finding districts…")
    districts, found_level = find_districts(rel_id, admin_level, args.admin_level)
    if not districts:
        print("❌  No districts found. Try --admin-level to specify manually.")
        sys.exit(1)

    # ── 3. Fetch polygons ─────────────────────────────────────────────────
    print(f"\n⬇️   Downloading {len(districts)} polygons…")
    fetched = []
    for i, d in enumerate(districts):
        name = (d.get("tags") or {}).get("name", f"District {i+1}")
        print(f"    [{i+1:>2}/{len(districts)}] {name}", end="  ", flush=True)
        try:
            pts = fetch_polygon(d["id"])
            if len(pts) < 3:
                print("skip (too few points)")
            else:
                fetched.append({"id": d["id"], "name": name, "points": pts})
                print(f"({len(pts)} pts)")
        except Exception as e:
            print(f"ERROR: {e}")
        time.sleep(3)

    if not fetched:
        print("❌  No polygons fetched successfully.")
        sys.exit(1)

    # ── 4. Cluster if needed ──────────────────────────────────────────────
    original_count = len(fetched)
    need_cluster   = original_count > args.max_regions
    clustered_from = original_count if need_cluster else None

    if need_cluster:
        k = args.clusters or min(max(original_count // 5, 6), 15)
        print(f"\n🔀  Clustering {original_count} districts → {k} groups…")

        pts_list    = [centroid(d["points"]) for d in fetched]
        assignments = kmeans(pts_list, k)

        groups = [[] for _ in range(k)]
        for i, a in enumerate(assignments):
            groups[a].append(fetched[i])
        groups = [g for g in groups if g]

        regions_raw = []
        for group in groups:
            all_pts = [p for d in group for p in d["points"]]
            hull    = convex_hull(all_pts) or all_pts
            c       = centroid([centroid(d["points"]) for d in group])
            label   = min(group, key=lambda d: _dist2(centroid(d["points"]), c))["name"]
            regions_raw.append({"name": label, "points": hull,
                                 "members": [d["name"] for d in group],
                                 "osmId": None, "adminLevel": None})
            print(f"    '{label}': {', '.join(d['name'] for d in group)}")
    else:
        regions_raw = [{"name": d["name"], "points": d["points"],
                        "members": [d["name"]], "osmId": d["id"],
                        "adminLevel": found_level}
                       for d in fetched]

    # ── 5. Build seed entries ─────────────────────────────────────────────
    seed_regions = []
    for r in sorted(regions_raw, key=lambda x: x["name"]):
        pts  = r["points"]
        lats = [p[0] for p in pts]
        lngs = [p[1] for p in pts]
        seed_regions.append({
            "name":            r["name"],
            "osmId":           r["osmId"],
            "adminLevel":      r["adminLevel"],
            "isPointFallback": False,
            "boundingBox": {
                "minLat": min(lats), "maxLat": max(lats),
                "minLng": min(lngs), "maxLng": max(lngs),
            },
            "encodedPolyline": encode_polyline(pts),
        })

    city_entry = {
        "name":        args.city,
        "countryCode": args.country.upper(),
        "regions":     seed_regions,
    }

    # ── 6. Preview ────────────────────────────────────────────────────────
    print(f"\n🗺   Writing preview…")
    preview = write_preview(args.city, args.country, seed_regions, clustered_from)
    print(f"    open {preview}")

    if args.dry_run:
        print(f"\n✅  Dry run — {len(seed_regions)} regions, no files written.\n")
        return

    # ── 7. Update region_seeds.json ───────────────────────────────────────
    with open(SEEDS_PATH) as f:
        seeds = json.load(f)

    existing_idx = next(
        (i for i, c in enumerate(seeds["cities"]) if c["name"] == args.city), None
    )
    if existing_idx is not None:
        print(f"\n📝  Replacing existing entry for {args.city}…")
        seeds["cities"][existing_idx] = city_entry
    else:
        seeds["cities"].append(city_entry)

    seeds["version"] += 1

    with open(SEEDS_PATH, "w") as f:
        json.dump(seeds, f, indent=2, ensure_ascii=False)

    print(f"\n✅  v{seeds['version']} — {len(seeds['cities'])} cities  "
          f"({len(seed_regions)} regions for {args.city})\n")

if __name__ == "__main__":
    main()
