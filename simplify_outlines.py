"""
simplify_outlines.py — thin out region outlines for the built catalog without
moving any border by more than a few metres, and without opening gaps between
neighbours.

Nine tenths of region_seeds.json is encoded polylines, and most of their points
sit on nearly straight stretches. Each outline is cut into arcs at the points
where the set of regions sharing the border changes (a corner where three
regions meet, or where a region's border stops being shared). An arc shared by
two regions is simplified once and both regions get the same points, so a
border that was shared vertex for vertex stays shared vertex for vertex: the
apps match shared borders exactly when they merge a city's downloaded regions
into one outline, and point-in-polygon never finds a sliver between neighbours.

Arcs are simplified with Douglas-Peucker: a point is dropped only if it lies
within the tolerance of the straight line between the points kept on either
side, so no border moves by more than the tolerance (plus the 1e-5° rounding of
the encoding, about a metre). Outlines of different cities that share vertices
are handled the same way, as the whole catalog goes into one graph.

Each outline's northern-, southern-, western- and easternmost points are always
kept: the apps download a region's streets by the box around its outline, and
the Overpass proxy caches by the exact query, so a box that moved by a metre
would miss the cache for that region.

The source files in cities/ are not touched: build.py calls simplify_regions()
on the way to the built file.
"""

import math

# The apps judge a GPS fix against the outlines, and a phone's fix is off by
# 5-10 m on a good day: a border moved by 2 m changes no credit anyone can see.
DEFAULT_TOLERANCE_M = 2.0

M_PER_DEG = 111_320.0


def simplify_regions(rings, tolerance_m=DEFAULT_TOLERANCE_M):
    """
    rings: list of outlines, each a list of (lat, lng) points, open or closed.
    Returns the simplified outlines in the same order, each closed (first point
    repeated at the end) as the catalog stores them.

    Dropping points can make an outline cross itself where two of its stretches
    run within the tolerance of each other (a neck a few metres wide). Any ring
    that was sound and comes back crossing itself keeps its arcs as they were,
    and so do its neighbours along those arcs, so the border stays shared.
    """
    cleaned = [_clean(r) for r in rings]
    arcs_by_ring = _split_into_arcs(cleaned)
    fixed_points = set()
    for ring in cleaned:
        if ring:
            fixed_points.update(_extremes(ring))
    pinned = set()  # canonical arc keys left unsimplified
    for _ in range(4):
        out = _assemble(cleaned, arcs_by_ring, tolerance_m, pinned, fixed_points)
        broken = [i for i, (before, after) in enumerate(zip(cleaned, out))
                  if len(before) >= 4 and _is_simple(before) and not _is_simple(after)]
        if not broken:
            break
        for i in broken:
            pinned.update(_canonical(arc)[0] for arc in arcs_by_ring[i])
    return out


def _extremes(ring):
    """The ring's points with the least and greatest latitude and longitude."""
    return {min(ring, key=lambda p: p[0]), max(ring, key=lambda p: p[0]),
            min(ring, key=lambda p: p[1]), max(ring, key=lambda p: p[1])}


def _assemble(cleaned, arcs_by_ring, tolerance_m, pinned, fixed_points):
    cache = {}
    out = []
    for ring, arcs in zip(cleaned, arcs_by_ring):
        if len(ring) < 4:
            out.append(ring + [ring[0]] if ring else ring)
            continue
        pts = []
        for arc in arcs:
            key, reverse = _canonical(arc)
            simplified = cache.get(key)
            if simplified is None:
                simplified = cache[key] = (list(key) if key in pinned
                                           else _douglas_peucker(list(key), tolerance_m, fixed_points))
            seg = simplified[::-1] if reverse else simplified
            if pts:
                seg = seg[1:]  # shares its first point with the previous arc's last
            pts.extend(seg)
        if pts[0] != pts[-1]:
            pts.append(pts[0])
        out.append(pts if len(pts) >= 4 else ring + [ring[0]])
    return out


