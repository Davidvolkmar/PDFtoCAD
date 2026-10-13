"""Orquesta la conversion: extraccion -> hatches -> rellenos -> lineas -> modelo."""
import math
import re
from collections import defaultdict

from . import geom
from .fills import build_fills
from .cleanup import resolve_fill_overlaps, resolve_hatch_overlaps
from .hatch import classify_pattern, detect_hatches

PT_TO_MM = 25.4 / 72

DEFAULTS = {
    # escala
    "scale_mode": "ratio",       # 'ratio' (1:N) | 'factor' (calibrado)
    "scale_ratio": 1.0,          # N de 1:N
    "units": "mm",               # unidades del dibujo: mm | cm | m
    "scale_factor": None,        # unidades de dibujo por punto PDF (modo 'factor')
    # capas
    "layer_mode": "ocg",         # ocg | color | color_width | single
    "hatch_layer": "same",       # same | suffix | single
    # hatches
    "detect_hatches": True,
    "hatch_style": "ansi",       # ansi | user
    "hatch_min_lines": 6,
    "hatch_max_spacing_mm": 12,
    "hatch_angle_tol": 0.5,
    "hatch_spacing_tol": 0.15,
    "hatch_gap_factor": 0.5,
    "hatch_dashes": True,
    "hatch_isolated_only": True, # solo tramos sueltos (no polilineas) pueden ser hatch
    "hatch_boundary": True,      # contorno como polilinea cerrada asociada al hatch
    "fix_overlaps": True,        # sin solapes entre hatches / entre rellenos
    "hatch_snap": True,          # ajustar el contorno a las lineas existentes (muros)
    "rejected_hatches": [],
    # rellenos
    "fills": True,
    "merge_fills": True,
    "skip_white_fills": True,
    # lineas
    "clip_paths": True,
    "curves_to_arcs": True,
    "join_polylines": True,
    "merge_collinear": True,
    "lineweights": True,
    "linetypes": True,
    # texto
    "texts": True,
    "text_height_factor": 0.72,
    # depuracion de capas
    "layer_rename": {},          # {capa_origen: capa_nueva}
    "layer_exclude": [],         # capas que no se exportan
}

# parametros que afectan a la deteccion de hatches (clave de cache)
HATCH_KEYS = ("hatch_min_lines", "hatch_max_spacing_mm", "hatch_angle_tol", "hatch_spacing_tol",
              "hatch_gap_factor", "hatch_dashes", "hatch_isolated_only")

UNIT_DIV = {"mm": 1.0, "cm": 10.0, "m": 1000.0}


def model_factor(opts):
    """Unidades de dibujo por punto PDF."""
    if opts.get("scale_mode") == "factor" and opts.get("scale_factor"):
        return float(opts["scale_factor"])
    return PT_TO_MM * float(opts.get("scale_ratio") or 1.0) / UNIT_DIV.get(opts.get("units", "mm"), 1.0)


def sanitize_layer(name):
    name = re.sub(r'[<>/\\":;?*|=`,]', "_", name or "").strip()
    return name[:200] or "0"


def _hex(c):
    return "%02X%02X%02X" % tuple(c) if c else "000000"


