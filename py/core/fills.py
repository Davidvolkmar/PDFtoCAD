"""Rellenos solidos: se reconstruyen como HATCH SOLID y, opcionalmente, se
fusionan los fragmentos adyacentes del mismo color (los exportadores suelen
partir un relleno en triangulos o franjas)."""
from collections import defaultdict

import numpy as np
import shapely

from .compat import coverage_union

from . import geom
from .extract import rings_to_geometry


def build_fills(pd, opts):
    merge = bool(opts.get("merge_fills", True))
    skip_white = bool(opts.get("skip_white_fills", True))
    page_area = pd.width * pd.height
    groups = defaultdict(list)
    singles = []
    skipped = 0
    simple = {}  # n_vertices -> [(pi, key, ring)]
    for pi, path in enumerate(pd.paths):
        if path["kind"] not in ("fill", "both") or path["fill"] is None:
            continue
        if path.get("fill_opacity") is not None and path["fill_opacity"] < 0.02:
            continue
        if skip_white and min(path["fill"]) >= 250:
            skipped += 1
            continue
        key = (path["layer"], path["fill"])
        sps = path["subpaths"]
        # camino rapido: un solo anillo de rectas sin recorte (triangulos, franjas...)
        if len(sps) == 1 and path["clip"] is None and all(sg[0] == "L" for sg in sps[0]["segs"]):
            ring = [sg[1] for sg in sps[0]["segs"]]
            if len(ring) >= 3:
                simple.setdefault(len(ring), []).append((pi, key, ring))
            continue
        rings = [geom.flatten_subpath(sp["segs"]) for sp in sps]
        g = rings_to_geometry(rings, path["even_odd"])
        if g is None or g.is_empty:
            continue
        if path["clip"] is not None:
            g = g.intersection(pd.clips[path["clip"]])
            if g.is_empty:
                continue
        if g.area > 0.9 * page_area:  # fondo de pagina
            skipped += 1
            continue
        if merge:
            groups[key].append((pi, shapely.set_precision(g, 0.01)))
        else:
            singles.append({"key": key, "polygon": g, "refs": [pi]})

    # poligonos simples creados en bloque (vectorizado)
    for n, items in simple.items():
        # redondeo a 0.01 pt: los bordes compartidos coinciden exactamente
        polys = shapely.polygons(np.round(np.array([it[2] for it in items], dtype=float), 2))
        bad = ~shapely.is_valid(polys)
        if bad.any():
            polys[bad] = shapely.make_valid(polys[bad])
        areas = shapely.area(polys)
        for (pi, key, _), g, a in zip(items, polys, areas):
            if a < 1e-6:
                continue
            if a > 0.9 * page_area:
                skipped += 1
                continue
            if merge:
                groups[key].append((pi, g))
            else:
                singles.append({"key": key, "polygon": g, "refs": [pi]})

    out = list(singles)
    for key, items in groups.items():
        # buffer minimo para soldar fragmentos que comparten borde
        u = _fast_union([g for _, g in items])
        polys = list(getattr(u, "geoms", [u]))
        refs = [pi for pi, _ in items]
        for p in polys:
            if p.geom_type == "Polygon" and p.area > 0.5:
                out.append({"key": key, "polygon": p.simplify(0.01), "refs": refs})
    return out, skipped


def _fast_union(geoms):
    """Union rapida de fragmentos.
    1) se ajustan a una rejilla de 0.01 pt para que los bordes compartidos coincidan;
    2) se separan en grupos que se tocan (consulta vectorizada con STRtree), porque
       unir miles de piezas de golpe crece mas que linealmente;
    3) cada grupo se une por cobertura (muy rapido) o, si hay solapes, union general."""
    arr = np.array(geoms, dtype=object)  # ya vienen en rejilla de 0.01 pt
    arr = arr[~shapely.is_empty(arr)]
    n = len(arr)
    if n == 0:
        return shapely.GeometryCollection()
    if n == 1:
        return arr[0]
    tree = shapely.STRtree(arr)
    # solape de cajas (sin predicado exacto: mucho mas rapido; agrupar de mas es inocuo)
    a_idx, b_idx = tree.query(arr)
    parent = np.arange(n)

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    m = a_idx < b_idx
    for i, j in zip(a_idx[m].tolist(), b_idx[m].tolist()):
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[rj] = ri
    groups = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    parts = []
    for idx in groups.values():
        sub = arr[idx]
        parts.append(sub[0] if len(idx) == 1 else _union_group(sub))
    out = []
    for g in parts:
        out.extend(getattr(g, "geoms", [g]))
    return shapely.MultiPolygon([g for g in out if g.geom_type == "Polygon"]) if len(out) > 1 else out[0]


def _union_group(sub):
    total = float(shapely.area(sub).sum())
    u = coverage_union(sub)
    if u is not None and u.is_valid and abs(u.area - total) <= 0.002 * max(total, 1e-9):
        return u
    return shapely.union_all(sub)
