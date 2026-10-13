"""Extraccion de la geometria vectorial de una pagina PDF.

Todo se devuelve en "coordenadas de papel": puntos PDF (1/72 in) con el eje Y
hacia arriba y el origen en la esquina inferior izquierda de la pagina visible
(ya con la rotacion de pagina aplicada).
"""
import math
import re

import pymupdf
from shapely.geometry import LineString, Polygon, box
from shapely.validation import make_valid

from . import geom


def _rgb(c):
    if c is None:
        return None
    if isinstance(c, (int, float)):
        c = (c, c, c)
    if len(c) == 1:
        c = (c[0], c[0], c[0])
    if len(c) == 4:  # CMYK aproximado
        C, M, Y, K = c
        c = ((1 - C) * (1 - K), (1 - M) * (1 - K), (1 - Y) * (1 - K))
    return tuple(int(round(max(0, min(1, v)) * 255)) for v in c[:3])


def parse_dashes(s):
    if not s:
        return None
    m = re.match(r"\[\s*([^\]]*)\]\s*([-\d.]*)", s)
    if not m:
        return None
    vals = [float(v) for v in m.group(1).split() if v]
    if not vals or all(v == 0 for v in vals):
        return None
    if len(vals) % 2:
        vals = vals * 2
    return vals


def _ring_polygon(r):
    try:
        p = Polygon(r)
    except Exception:
        return None, 0.0
    # area con signo (orientacion) antes de corregir
    signed = 0.0
    for i in range(len(r)):
        x1, y1 = r[i]
        x2, y2 = r[(i + 1) % len(r)]
        signed += x1 * y2 - x2 * y1
    if not p.is_valid:
        p = make_valid(p)
        if p.geom_type not in ("Polygon", "MultiPolygon"):
            p = p.buffer(0)
    if p.is_empty or p.area < 1e-6:
        return None, 0.0
    return p, signed / 2


def rings_to_geometry(rings, even_odd=True):
    """Convierte anillos (listas de puntos) en un poligono shapely respetando la
    regla de relleno del PDF (par-impar o nonzero)."""
    polys = []
    for r in rings:
        if len(r) < 3:
            continue
        p, signed = _ring_polygon(r)
        if p is not None:
            polys.append((p, signed))
    if not polys:
        return None
    if even_odd or len(polys) == 1:
        result = None
        for p, _ in polys:
            result = p if result is None else result.symmetric_difference(p)
        return result
    # nonzero: los anillos con la orientacion del mayor suman, los opuestos restan
    ref = max(polys, key=lambda x: abs(x[1]))[1] >= 0
    pos = [p for p, sg in polys if (sg >= 0) == ref]
    neg = [p for p, sg in polys if (sg >= 0) != ref]
    from shapely.ops import unary_union
    result = unary_union(pos)
    if neg:
        result = result.difference(unary_union(neg))
    return result


class PageData:
    def __init__(self):
        self.width = 0.0
        self.height = 0.0
        self.paths = []
        self.clips = []
        self.texts = []
        self.images = []
        self.ocgs = []
        self.n_raw_paths = 0
        self.cache = {}


