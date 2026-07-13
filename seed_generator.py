#!/usr/bin/env python3
"""
seed_generator.py — generate CityWalker region seeds for any city.

Queries Nominatim for the city relation, fetches child admin districts from
Overpass, clusters them if there are too many, and writes the result to
cities/<slug>.json, then rebuilds region_seeds.json via build.py. Always
writes an HTML preview you can open in a browser to verify the polygons
before committing.

Usage:
  python3 seed_generator.py "Mainz" DE
  python3 seed_generator.py "Sacramento" US
  python3 seed_generator.py "Sacramento" US --max-regions 12
  python3 seed_generator.py "Tokyo" JP --admin-level 7
  python3 seed_generator.py "Sacramento" US --dry-run

  # Use official city GeoJSON instead of OSM (skips Overpass entirely):
  python3 seed_generator.py "Sacramento" US --geojson-url "https://services5.arcgis.com/54falWtcpty3V47Z/arcgis/rest/services/Neighborhoods/FeatureServer/0/query?where=1%3D1&outFields=*&f=geojson&outSR=4326"

  # Use a local Shapefile (UTM Zone 32N) instead of OSM:
  python3 seed_generator.py "Ingolstadt" DE --shapefile /tmp/ingolstadt_shp/Bezirke_Polygon.shp --name-field bezname
"""

import argparse
import json
import math
import os
import random
import re
import subprocess
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
from pathlib import Path

try:
    from shapely.geometry import Polygon as ShapelyPolygon
    from shapely.ops import unary_union
    HAS_SHAPELY = True
except ImportError:
    HAS_SHAPELY = False

ROOT         = Path(__file__).parent
CITIES_DIR   = ROOT / "cities"
VERSION_PATH = ROOT / "VERSION"
PREVIEW_DIR  = ROOT / "previews"
CACHE_DIR    = ROOT / ".cache"
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
NOMINATIM    = "https://nominatim.openstreetmap.org"

# NFKD strips most diacritics; these letters don't decompose.
_TRANSLIT = str.maketrans({"ł": "l", "Ł": "l", "ø": "o", "Ø": "o",
                           "đ": "d", "Đ": "d", "ß": "ss"})

def slugify(city, country, slug_override=None):
    if slug_override:
        s = re.sub(r"[^a-z0-9-]+", "_", slug_override.lower()).strip("_")
    else:
        s = city.translate(_TRANSLIT)
        s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
        s = re.sub(r"[^a-z0-9-]+", "_", s.lower()).strip("_")
    if not s:
        raise ValueError(
            f"Couldn't derive a filename slug from {city!r} — the name has no "
            f"Latin-transliterable characters (common for Greek/Cyrillic/CJK "
            f"cities where Nominatim's English-name search only returns a "
            f"place node, not a boundary relation, so you have to query the "
            f"native name). Pass --slug to set one explicitly, e.g. "
            f"--slug piraeus."
        )
    return f"{s}_{country.lower()}"


# ── Download cache (resume support) ─────────────────────────────────────────

def _cache_path(city, country, slug=None):
    return CACHE_DIR / f"{slugify(city, country, slug)}.json"

def load_cache(city, country, slug=None):
    path = _cache_path(city, country, slug)
    if path.exists():
        data = json.loads(path.read_text())
        fetched = sum(1 for d in data["districts"] if d["points"] is not None)
        total   = len(data["districts"])
        print(f"  ↩️  Resuming from cache ({fetched}/{total} already fetched)")
        return data
    return None

def save_cache(city, country, rel_id, admin_level, found_level, districts, slug=None):
    CACHE_DIR.mkdir(exist_ok=True)
    _cache_path(city, country, slug).write_text(json.dumps({
        "city": city, "country": country,
        "rel_id": rel_id, "admin_level": admin_level, "found_level": found_level,
        "districts": [
            {"id": d["id"], "name": d["name"],
             "points": [[p[0], p[1]] for p in d["points"]] if d.get("points") else None}
            for d in districts
        ],
    }, indent=2))

