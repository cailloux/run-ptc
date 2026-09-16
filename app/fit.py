"""FIT course export: junction classification and the FIT binary encoder.

Design: docs/specs/fit-course-export.md. Message and field numbers, base
types, and the CRC-16 table are from the FIT SDK profile (checked against
the official garmin-fit-sdk Python package and a FIT-writing library's
source, not from memory alone) and verified by round-trip decoding with
garmin-fit-sdk in tests.
"""

import math
import struct
from dataclasses import dataclass

import psycopg

FIT_EPOCH = 631065600   # Unix seconds at 1989-12-31T00:00:00Z, the FIT epoch
SEMICIRCLE = 2 ** 31 / 180   # degrees -> semicircles

LatLng = tuple[float, float]

# course_point message (32), field `type` (5): the FIT profile's course
# point enum. Values from the FIT SDK profile.
CUE_TYPE = {
    "left": 6, "right": 7, "straight": 8,
    "left_fork": 16, "right_fork": 17,
    "slight_left": 19, "sharp_left": 20, "slight_right": 21, "sharp_right": 22,
    "u_turn": 23,
    "generic": 0,   # Connect's UI only offers this and other non-navigation
                    # types for manual editing; a fallback if the turn types
                    # turn out to be Garmin-reserved (see Step 0 findings).
}


@dataclass
class CoursePoint:
    lat: float
    lon: float
    distance_m: float
    type: str    # a key of CUE_TYPE
    name: str


# ---- geometry: bearings over a short window, not point to point --------------------------


def _bearing(a: LatLng, b: LatLng) -> float:
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    dlon = lon2 - lon1
    y = math.sin(dlon) * math.cos(lat2)
    x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return math.degrees(math.atan2(y, x)) % 360


def _haversine_m(a: LatLng, b: LatLng) -> float:
    r = 6371000.0
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def _bearing_over(latlngs: list[LatLng], idx: int, step: int, window_m: float) -> float:
    """The bearing from idx toward (step>0) or from (step<0) a point window_m away."""
    acc, j = 0.0, idx
    while 0 <= j + step < len(latlngs) and acc < window_m:
        acc += _haversine_m(latlngs[j], latlngs[j + step])
        j += step
    return _bearing(latlngs[idx], latlngs[j]) if step > 0 else _bearing(latlngs[j], latlngs[idx])


def turn_angle(latlngs: list[LatLng], idx: int, window_m: float) -> float:
    """Outgoing bearing minus incoming, normalized to -180..180. Negative is left."""
    inc = _bearing_over(latlngs, idx, -1, window_m)
    out = _bearing_over(latlngs, idx, +1, window_m)
    return ((out - inc + 180) % 360) - 180


def classify_turn(angle: float, thresholds: dict) -> str:
    a = abs(angle)
    if a > thresholds["sharp"]:
        return "u_turn"
    if a > thresholds["turn"]:
        return "sharp_left" if angle < 0 else "sharp_right"
    if a > thresholds["slight"]:
        return "left" if angle < 0 else "right"
    if a > thresholds["straight"]:
        return "slight_left" if angle < 0 else "slight_right"
    return "straight"


# ---- decision points: match a route line onto the stored graph ---------------------------


_CUE_NAME = {
    "left": "L path", "right": "R path",
    "left_fork": "L fork", "right_fork": "R fork",
    "slight_left": "Bear L", "slight_right": "Bear R",
    "sharp_left": "Sharp L", "sharp_right": "Sharp R",
    "straight": "Straight", "u_turn": "U-turn",
}


