"""Utilidades geometricas: Bezier, arcos, aplanado de trayectorias."""
import math

EPS = 1e-6


def dist(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def bezier_point(p0, p1, p2, p3, t):
    mt = 1 - t
    a, b, c, d = mt * mt * mt, 3 * mt * mt * t, 3 * mt * t * t, t * t * t
    return (a * p0[0] + b * p1[0] + c * p2[0] + d * p3[0],
            a * p0[1] + b * p1[1] + c * p2[1] + d * p3[1])


def flatten_bezier(p0, p1, p2, p3, n=None):
    if n is None:
        L = dist(p0, p1) + dist(p1, p2) + dist(p2, p3)
        n = max(4, min(32, int(L / 2) + 1))
    return [bezier_point(p0, p1, p2, p3, i / n) for i in range(n + 1)]


def circumcircle(a, b, c):
    ax, ay = a
    bx, by = b
    cx, cy = c
    d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-9:
        return None
    ux = ((ax * ax + ay * ay) * (by - cy) + (bx * bx + by * by) * (cy - ay) + (cx * cx + cy * cy) * (ay - by)) / d
    uy = ((ax * ax + ay * ay) * (cx - bx) + (bx * bx + by * by) * (ax - cx) + (cx * cx + cy * cy) * (bx - ax)) / d
    return (ux, uy), math.hypot(ax - ux, ay - uy)


def is_straight_bezier(p0, p1, p2, p3, tol):
    """True si los puntos de control estan sobre la cuerda."""
    L = dist(p0, p3)
    if L < EPS:
        return dist(p0, p1) < tol and dist(p0, p2) < tol
    ux, uy = (p3[0] - p0[0]) / L, (p3[1] - p0[1]) / L
    for p in (p1, p2):
        if abs((p[0] - p0[0]) * uy - (p[1] - p0[1]) * ux) > tol:
            return False
    return True


def bezier_to_arc(p0, p1, p2, p3, rel_tol=0.004, abs_tol=0.05):
    """Si la Bezier es (casi) un arco circular devuelve (centro, radio, ccw)."""
    pm = bezier_point(p0, p1, p2, p3, 0.5)
    cc = circumcircle(p0, pm, p3)
    if cc is None:
        return None
    c, r = cc
    if r > 1e6:
        return None
    tol = max(abs_tol, rel_tol * r)
    for t in (0.15, 0.3, 0.7, 0.85):
        q = bezier_point(p0, p1, p2, p3, t)
        if abs(dist(q, c) - r) > tol:
            return None
    # sentido de giro (sistema y hacia arriba)
    cross = (p0[0] - c[0]) * (pm[1] - c[1]) - (p0[1] - c[1]) * (pm[0] - c[0])
    # arcos de mas de 180 grados en una sola Bezier son raros; validar barrido
    sweep = arc_sweep(p0, p3, c, cross > 0)
    if sweep > math.pi * 1.2:
        return None
    return c, r, cross > 0


def arc_sweep(p0, p1, c, ccw):
    a0 = math.atan2(p0[1] - c[1], p0[0] - c[0])
    a1 = math.atan2(p1[1] - c[1], p1[0] - c[0])
    s = a1 - a0
    if ccw:
        while s <= 0:
            s += 2 * math.pi
    else:
        while s >= 0:
            s -= 2 * math.pi
        s = -s
    return s


def flatten_arc(p0, p1, c, r, ccw, step_deg=6):
    a0 = math.atan2(p0[1] - c[1], p0[0] - c[0])
    sw = arc_sweep(p0, p1, c, ccw)
    n = max(2, int(math.degrees(sw) / step_deg) + 1)
    sgn = 1 if ccw else -1
    return [(c[0] + r * math.cos(a0 + sgn * sw * i / n), c[1] + r * math.sin(a0 + sgn * sw * i / n))
            for i in range(n + 1)]


def flatten_segment(seg):
    """Devuelve lista de puntos de un segmento ('L'|'C'|'A')."""
    k = seg[0]
    if k == "L":
        return [seg[1], seg[2]]
    if k == "C":
        return flatten_bezier(seg[1], seg[2], seg[3], seg[4])
    if k == "A":
        return flatten_arc(seg[1], seg[2], seg[3], seg[4], seg[5])
    raise ValueError(k)


def seg_start(seg):
    return seg[1]


def seg_end(seg):
    return seg[4] if seg[0] == "C" else seg[2]


def flatten_subpath(segs):
    pts = []
    for s in segs:
        fp = flatten_segment(s)
        if pts and dist(pts[-1], fp[0]) < 1e-6:
            fp = fp[1:]
        pts.extend(fp)
    return pts


def bulge(seg):
    """Bulge de un segmento de arco para LWPOLYLINE."""
    sw = arc_sweep(seg[1], seg[2], seg[3], seg[5])
    b = math.tan(sw / 4)
    return b if seg[5] else -b