def extract_page(doc, pno, clip_geometry=True, progress=None):
    """progress(fraccion 0..1, mensaje) opcional."""
    prog = progress or (lambda f, m="": None)
    page = doc[pno]
    rot = page.rotation_matrix
    W, H = page.rect.width, page.rect.height
    pd = PageData()
    pd.width, pd.height = W, H
    page_box = box(0, 0, W, H)

    # transformacion afin en Python puro: Point*Matrix de PyMuPDF es ~20x mas lento
    ra, rb, rc, rd, re_, rf = rot.a, rot.b, rot.c, rot.d, rot.e, rot.f
    identity = (ra, rb, rc, rd, re_, rf) == (1, 0, 0, 1, 0, 0)

    def T(p):
        x, y = p[0], p[1]
        if identity:
            return (round(x, 4), round(H - y, 4))
        return (round(ra * x + rc * y + re_, 4), round(H - (rb * x + rd * y + rf), 4))

    prog(0.05, "Leyendo vectores del PDF (en planos grandes tarda unos segundos)")
    try:
        drawings = page.get_drawings(extended=True)
    except TypeError:
        drawings = page.get_drawings()
    pd.n_raw_paths = len(drawings)
    nd = max(1, len(drawings))
    step = max(1, nd // 50)
    prog(0.35, "Interpretando %d trazados" % nd)

    clip_stack = []  # (level, clip_index or None)

    for di, d in enumerate(drawings):
        if di % step == 0:
            prog(0.35 + 0.5 * di / nd, "Interpretando trazados %d / %d" % (di, nd))
        level = d.get("level", 0)
        while clip_stack and clip_stack[-1][0] >= level:
            clip_stack.pop()
        cur_clip = clip_stack[-1][1] if clip_stack else None
        typ = d.get("type")

        if typ == "group":
            clip_stack.append((level, cur_clip))
            continue

        subpaths = _items_to_subpaths(d.get("items", []), T, d.get("closePath", False))

        if typ == "clip":
            if not clip_geometry:
                clip_stack.append((level, None))
                continue
            g = rings_to_geometry([geom.flatten_subpath(sp["segs"]) for sp in subpaths],
                                  d.get("even_odd", True) is not False)
            idx = None
            if g is not None and not g.is_empty:
                if cur_clip is not None:
                    g = g.intersection(pd.clips[cur_clip])
                # recorte trivial (cubre toda la pagina) -> se ignora
                if g.buffer(1.0).contains(page_box):
                    idx = cur_clip
                else:
                    pd.clips.append(g)
                    idx = len(pd.clips) - 1
            clip_stack.append((level, idx))
            continue

        if not subpaths:
            continue
        has_fill = typ in ("f", "fs") and d.get("fill") is not None
        has_stroke = typ in ("s", "fs") and d.get("color") is not None
        if not has_fill and not has_stroke:
            continue
        pd.paths.append({
            "kind": "both" if (has_fill and has_stroke) else ("fill" if has_fill else "stroke"),
            "subpaths": subpaths,
            "color": _rgb(d.get("color")) if has_stroke else None,
            "fill": _rgb(d.get("fill")) if has_fill else None,
            "width": float(d.get("width") or 0.0) * _scale_of(rot),
            "dashes": parse_dashes(d.get("dashes")),
            "layer": d.get("layer") or "",
            "even_odd": bool(d.get("even_odd")),
            "clip": cur_clip,
            "fill_opacity": d.get("fill_opacity"),
        })

    if clip_geometry and pd.clips:
        prog(0.86, "Aplicando recortes")
        _apply_clips(pd)

    # ----- textos -----
    prog(0.92, "Leyendo textos")
    try:
        tdict = page.get_text("dict", flags=pymupdf.TEXT_PRESERVE_WHITESPACE)
    except Exception:
        tdict = {"blocks": []}
    a, b, c, dd = rot.a, rot.b, rot.c, rot.d
    for bl in tdict.get("blocks", []):
        if bl.get("type") != 0:
            continue
        for ln in bl.get("lines", []):
            dx, dy = ln.get("dir", (1, 0))
            vx, vy = dx * a + dy * c, dx * b + dy * dd
            ang = math.degrees(math.atan2(-vy, vx))
            for sp in ln.get("spans", []):
                txt = sp.get("text", "")
                if not txt.strip():
                    continue
                o = T(sp.get("origin", (sp["bbox"][0], sp["bbox"][3])))
                col = sp.get("color", 0)
                pd.texts.append({
                    "text": txt.rstrip(),
                    "x": o[0], "y": o[1],
                    "size": float(sp.get("size", 10)),
                    "angle": round(ang, 3),
                    "font": sp.get("font", ""),
                    "color": ((col >> 16) & 255, (col >> 8) & 255, col & 255),
                })

    # ----- imagenes (para detectar PDFs escaneados) -----
    try:
        for info in page.get_image_info():
            r = pymupdf.Rect(info["bbox"]) * rot
            pd.images.append((r.x0, H - r.y1, r.x1, H - r.y0))
    except Exception:
        pass

    try:
        pd.ocgs = sorted({v["name"] for v in doc.get_ocgs().values()})
    except Exception:
        pd.ocgs = []
    pd.cache = {}
    prog(1.0, "Lectura terminada")
    return pd


def _scale_of(rot):
    return math.hypot(rot.a, rot.b) or 1.0


def _items_to_subpaths(items, T, close_path):
    subpaths = []
    cur = None

    def start_new(p):
        nonlocal cur
        cur = {"segs": [], "closed": False}
        subpaths.append(cur)

    for it in items:
        k = it[0]
        if k == "l":
            p1, p2 = T(it[1]), T(it[2])
            if cur is None or geom.dist(geom.seg_end(cur["segs"][-1]) if cur["segs"] else p1, p1) > 1e-3:
                start_new(p1)
            if geom.dist(p1, p2) > 1e-6:
                cur["segs"].append(("L", p1, p2))
        elif k == "c":
            p0, p1, p2, p3 = T(it[1]), T(it[2]), T(it[3]), T(it[4])
            if cur is None or (cur["segs"] and geom.dist(geom.seg_end(cur["segs"][-1]), p0) > 1e-3):
                start_new(p0)
            cur["segs"].append(("C", p0, p1, p2, p3))
        elif k == "re":
            r = it[1]
            pts = [T((r.x0, r.y0)), T((r.x1, r.y0)), T((r.x1, r.y1)), T((r.x0, r.y1))]
            start_new(pts[0])
            cur["segs"] = [("L", pts[i], pts[(i + 1) % 4]) for i in range(4)]
            cur["closed"] = True
            cur = None
        elif k == "qu":
            q = it[1]
            pts = [T(q.ul), T(q.ur), T(q.lr), T(q.ll)]
            start_new(pts[0])
            cur["segs"] = [("L", pts[i], pts[(i + 1) % 4]) for i in range(4)]
            cur["closed"] = True
            cur = None

    out = []
    for sp in subpaths:
        segs = sp["segs"]
        if not segs:
            continue
        if not sp["closed"]:
            if geom.dist(geom.seg_start(segs[0]), geom.seg_end(segs[-1])) < 1e-3 and len(segs) > 1:
                sp["closed"] = True
        out.append(sp)
    if close_path and out and not out[-1]["closed"]:
        sp = out[-1]
        a, b = geom.seg_end(sp["segs"][-1]), geom.seg_start(sp["segs"][0])
        if geom.dist(a, b) > 1e-6:
            sp["segs"].append(("L", a, b))
        sp["closed"] = True
    return out


def _apply_clips(pd):
    """Recorta trazos rectos por su trayectoria de recorte (p.ej. patrones de Revit)."""
    from shapely.prepared import prep
    prepared = {}
    for path in pd.paths:
        ci = path["clip"]
        if ci is None or path["kind"] == "fill":
            continue
        cg = pd.clips[ci]
        if ci not in prepared:
            prepared[ci] = prep(cg)
        pg = prepared[ci]
        new_subpaths = []
        for sp in path["subpaths"]:
            pts = geom.flatten_subpath(sp["segs"])
            ls = LineString(pts) if len(pts) > 1 else None
            if ls is None:
                continue
            if pg.contains(ls):
                new_subpaths.append(sp)
                continue
            if not pg.intersects(ls):
                continue
            # solo se recortan segmentos rectos; las curvas parciales se aplanan
            for seg in sp["segs"]:
                fp = geom.flatten_segment(seg)
                sl = LineString(fp)
                if pg.contains(sl):
                    new_subpaths.append({"segs": [seg], "closed": False})
                    continue
                inter = sl.intersection(cg)
                for g in getattr(inter, "geoms", [inter]):
                    if g.geom_type == "LineString" and g.length > 1e-3:
                        cs = list(g.coords)
                        new_subpaths.append({"segs": [("L", tuple(cs[i]), tuple(cs[i + 1]))
                                                      for i in range(len(cs) - 1)], "closed": False})
        path["subpaths"] = new_subpaths