def _refine(cue: str, out_bearing: float, branch_bearings: list[float], settings) -> str | None:
    """A turn/straight classification, adjusted for other branches at the
    junction. Returns None when the cue should be dropped entirely.

    "straight" only fires (in "ambiguous" mode) when a branch leaves close
    enough to straight-ahead that staying straight needs confirming -- it
    stays "straight" either way, since the point is to say "yes, this one,
    ignore that branch," not to send the runner down it. A "slight" turn
    that has a near-parallel branch is a genuine fork -- which of two
    similar-looking paths to take -- so that one does get relabelled.
    """
    if cue == "straight":
        if settings.fit_straight_cues == "none":
            return None
        if settings.fit_straight_cues == "all":
            return "straight"
        for branch in branch_bearings:
            diff = ((branch - out_bearing + 180) % 360) - 180
            if abs(diff) <= 45:
                return "straight"
        return None   # unambiguous; no other branch could be mistaken for it
    if cue in ("slight_left", "slight_right"):
        for branch in branch_bearings:
            diff = ((branch - out_bearing + 180) % 360) - 180
            if abs(diff) <= 30:
                return "right_fork" if diff > 0 else "left_fork"
        return cue
    return cue


def find_course_points(conn: psycopg.Connection, latlngs: list[LatLng], settings,
                       snap_m: float = 5) -> list[CoursePoint]:
    """Decision points (graph vertices of degree >= 3) the route passes,
    classified from the line's own bearings, adjusted for other branches at
    the junction (see _refine), one cue per real intersection.

    Decision points within fit_cluster_m of each other are merged into one
    cue: real intersections are sometimes digitized as several close-by
    graph vertices (a crosswalk, a short connector), which independently
    would read as separate, often conflicting, cues for what a runner
    experiences as a single turn.

    A road crossed at grade -- the route stays on cart paths on both sides,
    with the road as an unused branch -- gets no cue at all for continuing
    straight or bearing slightly; only turning onto the road, or a real
    turn on the path itself, is worth calling out there.

    ponytail: matches the finished line back onto the graph by nearest-edge
    snap, since this runs on an already-built route's polyline (Step 0)
    rather than the vertex sequence pgr_withPoints returns while routing.
    Once the route builder carries junctions through edits (per the spec),
    building this straight from that sequence removes the snap step.
    """
    lats = [p[0] for p in latlngs]
    lons = [p[1] for p in latlngs]
    rows = conn.execute("""
        WITH p AS (
            SELECT i, ST_Transform(ST_SetSRID(ST_MakePoint(lon, lat), 4326), 32616) AS pt
            FROM unnest(%(lats)s::float8[], %(lons)s::float8[]) WITH ORDINALITY AS t(lat, lon, i)
        )
        SELECT p.i, e.source, e.target, e.layer
        FROM p CROSS JOIN LATERAL (
            SELECT re.source, re.target, s.layer, re.geom
            FROM route_edge re JOIN segment s ON s.id = re.segment_id
            WHERE ST_DWithin(re.geom, p.pt, %(snap_m)s) ORDER BY re.geom <-> p.pt LIMIT 1
        ) e
        ORDER BY p.i
    """, {"lats": lats, "lons": lons, "snap_m": snap_m}).fetchall()

    # (source, target, start_i, end_i, layer), 0-based.
    runs: list[tuple[int, int, int, int, str]] = []
    for i, source, target, layer in rows:
        i -= 1   # WITH ORDINALITY is 1-based
        if runs and runs[-1][0:2] == (source, target):
            runs[-1] = (source, target, runs[-1][2], i, layer)
        else:
            runs.append((source, target, i, i, layer))

    # (vertex, index in latlngs, layer in, layer out, neighbour in, neighbour
    # out). The in/out layers say whether the route itself is on a cart path
    # or a road either side of this vertex, used below to spot a road
    # crossed at grade; the in/out neighbours are the vertices the route
    # itself uses here, excluded below from "other branches" so the route's
    # own edges are never mistaken for an alternative to themselves.
    junctions = []
    for a, b in zip(runs, runs[1:]):
        shared = {a[0], a[1]} & {b[0], b[1]}
        if shared:
            v = shared.pop()
            nbr_in = a[0] if a[1] == v else a[1]
            nbr_out = b[1] if b[0] == v else b[0]
            junctions.append((v, a[3], a[4], b[4], nbr_in, nbr_out))

    if not junctions:
        return []
    vids = sorted({v for v, *_ in junctions})
    neighbours: dict[int, set[int]] = {}
    neighbour_layer: dict[int, dict[int, str]] = {}
    for v, nbr, layer in conn.execute("""
        SELECT v, nbr, s.layer FROM (
            SELECT source AS v, target AS nbr, segment_id FROM route_edge WHERE source = ANY(%(vids)s)
            UNION
            SELECT target AS v, source AS nbr, segment_id FROM route_edge WHERE target = ANY(%(vids)s)
        ) x JOIN segment s ON s.id = x.segment_id
    """, {"vids": vids}).fetchall():
        neighbours.setdefault(v, set()).add(nbr)
        neighbour_layer.setdefault(v, {})[nbr] = layer

    all_vids = sorted(set(vids) | {n for s in neighbours.values() for n in s})
    vertex_ll: dict[int, LatLng] = {row[0]: (row[1], row[2]) for row in conn.execute("""
        SELECT id, ST_Y(ST_Transform(geom, 4326)), ST_X(ST_Transform(geom, 4326))
        FROM route_vertex WHERE id = ANY(%(vids)s)
    """, {"vids": all_vids}).fetchall()}

    decisions = [j for j in junctions if len(neighbours.get(j[0], ())) >= 3]
    if not decisions:
        return []

    cum = [0.0]
    for a, b in zip(latlngs, latlngs[1:]):
        cum.append(cum[-1] + _haversine_m(a, b))

    # Decision vertices directly joined by a short edge are one physical
    # intersection (a crosswalk, a short connector splitting what's really
    # one complex), regardless of how far apart the route's own path
    # through it puts them. Union-find over those short edges only --
    # distance along the route would also merge two separate intersections
    # that just happen to sit close together, or under-merge a complex one
    # the route crosses at an angle.
    dvids = sorted({v for v, *_ in decisions})
    parent = {v: v for v in dvids}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in conn.execute("""
        SELECT source, target FROM route_edge
        WHERE source = ANY(%(vids)s) AND target = ANY(%(vids)s) AND length_m <= %(cluster_m)s
    """, {"vids": dvids, "cluster_m": settings.fit_cluster_m}).fetchall():
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    clusters: list[list[tuple[int, int, str, str]]] = [[decisions[0]]]
    for item in decisions[1:]:
        prev_vertex, prev_idx = clusters[-1][-1][0], clusters[-1][-1][1]
        # The same vertex right back-to-back is nearest-edge snap jitter --
        # two adjacent trace points briefly disagreeing on which edge they're
        # on -- not a real revisit, so it merges like any close cluster. The
        # same vertex much farther down the route (an out-and-back retrace)
        # is a separate pass through it and must not merge; union-find would
        # otherwise merge it with itself trivially since it's already its own
        # cluster root.
        same_vertex_nearby = (item[0] == prev_vertex
                               and cum[item[1]] - cum[prev_idx] <= settings.fit_cluster_m)
        different_but_linked = item[0] != prev_vertex and find(item[0]) == find(prev_vertex)
        if same_vertex_nearby or different_but_linked:
            clusters[-1].append(item)
        else:
            clusters.append([item])

    window = settings.fit_bearing_window_m
    out = []
    for cluster in clusters:
        entry_idx, exit_idx = cluster[0][1], cluster[-1][1]
        inc = _bearing_over(latlngs, entry_idx, -1, window)
        out_bearing = _bearing_over(latlngs, exit_idx, +1, window)
        angle = ((out_bearing - inc + 180) % 360) - 180
        cue = classify_turn(angle, settings.fit_turn_thresholds_deg)

        # Exclude the cluster's own vertices (another physical intersection
        # complex isn't a branch of itself) and the route's own entry and
        # exit neighbours (the path being run isn't a branch of itself
        # either -- without this, a short, dead-straight continuation can
        # match its own bearing and be mistaken for a confirming branch).
        used = {v for v, *_ in cluster} | {cluster[0][4], cluster[-1][5]}
        branch_layers = [
            (nbr, neighbour_layer[v].get(nbr))
            for v, *_ in cluster for nbr in neighbours[v] - used
        ]
        # A road crossed at grade (the route is on a cart path both sides,
        # one of its unused branches is the road) needs no cue while the
        # runner just keeps going straight or bears slightly -- only a real
        # turn onto the road, or a real turn on the path itself, is worth
        # calling out. The road branch also never counts toward "another
        # path leaves here too", so it can't turn into a spurious fork or
        # an "ignore that branch" straight cue either.
        entry_layer, exit_layer = cluster[0][2], cluster[-1][3]
        crossing = (entry_layer == "cartpath" and exit_layer == "cartpath"
                    and any(layer == "road" for _, layer in branch_layers))
        if crossing and cue in ("straight", "slight_left", "slight_right"):
            continue

        branch_bearings = [
            _bearing(vertex_ll[v], vertex_ll[nbr])
            for v, *_ in cluster for nbr in neighbours[v] - used
            if v in vertex_ll and nbr in vertex_ll and neighbour_layer[v].get(nbr) != "road"
        ]
        cue = _refine(cue, out_bearing, branch_bearings, settings)
        if cue is None:
            continue

        lat, lon = latlngs[entry_idx]
        out.append(CoursePoint(lat, lon, cum[entry_idx], cue, _CUE_NAME[cue]))
    return out


