"""Limpieza de contornos: anillos validos y cerrados, sin solapes entre hatches
ni entre rellenos, para que en CAD cada HATCH tenga un contorno limpio."""
import math

import numpy as np
import shapely
from shapely.geometry import MultiPolygon, Polygon
from shapely.geometry.polygon import orient


def clean_polygon(g, min_area=0.05):
    """Devuelve un (Multi)Polygon valido: anillos cerrados, sin vertices repetidos ni
    colineales, exterior antihorario e islas en sentido horario. None si no queda nada."""
    if g is None or g.is_empty:
        return None
    if not g.is_valid:
        g = shapely.make_valid(g)
    parts = []
    for p in shapely.get_parts(g):
        if p.geom_type == "Polygon":
            parts.append(p)
        elif p.geom_type in ("MultiPolygon", "GeometryCollection"):
            parts.extend(q for q in shapely.get_parts(p) if q.geom_type == "Polygon")
    out = []
    for p in parts:
        # quita vertices duplicados/colineales sin mover el contorno
        p = p.simplify(0, preserve_topology=True)
        if p.is_empty or p.area < min_area:
            continue
        holes = [r for r in p.interiors if Polygon(r).area >= min_area]
        p = orient(Polygon(p.exterior, holes), sign=1.0)
        if len(p.exterior.coords) >= 4:  # 3 vertices + cierre
            out.append(p)
    if not out:
        return None
    return out[0] if len(out) == 1 else MultiPolygon(out)


def _same_pattern(a, b):
    if a["key"] != b["key"] or len(a["families"]) != len(b["families"]):
        return False
    for fa, fb in zip(sorted(a["families"], key=lambda f: f["angle"]),
                      sorted(b["families"], key=lambda f: f["angle"])):
        d = abs(fa["angle"] - fb["angle"]) % 180
        if min(d, 180 - d) > 1 or abs(fa["spacing"] - fb["spacing"]) > 0.04 * fa["spacing"]:
            return False
        if bool(fa["dashes"]) != bool(fb["dashes"]):
            return False
    return True


def resolve_hatch_overlaps(hatches, min_ratio=0.01):
    """Hatches activos que se pisan: si son del mismo patron se fusionan en uno; si no,
    al mayor se le resta la zona comun (el menor suele ser el mas especifico).
    Devuelve (lista_resultante, n_fusionados, n_recortados)."""
    act = [h for h in hatches if not h.get("rejected")]
    for h in act:
        h["polygon"] = clean_polygon(h["polygon"]) or h["polygon"]
    if len(act) < 2:
        return hatches, 0, 0
    polys = np.array([h["polygon"] for h in act], dtype=object)
    tree = shapely.STRtree(polys)
    a_idx, b_idx = tree.query(polys, predicate="intersects")
    m = a_idx < b_idx
    pairs = list(zip(a_idx[m].tolist(), b_idx[m].tolist()))
    if not pairs:
        return hatches, 0, 0
    gone = set()
    merged = trimmed = 0
    for i, j in pairs:
        if i in gone or j in gone:
            continue
        A, B = act[i], act[j]
        inter = A["polygon"].intersection(B["polygon"]).area
        if inter <= min_ratio * min(A["polygon"].area, B["polygon"].area):
            continue
        if _same_pattern(A, B):
            A["polygon"] = clean_polygon(A["polygon"].union(B["polygon"])) or A["polygon"]
            A["refs"] = A["refs"] + B["refs"]
            A["n_lines"] += B["n_lines"]
            gone.add(j)
            merged += 1
        else:
            big, small = (A, B) if A["polygon"].area >= B["polygon"].area else (B, A)
            cut = clean_polygon(big["polygon"].difference(small["polygon"]))
            if cut is None:
                # el mayor queda cubierto entero: sobra
                gone.add(i if big is A else j)
            else:
                big["polygon"] = cut
            trimmed += 1
    ids_gone = {act[k]["id"] for k in gone}
    return [h for h in hatches if h["id"] not in ids_gone], merged, trimmed


def resolve_fill_overlaps(fills):
    """Rellenos solidos de distinto color que se pisan: como en el PDF, lo que se pinta
    despues tapa lo anterior; se recorta lo tapado para que no queden superpuestos."""
    for f in fills:
        f["polygon"] = clean_polygon(f["polygon"]) or f["polygon"]
    if len(fills) < 2:
        return fills, 0
    order = sorted(range(len(fills)), key=lambda k: min(fills[k]["refs"]) if fills[k]["refs"] else 0)
    fl = [fills[k] for k in order]
    polys = np.array([f["polygon"] for f in fl], dtype=object)
    tree = shapely.STRtree(polys)
    a_idx, b_idx = tree.query(polys, predicate="intersects")
    m = a_idx < b_idx  # a se pinto antes que b
    later = {}
    for a, b in zip(a_idx[m].tolist(), b_idx[m].tolist()):
        if fl[a]["color"] != fl[b]["color"] or fl[a]["layer"] != fl[b]["layer"]:
            later.setdefault(a, []).append(b)
    fixed = 0
    keep = []
    for k, f in enumerate(fl):
        if k in later:
            cover = shapely.union_all([fl[b]["polygon"] for b in later[k]])
            if f["polygon"].intersection(cover).area > 0.01 * f["polygon"].area:
                f["polygon"] = clean_polygon(f["polygon"].difference(cover))
                fixed += 1
        if f["polygon"] is not None:
            keep.append(f)
    return keep, fixed