def clear_cache(city, country, slug=None):
    p = _cache_path(city, country, slug)
    if p.exists():
        p.unlink()

# ── HTTP helpers ────────────────────────────────────────────────────────────

_USER_AGENT = (
    "Mozilla/5.0 (compatible; CityWalker-seeds/1.0; "
    "+https://github.com/citywalker-app/citywalker-seeds)"
)

def _get(url, headers=None):
    req = urllib.request.Request(url, headers={
        "User-Agent": _USER_AGENT,
        # urllib sends no Accept header by default — some open-data portals
        # (data.boston.gov via S3 presigned redirect) reject that as a bot signal.
        "Accept": "application/json, application/geo+json, */*",
        **(headers or {})
    })
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())

def _post(url, body, retries=3, pause=15):
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=body.encode(), method="POST", headers={
                # Overpass-API 406's on the Mozilla-style UA used by _get for
                # open-data portals — use the simple tool name for Overpass POSTs.
                "User-Agent": "CityWalker-seeds/1.0",
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

    # A relation can have multiple disjoint outer rings (exclaves / multi-part
    # districts, e.g. Munich's Stadtbezirke). Chain every ring we can build
    # from the remaining segments, then keep the largest — same "most points
    # as a proxy for size" heuristic _geojson_ring_to_points uses for
    # GeoJSON MultiPolygons, applied here so OSM relations get the same
    # treatment instead of silently returning just the first fragment.
    rings = []
    while segs:
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
        rings.append(ring)

    best = max(rings, key=len)
    return [nodes[n] for n in best if n in nodes]

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

def _union_polygons(districts):
    """
    Return the exterior polygon of the union of all member district polygons.
    Uses Shapely when available (exact union, no overlap); falls back to the
    convex hull of all member points when Shapely is not installed.
    """
    if HAS_SHAPELY:
        polys = []
        for d in districts:
            pts = d["points"]
            if len(pts) >= 3:
                # Shapely uses (x, y) = (lng, lat)
                poly = ShapelyPolygon([(p[1], p[0]) for p in pts])
                if not poly.is_valid:
                    # OSM ways occasionally stitch into a self-touching/bowtie
                    # ring — buffer(0) is the standard Shapely repair trick,
                    # otherwise unary_union raises a TopologyException.
                    poly = poly.buffer(0)
                polys.append(poly)
        try:
            merged = unary_union(polys) if polys else None
        except Exception as e:
            print(f"    ⚠️   union failed even after repair ({e}); "
                  f"falling back to convex hull for this cluster", file=sys.stderr)
            merged = None
        if merged is not None:
            # MultiPolygon → take the largest piece
            if merged.geom_type == "MultiPolygon":
                merged = max(merged.geoms, key=lambda g: g.area)
            if not merged.is_empty:
                # Convert back to (lat, lng)
                return [(lat, lng) for lng, lat in merged.exterior.coords]
    # Fallback: convex hull. Bridges gaps between non-adjacent member
    # districts and bloats past the real union footprint, which can make
    # neighboring clusters visibly overlap in the preview — always prefer
    # the Shapely path (`pip install shapely` / see requirements.txt).
    print("    ⚠️   shapely not installed — using convex-hull fallback for this "
          "cluster; check the preview closely for overlaps with its neighbors",
          file=sys.stderr)
    all_pts = [p for d in districts for p in d["points"]]
    return convex_hull(all_pts) or all_pts


# ── Shapefile source (UTM → WGS84) ──────────────────────────────────────────

def _utm_to_latlon(easting, northing, zone=32, southern=False):
    import math
    k0 = 0.9996; a = 6378137.0; e2 = 0.00669438
    e_p2 = e2 / (1 - e2); e4 = e2**2; e6 = e2**3
    x = easting - 500000
    # Southern-hemisphere UTM uses a false northing of 10,000,000 m so that
    # northing values stay positive. Subtract it back out before the maths.
    y = northing - 10_000_000 if southern else northing
    m = y / k0
    mu = m / (a * (1 - e2/4 - 3*e4/64 - 5*e6/256))
    e1 = (1 - math.sqrt(1-e2)) / (1 + math.sqrt(1-e2))
    phi1 = (mu + (3*e1/2 - 27*e1**3/32)*math.sin(2*mu)
               + (21*e1**2/16 - 55*e1**4/32)*math.sin(4*mu)
               + (151*e1**3/96)*math.sin(6*mu))
    N1 = a / math.sqrt(1 - e2*math.sin(phi1)**2)
    T1 = math.tan(phi1)**2
    C1 = e_p2 * math.cos(phi1)**2
    R1 = a*(1-e2) / (1-e2*math.sin(phi1)**2)**1.5
    D = x / (N1*k0)
    lat = phi1 - (N1*math.tan(phi1)/R1)*(
        D**2/2
        - (5+3*T1+10*C1-4*C1**2-9*e_p2)*D**4/24
        + (61+90*T1+298*C1+45*T1**2-252*e_p2-3*C1**2)*D**6/720)
    lon_0 = math.radians((zone-1)*6 - 180 + 3)
    lon = lon_0 + (D - (1+2*T1+C1)*D**3/6
                     + (5-2*C1+28*T1-3*C1**2+8*e_p2+24*T1**2)*D**5/120) / math.cos(phi1)
    return math.degrees(lat), math.degrees(lon)

def districts_from_shapefile(path, name_field="NAME", utm_zone=32, utm_south=False):
    """
    Read a Shapefile (UTM Zone utm_zone) and return a list of
    {"name": str, "points": [(lat, lng), ...]} dicts.
    Requires pyshp (pip install pyshp).
    """
    try:
        import shapefile as pyshp
    except ImportError:
        print("❌  pyshp not installed. Run: pip install pyshp")
        sys.exit(1)
    sf = pyshp.Reader(path, encoding="latin-1")
    fields = [f[0] for f in sf.fields[1:]]
    districts = []
    for sr in sf.shapeRecords():
        rec  = dict(zip(fields, sr.record))
        name = rec.get(name_field) or "Unknown"
        pts  = [_utm_to_latlon(e, n, utm_zone, utm_south) for e, n in sr.shape.points]
        if len(pts) >= 3:
            districts.append({"name": name, "points": pts})
    print(f"  {len(districts)} districts loaded from Shapefile")
    return districts


# ── GeoJSON source (city open data / ArcGIS) ────────────────────────────────

def _geojson_ring_to_points(ring):
    """Convert a GeoJSON exterior ring [[lng, lat], ...] to [(lat, lng), ...]."""
    return [(coord[1], coord[0]) for coord in ring]

def _largest_ring(geometry):
    """Extract the exterior ring of the largest polygon from a Polygon or MultiPolygon."""
    if geometry["type"] == "Polygon":
        return geometry["coordinates"][0]
    if geometry["type"] == "MultiPolygon":
        # Pick the polygon with the most exterior-ring points as a proxy for largest
        return max(geometry["coordinates"], key=lambda p: len(p[0]))[0]
    return []

def districts_from_geojson(url, name_field="NAME"):
    """
    Download a GeoJSON FeatureCollection from url and return a list of
    {"name": str, "points": [(lat, lng), ...]} dicts ready for clustering.
    Skips features with fewer than 3 points.
    """
    print(f"  Fetching {url[:80]}{'…' if len(url) > 80 else ''}")
    data = _get(url)
    features = data.get("features", [])
    districts = []
    skipped = 0
    for feat in features:
        name = (feat.get("properties") or {}).get(name_field) or "Unknown"
        geom = feat.get("geometry")
        if not geom:
            skipped += 1
            continue
        ring = _largest_ring(geom)
        pts  = _geojson_ring_to_points(ring)
        if len(pts) < 3:
            skipped += 1
            continue
        districts.append({"name": name, "points": pts})
    if skipped:
        print(f"  ⚠️  Skipped {skipped} features (no geometry or too few points)")
    print(f"  {len(districts)} districts loaded from GeoJSON")
    return districts


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

def write_preview(city_name, country_code, regions, original_count, slug=None):
    PREVIEW_DIR.mkdir(exist_ok=True)
    path = PREVIEW_DIR / f"{slugify(city_name, country_code, slug)}.html"

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
    parser.add_argument("--geojson-url", default=None,
                        help="GeoJSON URL (ArcGIS/open data) — skips Overpass entirely")
    parser.add_argument("--shapefile", default=None,
                        help="Local .shp file path — skips Overpass entirely")
    parser.add_argument("--utm-zone", type=int, default=32,
                        help="UTM zone for Shapefile reprojection (default: 32)")
    parser.add_argument("--utm-south", action="store_true",
                        help="Source Shapefile is southern-hemisphere UTM (false_northing 10,000,000)")
    parser.add_argument("--name-field", default="NAME",
                        help="GeoJSON/Shapefile property for district name (default: NAME)")
    parser.add_argument("--source-license", default=None,
                        help="License of the source data, e.g. 'CC0-1.0' or 'DL-DE/BY-2-0' "
                             "(required info for GeoJSON/Shapefile sources; OSM defaults to ODbL-1.0)")
    parser.add_argument("--source-attribution", default=None,
                        help="Attribution line for the source, e.g. 'City of Sacramento Open Data'")
    parser.add_argument("--dry-run", action="store_true",
                        help="Generate preview only; don't write any seed files")
    parser.add_argument("--slug", default=None,
                        help="Explicit filename slug (without the country suffix), for "
                             "cities whose name has no Latin-transliterable characters, "
                             "e.g. --slug piraeus for a city queried by its Greek name")
    args = parser.parse_args()

    print(f"\n🔍  {args.city}, {args.country.upper()}")

    if args.geojson_url:
        # ── GeoJSON path: one request, no Overpass, no cache needed ──────
        print(f"\n🌐  Loading districts from GeoJSON…")
        fetched = districts_from_geojson(args.geojson_url, args.name_field)
        if not fetched:
            print("❌  No districts found in GeoJSON.")
            sys.exit(1)
        found_level = None

    elif args.shapefile:
        # ── Shapefile path: local file, no Overpass, no cache needed ─────
        print(f"\n📂  Loading districts from Shapefile…")
        fetched = districts_from_shapefile(args.shapefile, args.name_field, args.utm_zone, args.utm_south)
        if not fetched:
            print("❌  No districts found in Shapefile.")
            sys.exit(1)
        found_level = None

    else:
        # ── OSM / Overpass path (with resume cache) ───────────────────────
        cache = load_cache(args.city, args.country, args.slug)
        if cache:
            rel_id      = cache["rel_id"]
            admin_level = cache["admin_level"]
            found_level = cache["found_level"]
            cached_districts = [
                {"id": d["id"], "name": d["name"],
                 "points": [tuple(p) for p in d["points"]] if d["points"] else None}
                for d in cache["districts"]
            ]
            print(f"    (rel:{rel_id}  al:{admin_level})")
        else:
            rel_id, admin_level, display_name = find_city(args.city, args.country)
            print(f"    {display_name}  (rel:{rel_id}  al:{admin_level})")
            time.sleep(1)

            print(f"\n🗂   Finding districts…")
            raw_districts, found_level = find_districts(rel_id, admin_level, args.admin_level)
            if not raw_districts:
                print("❌  No districts found. Try --admin-level to specify manually.")
                sys.exit(1)
            # For non-Latin-script cities (Taipei, Tokyo, Seoul, etc.) show
            # the English name first with the native script in parentheses,
            # so English-speaking users can read it while locals get visual
            # confirmation. Falls back gracefully when one side is missing.
            def _pick_name(tags, idx):
                native = tags.get("name")
                english = tags.get("name:en") or tags.get("name:en-US")
                if english and native and english != native:
                    return f"{english} ({native})"
                return english or native or f"District {idx + 1}"
            cached_districts = [
                {"id": d["id"],
                 "name": _pick_name(d.get("tags") or {}, i),
                 "points": None}
                for i, d in enumerate(raw_districts)
            ]
            save_cache(args.city, args.country, rel_id, admin_level, found_level, cached_districts, args.slug)

        # ── 3. Fetch polygons (resume-aware) ──────────────────────────────
        total   = len(cached_districts)
        pending = sum(1 for d in cached_districts if d["points"] is None)
        print(f"\n⬇️   Downloading polygons  ({total - pending} cached, {pending} remaining)…")

        for i, d in enumerate(cached_districts):
            if d["points"] is not None:
                continue
            print(f"    [{i+1:>2}/{total}] {d['name']}", end="  ", flush=True)
            try:
                pts = fetch_polygon(d["id"])
                if len(pts) < 3:
                    print("skip (too few points)")
                    d["points"] = []
                else:
                    d["points"] = pts
                    print(f"({len(pts)} pts)")
            except Exception as e:
                print(f"ERROR: {e}")
            save_cache(args.city, args.country, rel_id, admin_level, found_level, cached_districts, args.slug)
            time.sleep(3)

        fetched = [d for d in cached_districts if d.get("points")]
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
            c     = centroid([centroid(d["points"]) for d in group])
            label = min(group, key=lambda d: _dist2(centroid(d["points"]), c))["name"]
            pts   = _union_polygons(group)
            regions_raw.append({"name": label, "points": pts,
                                 "members": [d["name"] for d in group],
                                 "osmId": None, "adminLevel": None})
            print(f"    '{label}': {', '.join(d['name'] for d in group)}")
    else:
        regions_raw = [{"name": d["name"], "points": d["points"],
                        "members": [d["name"]], "osmId": d.get("id"),
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

    if args.geojson_url:
        method = "geojson"
    elif args.shapefile:
        method = "shapefile"
    else:
        method = "osm"

    source = {"method": method}
    if args.geojson_url:
        source["url"] = args.geojson_url
    if method == "osm":
        source["license"]     = args.source_license or "ODbL-1.0"
        source["attribution"] = args.source_attribution or "© OpenStreetMap contributors"
    else:
        source["license"]     = args.source_license or "unknown"
        source["attribution"] = args.source_attribution or "unknown"

    city_entry = {
        "name":        args.city,
        "countryCode": args.country.upper(),
        "regions":     seed_regions,
        "source":      source,
    }

    # ── 6. Preview ────────────────────────────────────────────────────────
    print(f"\n🗺   Writing preview…")
    preview = write_preview(args.city, args.country, seed_regions, clustered_from, args.slug)
    print(f"    open {preview}")

    if args.dry_run:
        print(f"\n✅  Dry run — {len(seed_regions)} regions, no files written.\n")
        return

    # ── 7. Write cities/<slug>.json, bump VERSION, rebuild ───────────────
    CITIES_DIR.mkdir(exist_ok=True)
    city_path = CITIES_DIR / f"{slugify(args.city, args.country, args.slug)}.json"
    if city_path.exists():
        print(f"\n📝  Replacing existing {city_path.name}…")
    city_path.write_text(json.dumps(city_entry, indent=2, ensure_ascii=False) + "\n")

    version = int(VERSION_PATH.read_text().strip()) + 1
    VERSION_PATH.write_text(f"{version}\n")

    subprocess.run([sys.executable, str(ROOT / "build.py")], check=True)

    if not args.geojson_url and not args.shapefile:
        clear_cache(args.city, args.country, args.slug)
    if source["license"] == "unknown" or source["attribution"] == "unknown":
        print(f"⚠️   Fill in source license/attribution in {city_path.name} "
              f"(or pass --source-license / --source-attribution)")
    print(f"✅  {city_path.name} — {len(seed_regions)} regions, catalog v{version}\n")

if __name__ == "__main__":
    main()