# ---- FIT binary encoding -------------------------------------------------------------------

_CRC_TABLE = (
    0x0000, 0xCC01, 0xD801, 0x1400, 0xF001, 0x3C00, 0x2800, 0xE401,
    0xA001, 0x6C00, 0x7800, 0xB401, 0x5000, 0x9C01, 0x8801, 0x4400,
)


def _crc16(data: bytes, crc: int = 0) -> int:
    for byte in data:
        tmp = _CRC_TABLE[crc & 0xF]
        crc = (crc >> 4) & 0x0FFF
        crc = crc ^ tmp ^ _CRC_TABLE[byte & 0xF]
        tmp = _CRC_TABLE[crc & 0xF]
        crc = (crc >> 4) & 0x0FFF
        crc = crc ^ tmp ^ _CRC_TABLE[(byte >> 4) & 0xF]
    return crc & 0xFFFF


# Base type bytes (FIT SDK profile).
_ENUM, _UINT8, _UINT16, _SINT32, _UINT32, _STRING = 0x00, 0x02, 0x84, 0x85, 0x86, 0x07

# (local message number, global message number, [(field_def_num, size, base_type)])
_FILE_ID = (0, 0, [(0, 1, _ENUM), (1, 2, _UINT16), (2, 2, _UINT16), (4, 4, _UINT32)])
_COURSE = (1, 31, [(4, 1, _ENUM), (5, 64, _STRING)])
_LAP = (2, 19, [(253, 4, _UINT32), (0, 1, _ENUM), (1, 1, _ENUM), (2, 4, _UINT32),
                (3, 4, _SINT32), (4, 4, _SINT32), (5, 4, _SINT32), (6, 4, _SINT32),
                (7, 4, _UINT32), (8, 4, _UINT32), (9, 4, _UINT32)])