class Converter:
    def __init__(self, pd, opts):
        self.pd = pd
        self.o = dict(DEFAULTS)
        self.o.update({k: v for k, v in (opts or {}).items() if v is not None or k == "scale_factor"})

    # ---------------- capas ----------------
    def layer_for(self, src_layer, color, width, kind):
        mode = self.o["layer_mode"]
        if kind == "text":
            base = "TEXTO" if mode != "single" else "PDF"
            if mode == "color":
                base = "TEXTO_" + _hex(color)
            return base
        if mode == "ocg":
            base = src_layer or ("C_" + _hex(color))
        elif mode == "color":
            base = "C_" + _hex(color)
        elif mode == "color_width":
            base = "C_%s_W%03d" % (_hex(color), int(round(width * PT_TO_MM * 100)))
        else:
            base = "PDF"
        if kind in ("hatch", "fill"):
            hl = self.o["hatch_layer"]
            if hl == "suffix":
                base = base + "_HATCH"
            elif hl == "single":
                base = "HATCH"
        return sanitize_layer(base)

    # ---------------- conversion ----------------
    def run(self, progress=None):
        """progress(fraccion 0..1, mensaje) opcional."""
        prog = progress or (lambda f, m="": None)
        pd, o = self.pd, self.o
        cache = getattr(pd, "cache", None)
        if cache is None:
            cache = pd.cache = {}
        res = {"hatches": [], "fills": [], "lines": [], "polys": [], "circles": [],
               "arcs": [], "splines": [], "texts": [], "stats": {}}

        # --- hatches de patron (la deteccion se cachea por parametros) ---
        all_hatches = []
        if o["detect_hatches"]:
            hkey = ("hatch",) + tuple(o[k] for k in HATCH_KEYS)
            if hkey not in cache:
                cache[hkey] = detect_hatches(
                    pd, o, progress=lambda f, m="": prog(0.02 + 0.53 * f, m))
            all_hatches = [dict(h) for h in cache[hkey]]
        prog(0.56, "Clasificando patrones")
        rejected = set(int(x) for x in (o.get("rejected_hatches") or []))
        consumed = set()
        for h in all_hatches:
            h["rejected"] = h["id"] in rejected
            layer, color, width = h["key"]
            h["layer"] = self.layer_for(layer, color, width, "hatch")
            h["color"] = color
            h["pat"] = classify_pattern(h, o["hatch_style"])
            h["pattern"], h["ptype"] = h["pat"]["name"], h["pat"]["ptype"]
            if not h["rejected"]:
                consumed.update(h["refs"])
        res["hatches"] = all_hatches
        if o["hatch_snap"] and all_hatches:
            skey = ("snap", hkey, frozenset(rejected))
            if skey not in cache:
                prog(0.58, "Ajustando contornos a muros")
                snap_hatch_boundaries(pd, all_hatches, consumed,
                                      progress=lambda f, m="": prog(0.58 + 0.14 * f, m))
                cache[skey] = {h["id"]: h["polygon"] for h in all_hatches if h.get("snapped")}
            for h in all_hatches:
                if h["id"] in cache[skey]:
                    h["polygon"] = cache[skey][h["id"]]
                    h["snapped"] = True
        n_hmerged = n_htrim = n_ftrim = 0
        if all_hatches and o["fix_overlaps"]:
            prog(0.72, "Limpiando contornos y solapes de hatches")
            all_hatches, n_hmerged, n_htrim = resolve_hatch_overlaps(all_hatches)
            res["hatches"] = all_hatches

        # --- rellenos solidos ---
        n_white = 0
        if o["fills"]:
            prog(0.73, "Fusionando rellenos solidos")
            fkey = ("fills", o["merge_fills"], o["skip_white_fills"])
            if fkey not in cache:
                cache[fkey] = build_fills(pd, o)
            fills, n_white = cache[fkey]
            fills = [dict(f) for f in fills]
            for f in fills:
                layer, color = f["key"]
                f["layer"] = self.layer_for(layer, color, 0, "fill")
                f["color"] = color
            if o["fix_overlaps"]:
                fills, n_ftrim = resolve_fill_overlaps(fills)
            res["fills"] = fills

        # --- trazos ---
        prog(0.80, "Convirtiendo lineas, arcos y polilineas")
        lines_by_key = defaultdict(list)
        n_src_segments = 0
        npaths = max(1, len(pd.paths))
        pstep = max(1, npaths // 20)
        for pi, path in enumerate(pd.paths):
            if pi % pstep == 0:
                prog(0.80 + 0.12 * pi / npaths, "Convirtiendo lineas %d / %d" % (pi, npaths))
            if path["kind"] == "fill":
                continue
            style = {
                "layer": self.layer_for(path["layer"], path["color"], path["width"], "line"),
                "color": path["color"],
                "width": path["width"],
                "dashes": path["dashes"] if o["linetypes"] else None,
            }
            for si, sp in enumerate(path["subpaths"]):
                segs = sp["segs"]
                n_src_segments += len(segs)
                runs, cur = [], []
                for ki, seg in enumerate(segs):
                    if (pi, si, ki) in consumed:
                        if cur:
                            runs.append(cur)
                        cur = []
                    else:
                        cur.append(seg)
                if cur:
                    runs.append(cur)
                closed = sp["closed"] and len(runs) == 1 and len(runs[0]) == len(segs)
                for run in runs:
                    self._emit_run(run, closed, style, res, lines_by_key)

        # --- lineas sueltas: fusion de colineales / duplicados ---
        prog(0.93, "Uniendo lineas colineales")
        for (lk, _), items in lines_by_key.items():
            style = items[0][1]
            if o["merge_collinear"]:
                for a, b in _merge_collinear([it[0] for it in items]):
                    res["lines"].append({"p1": a, "p2": b, **style})
            else:
                for (a, b), _ in items:
                    res["lines"].append({"p1": a, "p2": b, **style})

        # --- textos ---
        if o["texts"]:
            for t in pd.texts:
                t2 = dict(t)
                t2["layer"] = self.layer_for("", t["color"], 0, "text")
                res["texts"].append(t2)

        active = [h for h in all_hatches if not h["rejected"]]
        res["stats"] = {
            "paths_pdf": pd.n_raw_paths,
            "segmentos_pdf": n_src_segments,
            "hatches": len(active),
            "hatches_rechazados": len(all_hatches) - len(active),
            "lineas_hatch_absorbidas": len(consumed),
            "hatches_fusionados": n_hmerged,
            "solapes_corregidos": n_htrim + n_ftrim,
            "rellenos": len(res["fills"]),
            "rellenos_blancos_omitidos": n_white,
            "lineas": len(res["lines"]),
            "polilineas": len(res["polys"]),
            "circulos": len(res["circles"]),
            "arcos": len(res["arcs"]),
            "splines": len(res["splines"]),
            "textos": len(res["texts"]),
        }
        self._apply_layer_edits(res)
        prog(1.0, "Listo")
        res["k"] = model_factor(o)
        res["page"] = {"width": pd.width, "height": pd.height}
        res["opts"] = o
        return res

    def _apply_layer_edits(self, res):
        ren = {k: sanitize_layer(v) for k, v in (self.o.get("layer_rename") or {}).items() if v}
        excl = set(self.o.get("layer_exclude") or [])
        info = {}
        for kind in ("hatches", "fills", "lines", "polys", "circles", "arcs", "splines", "texts"):
            kept = []
            for e in res[kind]:
                src = e["layer"]
                li = info.setdefault(src, {"name": src, "count": 0, "color": e.get("color")})
                if not (kind == "hatches" and e.get("rejected")):
                    li["count"] += 1
                if src in excl:
                    continue
                e["src_layer"] = src
                e["layer"] = ren.get(src, src)
                kept.append(e)
            res[kind] = kept
        for name, li in info.items():
            li["excluded"] = name in excl
            li["rename"] = ren.get(name, "")
        res["layers"] = sorted(info.values(), key=lambda x: x["name"].lower())

    def _convert_curves(self, run):
        if not self.o["curves_to_arcs"]:
            return list(run)
        out = []
        for s in run:
            if s[0] == "C":
                if geom.is_straight_bezier(s[1], s[2], s[3], s[4], 0.05):
                    if geom.dist(s[1], s[4]) > 1e-6:
                        out.append(("L", s[1], s[4]))
                    continue
                arc = geom.bezier_to_arc(s[1], s[2], s[3], s[4])
                if arc:
                    c, r, ccw = arc
                    out.append(("A", s[1], s[4], c, r, ccw))
                    continue
            out.append(s)
        return out

    def _emit_run(self, run, closed, style, res, lines_by_key):
        run = self._convert_curves(run)
        if not run:
            return
        # circulo completo
        if closed and all(s[0] == "A" for s in run):
            c0, r0 = run[0][3], run[0][4]
            if all(geom.dist(s[3], c0) < max(0.05, r0 * 0.01) and abs(s[4] - r0) < max(0.05, r0 * 0.01)
                   for s in run):
                total = sum(geom.arc_sweep(s[1], s[2], s[3], s[5]) for s in run)
                if abs(total - 2 * math.pi) < 0.05:
                    res["circles"].append({"c": c0, "r": r0, **style})
                    return
        if self.o["join_polylines"] and len(run) > 1:
            chunk = []
            for s in run:
                if s[0] == "C":
                    if chunk:
                        res["polys"].append({"segs": chunk, "closed": False, **style})
                        chunk = []
                    res["splines"].append({"ctrl": [s[1], s[2], s[3], s[4]], **style})
                else:
                    chunk.append(s)
            if chunk:
                is_closed = closed and len(chunk) == len(run)
                if len(chunk) == 1 and not is_closed:
                    self._emit_single(chunk[0], style, res, lines_by_key)
                else:
                    res["polys"].append({"segs": chunk, "closed": is_closed, **style})
            return
        for s in run:
            self._emit_single(s, style, res, lines_by_key)

    def _emit_single(self, s, style, res, lines_by_key):
        if s[0] == "L":
            key = (style["layer"], style["color"], round(style["width"], 3), str(style["dashes"]))
            lines_by_key[(key, 0)].append(((s[1], s[2]), style))
        elif s[0] == "A":
            res["arcs"].append({"p1": s[1], "p2": s[2], "c": s[3], "r": s[4], "ccw": s[5], **style})
        elif s[0] == "C":
            res["splines"].append({"ctrl": [s[1], s[2], s[3], s[4]], **style})


def snap_hatch_boundaries(pd, hatches, consumed, progress=None):
    """El contorno deducido de las lineas queda hasta un espaciado por dentro del
    contorno real. Si las lineas existentes (muros, contornos) cierran una region
    que contiene al hatch y es solo un poco mayor, se usa esa region.

    Rendimiento: todas las regiones cerradas del plano se calculan UNA vez
    (nodado + polygonize global) y luego se cruzan con los hatches en bloque."""
    import numpy as np
    import shapely

    prog = progress or (lambda f, m=None: None)
    act = [h for h in hatches if not h.get("rejected")]
    if not act:
        return
    prog(0.0, "Buscando recintos cerrados")
    straight, curved = [], []
    for pi, path in enumerate(pd.paths):
        if path["kind"] == "fill":
            continue
        for si, sp in enumerate(path["subpaths"]):
            for ki, seg in enumerate(sp["segs"]):
                if (pi, si, ki) in consumed:
                    continue
                if seg[0] == "L":
                    straight.append((seg[1], seg[2]))
                else:
                    curved.append(geom.flatten_segment(seg))
    parts = []
    if straight:
        arr = np.array(straight, dtype=float)
        keep = np.hypot(arr[:, 1, 0] - arr[:, 0, 0], arr[:, 1, 1] - arr[:, 0, 1]) > 1e-3
        parts.append(shapely.linestrings(arr[keep]))
    if curved:
        parts.append(np.array([shapely.LineString(c) for c in curved if len(c) > 1], dtype=object))
    if not parts:
        return
    lines = np.concatenate(parts)
    if len(lines) == 0:
        return
    try:
        noded = shapely.union_all(lines)
        prog(0.35, "Calculando recintos")
        faces = shapely.get_parts(shapely.polygonize(shapely.get_parts(noded)))
    except Exception:
        return
    if len(faces) == 0:
        return
    faces = np.asarray(faces, dtype=object)
    fa = shapely.area(faces)
    prog(0.6, "Cruzando %d hatches con %d recintos" % (len(act), len(faces)))

    polys = np.array([h["polygon"] for h in act], dtype=object)
    pa = shapely.area(polys)
    sp_ = np.array([max(f["spacing"] for f in h["families"]) for h in act])
    max_area = pa + shapely.length(polys) * sp_ * 0.75

    tree = shapely.STRtree(faces)
    hi, fi = tree.query(polys)  # solape de cajas
    # una cara solo sirve si es candidata (<= max_area) o si puede quedar cubierta
    # al 60% por el hatch (area <= area_hatch / 0.6): se descartan antes de intersecar
    lim = np.maximum(max_area, pa / 0.6)
    m = fa[fi] <= lim[hi]
    hi, fi = hi[m], fi[m]
    # 1) contencion (geometria preparada, barata): si el recinto contiene al hatch,
    #    la interseccion es el propio hatch y no hace falta calcularla
    shapely.prepare(faces)
    # (se encoge un poco: el contorno deducido lleva un margen de ~2% del espaciado)
    shrunk = shapely.buffer(polys, -0.05 * sp_, join_style="mitre")
    shrunk = np.where(shapely.is_empty(shrunk), polys, shrunk)
    cont = shapely.contains(faces[fi], shrunk[hi])
    inter = np.where(cont, pa[hi], 0.0)
    ok_c = cont & (fa[fi] >= pa[hi] * 0.98) & (fa[fi] <= max_area[hi])
    resolved = np.zeros(len(act), dtype=bool)
    resolved[hi[ok_c]] = True
    # 2) interseccion exacta solo para hatches sin recinto contenedor valido
    need = ~cont & ~resolved[hi]
    if need.any():
        inter[need] = shapely.area(shapely.intersection(faces[fi[need]], polys[hi[need]]))
    m = inter > 0
    hi, fi, inter = hi[m], fi[m], inter[m]

    order = np.argsort(hi, kind="stable")
    hi, fi, inter = hi[order], fi[order], inter[order]
    starts = np.searchsorted(hi, np.arange(len(act)))
    ends = np.searchsorted(hi, np.arange(len(act)), side="right")
    n = len(act)
    for k in range(n):
        if k % 50 == 0:
            prog(0.6 + 0.4 * k / n, "Ajustando contornos %d / %d" % (k, n))
        a0, a1 = starts[k], ends[k]
        if a0 == a1:
            continue
        f_k, i_k = fi[a0:a1], inter[a0:a1]
        fa_k = fa[f_k]
        ok = (fa_k >= pa[k] * 0.98) & (fa_k <= max_area[k]) & (i_k >= 0.97 * pa[k])
        best = None
        if ok.any():
            c = np.flatnonzero(ok)
            best = faces[f_k[c[np.argmin(fa_k[c])]]]
        else:
            # hatch que ocupa varias caras (p.ej. con islas): union de caras cubiertas
            cov = i_k >= 0.6 * fa_k
            if cov.any():
                u = shapely.union_all(faces[f_k[cov]])
                if (u.area >= pa[k] * 0.98 and u.area <= max_area[k]
                        and u.intersection(polys[k]).area >= 0.97 * pa[k]):
                    best = u
        if best is not None:
            act[k]["polygon"] = best
            act[k]["snapped"] = True


def _merge_collinear(segs, ang_q=0.05, off_q=0.02):
    """Fusiona segmentos colineales que se tocan o solapan (y elimina duplicados)."""
    groups = defaultdict(list)
    for a, b in segs:
        dx, dy = b[0] - a[0], b[1] - a[1]
        L = math.hypot(dx, dy)
        if L < 1e-6:
            continue
        ang = math.atan2(dy, dx) % math.pi
        if ang > math.pi - 1e-4:
            ang = 0.0
        ux, uy = math.cos(ang), math.sin(ang)
        off = -a[0] * uy + a[1] * ux
        t1, t2 = a[0] * ux + a[1] * uy, b[0] * ux + b[1] * uy
        kq = (round(math.degrees(ang) / ang_q), round(off / off_q))
        groups[kq].append((min(t1, t2), max(t1, t2), ang, off, a, b))
    out = []
    for items in groups.values():
        if len(items) == 1:
            it = items[0]
            out.append((it[4], it[5]))
            continue
        items.sort()
        ang, off = items[0][2], items[0][3]
        ux, uy = math.cos(ang), math.sin(ang)
        cur0, cur1 = items[0][0], items[0][1]
        spans = []
        for t0, t1, *_ in items[1:]:
            if t0 <= cur1 + 1e-3:
                cur1 = max(cur1, t1)
            else:
                spans.append((cur0, cur1))
                cur0, cur1 = t0, t1
        spans.append((cur0, cur1))
        for t0, t1 in spans:
            p = (t0 * ux - off * uy, t0 * uy + off * ux)
            q = (t1 * ux - off * uy, t1 * uy + off * ux)
            out.append((p, q))
    return out


def convert(pd, opts, progress=None):
    return Converter(pd, opts).run(progress)