def _is_simple(ring):
    """Whether the outline does not cross or touch itself (shapely, as build.py has it)."""
    try:
        from shapely.geometry import Polygon
    except ImportError:  # no check possible; build.py refuses to run without shapely anyway
        return True
    pts = ring[:-1] if len(ring) > 1 and ring[0] == ring[-1] else ring
    if len(pts) < 3:
        return True
    return Polygon([(lng, lat) for lat, lng in pts]).is_valid


def _clean(ring):
    """Open ring with repeated consecutive points and the closing point removed."""
    pts = [tuple(p) for p in ring]
    out = []
    for p in pts:
        if not out or out[-1] != p:
            out.append(p)
    while len(out) > 1 and out[0] == out[-1]:
        out.pop()
    return out


def _split_into_arcs(rings):
    """
    For each ring, its vertices grouped into arcs. A new arc starts wherever the set
    of rings sharing the edge changes, so every arc is shared whole or not at all.
    A ring with no such change (an island, or one with no shared edges at all) is
    split at its first point and the point farthest from it, so Douglas-Peucker
    has two fixed ends to work between.
    """
    owners = {}
    for i, ring in enumerate(rings):
        n = len(ring)
        for k in range(n):
            owners.setdefault(_edge_key(ring[k], ring[(k + 1) % n]), set()).add(i)

    result = []
    for ring in rings:
        n = len(ring)
        if n < 4:
            result.append([ring])
            continue
        edge_owner = [frozenset(owners[_edge_key(ring[k], ring[(k + 1) % n])]) for k in range(n)]
        # vertex k sits between edge k-1 and edge k
        cuts = [k for k in range(n) if edge_owner[k - 1] != edge_owner[k]]
        if len(cuts) < 2:
            far = max(range(1, n), key=lambda k: _dist2(ring[0], ring[k]))
            cuts = [0, far]
        arcs = []
        for a, b in zip(cuts, cuts[1:] + [cuts[0] + n]):
            arcs.append([ring[k % n] for k in range(a, b + 1)])
        result.append(arcs)
    return result


def _edge_key(a, b):
    return (a, b) if a <= b else (b, a)


def _canonical(arc):
    """The arc as a hashable key in a fixed direction, and whether it was reversed."""
    forward = tuple(arc)
    backward = forward[::-1]
    if backward < forward:
        return backward, True
    return forward, False


def _dist2(a, b):
    return (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2


def _douglas_peucker(points, tolerance_m, fixed_points=frozenset()):
    """
    Iterative Douglas-Peucker; distances in metres on a local flat projection.
    Points in fixed_points are kept, and the stretches between them thinned on their own.
    """
    n = len(points)
    if n <= 2:
        return points
    k = math.cos(math.radians(sum(p[0] for p in points) / n))
    xy = [(p[1] * k * M_PER_DEG, p[0] * M_PER_DEG) for p in points]
    keep = [p in fixed_points for p in points]
    keep[0] = keep[-1] = True
    anchors = [i for i in range(n) if keep[i]]
    stack = list(zip(anchors, anchors[1:]))
    while stack:
        s, e = stack.pop()
        if e - s < 2:
            continue
        sx, sy = xy[s]
        ex, ey = xy[e]
        dx, dy = ex - sx, ey - sy
        length2 = dx * dx + dy * dy
        worst, idx = 0.0, -1
        for i in range(s + 1, e):
            px, py = xy[i]
            if length2 == 0.0:
                d = math.hypot(px - sx, py - sy)
            else:
                t = ((px - sx) * dx + (py - sy) * dy) / length2
                t = 0.0 if t < 0.0 else 1.0 if t > 1.0 else t
                d = math.hypot(px - sx - t * dx, py - sy - t * dy)
            if d > worst:
                worst, idx = d, i
        if worst > tolerance_m:
            keep[idx] = True
            stack.append((s, idx))
            stack.append((idx, e))
    return [p for p, kept in zip(points, keep) if kept]