_EVENT = (3, 21, [(253, 4, _UINT32), (0, 1, _ENUM), (1, 1, _ENUM)])
_RECORD = (4, 20, [(253, 4, _UINT32), (0, 4, _SINT32), (1, 4, _SINT32), (5, 4, _UINT32)])
_COURSE_POINT = (5, 32, [(1, 4, _UINT32), (2, 4, _SINT32), (3, 4, _SINT32), (4, 4, _UINT32),
                         (5, 1, _ENUM), (6, 16, _STRING)])

_SPORT_RUNNING = 1
_FILE_TYPE_COURSE = 6
_MANUFACTURER_DEVELOPMENT = 255
_EVENT_TIMER, _EVENT_TYPE_START, _EVENT_TYPE_STOP_ALL = 0, 0, 4


def _pack(fields: list[tuple[int, int, int]], values: list) -> bytes:
    out = b""
    for (_, size, base_type), value in zip(fields, values):
        if base_type == _STRING:
            out += value.encode("ascii", "replace")[:size - 1].ljust(size, b"\0")
        elif base_type in (_ENUM, _UINT8):
            out += struct.pack("<B", value)
        elif base_type == _UINT16:
            out += struct.pack("<H", value)
        elif base_type == _SINT32:
            out += struct.pack("<i", value)
        elif base_type == _UINT32:
            out += struct.pack("<I", value)
        else:
            raise ValueError(f"unhandled base type {base_type:#x}")
    return out


def _definition(mesg: tuple[int, int, list]) -> bytes:
    local_num, global_num, fields = mesg
    header = bytes([0x40 | local_num])
    body = struct.pack("<BBHB", 0, 0, global_num, len(fields))   # reserved, architecture(LE)
    for def_num, size, base_type in fields:
        body += struct.pack("<BBB", def_num, size, base_type)
    return header + body


def _data(mesg: tuple[int, int, list], values: list) -> bytes:
    local_num, _, fields = mesg
    return bytes([local_num]) + _pack(fields, values)


def _semicircles(lat: float, lon: float) -> tuple[int, int]:
    return round(lat * SEMICIRCLE), round(lon * SEMICIRCLE)


def encode_course(name: str, latlngs: list[LatLng], course_points: list[CoursePoint],
                  pace_min_per_mi: float, created_at: int, flavor: str = "generic") -> bytes:
    """A FIT course file: file_id, course, lap, event(start), one record per
    point, one course_point per cue, event(stop). created_at is a Unix
    timestamp; record timestamps are synthesized from it at a nominal pace.

    flavor "generic" (default) tags every course point with the FIT
    profile's generic waypoint type -- the real direction only shows up in
    `name` -- because Garmin Connect Web silently drops or reprocesses the
    real turn types on import. flavor "garmin" emits the real types, for
    side-loading straight onto a device that doesn't filter them.
    """
    meters_per_mi = 1609.344
    m_per_s = meters_per_mi / (pace_min_per_mi * 60)
    fit_time = created_at - FIT_EPOCH

    dists = [0.0]
    for a, b in zip(latlngs, latlngs[1:]):
        dists.append(dists[-1] + _haversine_m(a, b))
    total_m = dists[-1]
    total_s = total_m / m_per_s if m_per_s else 0

    body = b""
    body += _definition(_FILE_ID)
    body += _data(_FILE_ID, [_FILE_TYPE_COURSE, _MANUFACTURER_DEVELOPMENT, 0, fit_time])

    body += _definition(_COURSE)
    body += _data(_COURSE, [_SPORT_RUNNING, name])

    slat, slon = _semicircles(*latlngs[0])
    elat, elon = _semicircles(*latlngs[-1])
    body += _definition(_LAP)
    body += _data(_LAP, [fit_time, _EVENT_TIMER, _EVENT_TYPE_START, fit_time,
                        slat, slon, elat, elon,
                        round(total_s * 1000), round(total_s * 1000), round(total_m * 100)])

    body += _definition(_EVENT)
    body += _data(_EVENT, [fit_time, _EVENT_TIMER, _EVENT_TYPE_START])

    body += _definition(_RECORD)
    for (lat, lon), dist in zip(latlngs, dists):
        plat, plon = _semicircles(lat, lon)
        ts = fit_time + round(dist / m_per_s) if m_per_s else fit_time
        body += _data(_RECORD, [ts, plat, plon, round(dist * 100)])

    if course_points:
        body += _definition(_COURSE_POINT)
        for cp in course_points:
            plat, plon = _semicircles(cp.lat, cp.lon)
            ts = fit_time + round(cp.distance_m / m_per_s) if m_per_s else fit_time
            cue_type = CUE_TYPE["generic"] if flavor == "generic" else CUE_TYPE[cp.type]
            body += _data(_COURSE_POINT, [ts, plat, plon, round(cp.distance_m * 100),
                                          cue_type, cp.name])

    body += _definition(_EVENT)
    body += _data(_EVENT, [fit_time + round(total_s), _EVENT_TIMER, _EVENT_TYPE_STOP_ALL])

    header = struct.pack("<BBHI4s", 14, 0x10, 2149, len(body), b".FIT")
    header += struct.pack("<H", _crc16(header))
    return header + body + struct.pack("<H", _crc16(body, _crc16(header)))
