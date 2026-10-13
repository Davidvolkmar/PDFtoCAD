"""Reconstruccion de hatches a partir de lineas explotadas.

Idea: los hatches de patron (ANSI31, NET, USER...) llegan al PDF como familias de
segmentos paralelos y equiespaciados. Se agrupan por estilo (capa/color/grosor)
y angulo, se ordenan por su desplazamiento perpendicular y se conectan los
segmentos vecinos que se solapan a lo largo de la direccion de la linea y cuyo
espaciado es coherente. Cada grupo conectado es un hatch: su contorno se obtiene
uniendo los trapecios que forman cada par de lineas vecinas.
"""
import bisect
import math
import statistics
from collections import defaultdict

import numpy as np
import shapely

from .compat import coverage_union
from shapely.geometry import Polygon
from shapely.ops import unary_union

PT_PER_MM = 72 / 25.4


class _UF:
    def __init__(self, n):
        self.p = list(range(n))

    def find(self, a):
        p = self.p
        while p[a] != a:
            p[a] = p[p[a]]
            a = p[a]
        return a

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[rb] = ra


def detect_hatches(pd, opts, progress=None):
    prog = progress or (lambda f, m="": None)
    min_lines = int(opts.get("hatch_min_lines", 6))
    max_sp = float(opts.get("hatch_max_spacing_mm", 12)) * PT_PER_MM
    ang_tol = float(opts.get("hatch_angle_tol", 0.5))
    sp_tol = float(opts.get("hatch_spacing_tol", 0.15))
    gap_factor = float(opts.get("hatch_gap_factor", 0.5))
    detect_dashes = bool(opts.get("hatch_dashes", True))
    min_len = 0.3
    isolated_only = bool(opts.get("hatch_isolated_only", True))

    by_key = defaultdict(list)
    for pi, path in enumerate(pd.paths):
        if path["kind"] == "fill" or path["dashes"]:
            continue
        key = (path["layer"], path["color"], round(path["width"], 2))
        for si, sp in enumerate(path["subpaths"]):
            # las lineas de un hatch son tramos sueltos; los tramos encadenados
            # (polilineas, contornos, rectangulos) son geometria del dibujo
            if isolated_only and len(sp["segs"]) > 1:
                continue
            for ki, seg in enumerate(sp["segs"]):
                if seg[0] != "L":
                    continue
                (x1, y1), (x2, y2) = seg[1], seg[2]
                L = math.hypot(x2 - x1, y2 - y1)
                if L < min_len:
                    continue
                ang = math.degrees(math.atan2(y2 - y1, x2 - x1)) % 180.0
                if ang > 180.0 - ang_tol:
                    ang -= 180.0
                by_key[key].append((ang, x1, y1, x2, y2, (pi, si, ki)))

    hatches = []
    total = max(1, sum(len(v) for v in by_key.values()))
    done = 0
    prog(0.0, "Buscando hatches en %d lineas" % total)
    for key, segs in by_key.items():
        done += len(segs)
        if len(segs) < min_lines:
            continue
        segs.sort(key=lambda s: s[0])
        groups, cur = [], [segs[0]]
        for s in segs[1:]:
            if s[0] - cur[-1][0] > ang_tol:
                groups.append(cur)
                cur = [s]
            else:
                cur.append(s)
        groups.append(cur)
        key_hatches = []
        gdone = done - len(segs)
        for g in groups:
            g0 = gdone
            gdone += len(g)

            def tick(f, g0=g0, n=len(g)):
                frac = (g0 + f * n) / total
                prog(min(0.95, frac), "Reconstruyendo hatches (%d%% de las lineas)" % int(100 * frac))

            tick(0)
            if len(g) >= min_lines:
                key_hatches.extend(_clusters_for_group(g, key, min_lines, max_sp, sp_tol,
                                                       gap_factor, detect_dashes, tick))
        hatches.extend(_merge_crosshatch(key_hatches))

    # orden determinista: por posicion
    hatches.sort(key=lambda h: (round(-h["polygon"].bounds[3], 1), round(h["polygon"].bounds[0], 1)))
    for i, h in enumerate(hatches):
        h["id"] = i
    return hatches


def _clusters_for_group(g, key, min_lines, max_sp, sp_tol, gap_factor, detect_dashes, tick=None):
    tick = tick or (lambda f: None)
    theta = math.radians(statistics.median(s[0] for s in g))
    ux, uy = math.cos(theta), math.sin(theta)
    nx, ny = -uy, ux

    items = []
    for ang, x1, y1, x2, y2, ref in g:
        d = ((x1 + x2) * 0.5) * nx + ((y1 + y2) * 0.5) * ny
        t1, t2 = x1 * ux + y1 * uy, x2 * ux + y2 * uy
        if t1 > t2:
            t1, t2 = t2, t1
        items.append((d, t1, t2, ref))
    items.sort(key=lambda it: it[0])

    row_eps = 0.12
    rows = []  # [d, [pieces]]
    for it in items:
        if rows and it[0] - rows[-1][0] <= row_eps:
            r = rows[-1]
            r[1].append(it)
        else:
            rows.append([it[0], [it]])
    for r in rows:
        r[0] = sum(p[0] for p in r[1]) / len(r[1])
    if len(rows) < min_lines:
        return []

    diffs = [rows[i + 1][0] - rows[i][0] for i in range(len(rows) - 1)]
    diffs = [x for x in diffs if row_eps < x <= max_sp]
    if not diffs:
        return []
    s_est = statistics.median(diffs)

    # ---- runs: piezas colineales unidas si el hueco es pequeno ----
    runs = []          # (row_idx, t0, t1, pieces)
    row_runs = []      # por fila: lista de indices de runs ordenados por t0
    row_t0 = []
    for ri, (d, pieces) in enumerate(rows):
        pieces.sort(key=lambda p: p[1])
        idxs = []
        cur = None
        for p in pieces:
            gap = p[1] - cur[2] if cur is not None else 0
            # hueco pequeno, o hueco entre guiones de un patron discontinuo
            dash_gap = min(1.2 * s_est, 0.8 * max(p[2] - p[1], cur[3][-1][2] - cur[3][-1][1])) if cur else 0
            if cur is not None and gap <= max(gap_factor * s_est, dash_gap):
                cur[2] = max(cur[2], p[2])
                cur[3].append(p)
            else:
                cur = [ri, p[1], p[2], [p]]
                runs.append(cur)
                idxs.append(len(runs) - 1)
        row_runs.append(idxs)
        row_t0.append([runs[k][1] for k in idxs])

    # ---- vecinos: hasta 3 filas superiores con solape ----
    # (mirar mas alla de la primera evita que una linea intrusa corte el hatch)
    edges = []        # (a, b, dd, primera_fila)
    nruns = len(runs)
    nrows_all = len(rows)
    rstep = max(1, nrows_all // 20)
    for ri in range(nrows_all):
        if ri % rstep == 0:
            tick(0.6 * ri / nrows_all)  # busqueda de vecinos: ~60% del grupo
        d0 = rows[ri][0]
        for a in row_runs[ri]:
            ra = runs[a]
            hits = 0
            for rj in range(ri + 1, len(rows)):
                dd = rows[rj][0] - d0
                if dd > max_sp:
                    break
                cand = row_runs[rj]
                hi = bisect.bisect_left(row_t0[rj], ra[2])
                found = []
                for k in range(hi - 1, -1, -1):
                    rb = runs[cand[k]]
                    if rb[2] > ra[1]:
                        found.append(cand[k])
                    else:
                        break  # runs de una fila ordenados y sin solape
                if found:
                    for b in found:
                        edges.append((a, b, dd, hits == 0))
                    hits += 1
                    if hits >= 3:
                        break

    # ---- espaciados dominantes del grupo ----
    first = sorted(e[2] for e in edges if e[3])
    modes = []
    i = 0
    while i < len(first):
        j = i
        while j + 1 < len(first) and first[j + 1] - first[i] <= first[i] * sp_tol:
            j += 1
        cnt = j - i + 1
        if cnt >= max(min_lines - 1, 0.03 * len(first)):
            modes.append(statistics.median(first[i:j + 1]))
        i = j + 1

    if not modes or not edges:
        return []
    dd_arr = np.fromiter((e[2] for e in edges), dtype=float, count=len(edges))
    hit = np.zeros(len(edges), dtype=bool)
    for m in modes:
        hit |= np.abs(dd_arr - m) <= m * sp_tol
    good = [edges[i] for i in np.flatnonzero(hit)]
    local = [math.inf] * nruns
    for a, b, dd, _ in good:
        if dd < local[a]:
            local[a] = dd
        if dd < local[b]:
            local[b] = dd

    uf = _UF(nruns)
    accepted = []
    for a, b, dd, nearest in good:
        if dd <= local[a] * (1 + sp_tol) and dd <= local[b] * (1 + sp_tol):
            uf.union(a, b)
            accepted.append((a, b, dd, nearest))

    clusters = defaultdict(list)
    for e in accepted:
        clusters[uf.find(e[0])].append(e)

    out = []
    ncl = max(1, len(clusters))
    cstep = max(1, ncl // 20)
    for ci, (root, cedges) in enumerate(clusters.items()):
        if ci % cstep == 0:
            tick(0.6 + 0.4 * ci / ncl)
        members = set()
        for a, b, _, _ in cedges:
            members.add(a)
            members.add(b)
        nrows = len({runs[m][0] for m in members})
        if nrows < min_lines:
            continue
        ds = [e[2] for e in cedges]
        s = float(np.median(ds))
        if s <= 0:
            continue
        regular = sum(1 for x in ds if abs(x - s) <= s * sp_tol) / len(ds)
        if regular < 0.8:
            continue

        # trapecios entre lineas vecinas (vectorizado); basta con el vecino inmediato
        qedges = [e for e in cedges if e[3]] or cedges
        qa = np.empty((len(qedges), 4, 2))
        for i, (a, b, _, _) in enumerate(qedges):
            ra, rb = runs[a], runs[b]
            da, db = rows[ra[0]][0], rows[rb[0]][0]
            qa[i] = ((ra[1], da), (ra[2], da), (rb[2], db), (rb[1], db))
        t, d = qa[..., 0], qa[..., 1]
        xy = np.stack((t * ux + d * nx, t * uy + d * ny), axis=-1)
        quads = shapely.polygons(xy)
        quads = quads[shapely.area(quads) > 1e-6]
        if len(quads) == 0:
            continue
        merged = _union_quads(quads)
        poly = merged.buffer(s * 0.02, join_style=2).simplify(s * 0.01)
        if poly.is_empty:
            continue

        refs = []
        for m in members:
            refs.extend(p[3] for p in runs[m][3])
        families = _families(members, runs, rows, s, theta, detect_dashes)

        # "dudoso": pocas lineas de igual longitud y separacion grande -> escalera,
        # celosia, persiana... (el usuario decide)
        lens = [runs[m][2] - runs[m][1] for m in members]
        mean_len = sum(lens) / len(lens)
        equal = mean_len > 0 and float(np.std(lens)) / mean_len < 0.03
        suspect = (equal and nrows <= 25) or (nrows <= 8 and s > 4 * PT_PER_MM)

        out.append({
            "key": key,
            "families": families,
            "polygon": poly,
            "refs": refs,
            "n_lines": len(refs),
            "suspect": suspect,
        })
    return out


def _union_quads(quads):
    """Los trapecios entre filas vecinas comparten bordes exactos: la union de
    cobertura es ~10x mas rapida. Si hay solapes (validez/area no cuadran) se usa
    la union general."""
    total = float(shapely.area(quads).sum())
    u = coverage_union(quads)
    if u is not None and u.is_valid and abs(u.area - total) <= 1e-6 * max(total, 1.0) + 1e-6:
        return u
    try:
        return shapely.union_all(quads)
    except Exception:
        return shapely.union_all(shapely.make_valid(quads))


def _families(members, runs, rows, s, theta, detect_dashes):
    """Familias de lineas del patron. Soporta filas alternas continua/discontinua
    (ANSI33, etc.) generando dos familias con periodo doble."""
    ux, uy = math.cos(theta), math.sin(theta)
    nx, ny = -uy, ux
    ang = math.degrees(theta) % 180.0
    by_row = defaultdict(list)
    for m in members:
        by_row[runs[m][0]].append(runs[m])

    def base_of(run):
        pc = run[3]
        # en discontinuos, el primer guion puede estar recortado por el contorno
        bt, bd = (pc[1][1] if len(pc) >= 3 else pc[0][1]), rows[run[0]][0]
        return (bt * ux + bd * nx, bt * uy + bd * ny)

    def dash_stats(rs):
        lens, gaps = [], []
        for r in rs:
            pc = r[3]
            for i in range(1, len(pc)):
                gaps.append(pc[i][1] - pc[i - 1][2])
            for p in pc[1:-1]:
                lens.append(p[2] - p[1])
        if len(gaps) < 4 or not lens:
            return []
        gm, lm = float(np.median(gaps)), float(np.median(lens))
        if gm <= 0.05 or lm <= 0.05 or float(np.std(gaps)) / gm > 0.35:
            return []
        return [lm, -gm]

    order = sorted(by_row)
    rtype = {}
    for ri in order:
        rs = by_row[ri]
        rtype[ri] = sum(1 for r in rs if len(r[3]) >= 3) >= 0.5 * len(rs)
    first_run = by_row[order[0]][0]
    if not detect_dashes:
        return [{"angle": ang, "spacing": s, "base": base_of(first_run), "dashes": []}]
    nd = sum(1 for ri in order if rtype[ri])
    frac = nd / len(order)
    if 0.3 <= frac <= 0.7 and len(order) >= 4:
        alt = sum(1 for a, b in zip(order, order[1:]) if rtype[a] != rtype[b]) / (len(order) - 1)
        if alt >= 0.75:
            cont_rows = [ri for ri in order if not rtype[ri]]
            dash_rows = [ri for ri in order if rtype[ri]]
            dash = dash_stats([r for ri in dash_rows for r in by_row[ri]])
            if dash:
                return [
                    {"angle": ang, "spacing": 2 * s, "base": base_of(by_row[cont_rows[0]][0]), "dashes": []},
                    {"angle": ang, "spacing": 2 * s, "base": base_of(by_row[dash_rows[0]][0]), "dashes": dash},
                ]
    if frac > 0.7:
        dash = dash_stats([r for ri in order for r in by_row[ri]])
        if dash:
            return [{"angle": ang, "spacing": s, "base": base_of(first_run), "dashes": dash}]
    return [{"angle": ang, "spacing": s, "base": base_of(first_run), "dashes": []}]


def _merge_crosshatch(hs):
    """Une familias de distinto angulo que cubren la misma region (p.ej. NET, ANSI37)."""
    if len(hs) < 2:
        return hs
    tree = shapely.STRtree([h["polygon"] for h in hs])
    merged = [False] * len(hs)
    out = []
    for i, a in enumerate(hs):
        if merged[i]:
            continue
        cur = a
        for j in sorted(int(x) for x in tree.query(a["polygon"])):
            if j <= i or merged[j]:
                continue
            b = hs[j]
            if any(abs(f["angle"] - fb["angle"]) < 1 for f in cur["families"] for fb in b["families"]):
                continue
            pa, pb = cur["polygon"], b["polygon"]
            if not pa.intersects(pb):
                continue
            inter = pa.intersection(pb).area
            if inter / max(1e-9, min(pa.area, pb.area)) > 0.85:
                cur = {
                    "key": cur["key"],
                    "families": cur["families"] + b["families"],
                    "polygon": pa.union(pb),
                    "refs": cur["refs"] + b["refs"],
                    "n_lines": cur["n_lines"] + b["n_lines"],
                    "suspect": cur["suspect"] and b["suspect"],
                }
                merged[j] = True
        out.append(cur)
    return out


_LIB = None
PRIORITY = ["ANSI31", "ANSI37", "ANSI32", "ANSI33", "ANSI34", "ANSI35", "ANSI36", "ANSI38",
            "NET", "LINE", "DASH", "NET3", "SQUARE", "BRICK", "AR-BRSTD", "AR-B816", "AR-PARQ1"]


def _library():
    """Patrones estandar de AutoCAD (acadiso.pat) que solo tienen lineas."""
    global _LIB
    if _LIB is not None:
        return _LIB
    from ezdxf.tools import pattern as ezpat
    lib = []
    pats = ezpat.load()
    names = [n for n in PRIORITY if n in pats] + sorted(n for n in pats if n not in PRIORITY)
    for name in names:
        lines = []
        ok = True
        for ang, base, off, dashes in pats[name]:
            th = math.radians(ang)
            perp = abs(-math.sin(th) * off[0] + math.cos(th) * off[1])
            if perp < 1e-6 or len(dashes) > 6:
                ok = False
                break
            lines.append((ang % 180.0, perp, [abs(d) for d in dashes]))
        if ok and 1 <= len(lines) <= 4:
            lib.append((name, lines))
    _LIB = lib
    return lib


def _dash_eq(a, b, c, tol=0.06):
    if bool(a) != bool(b):
        return False
    if not a:
        return True
    if len(a) != len(b):
        return False
    return all(abs(x - y * c) <= tol * max(y * c, 1e-9) + 0.05 for x, y in zip(a, b))


def _ang_eq(a, b, tol=1.0):
    d = abs(a - b) % 180.0
    return min(d, 180.0 - d) <= tol


def match_library(fams):
    """Busca un patron estandar equivalente. Devuelve (nombre, escala, angulo) o None."""
    fd = [(f["angle"] % 180.0, f["spacing"], [abs(v) for v in f["dashes"]]) for f in fams]
    for name, lines in _library():
        if len(lines) != len(fd):
            continue
        a0, s0, d0 = fd[0]
        for la, lp, ld in lines:
            c = s0 / lp
            if not _dash_eq(d0, ld, c):
                continue
            r = (a0 - la) % 180.0
            used = {id(lines[lines.index((la, lp, ld))])}
            free = list(lines)
            free.remove((la, lp, ld))
            ok = True
            for fa, fs, fdsh in fd[1:]:
                hit = None
                for cand in free:
                    ca, cp, cdsh = cand
                    if (_ang_eq(ca + r, fa) and abs(cp * c - fs) <= 0.04 * fs
                            and _dash_eq(fdsh, cdsh, c)):
                        hit = cand
                        break
                if hit is None:
                    ok = False
                    break
                free.remove(hit)
            if ok:
                return name, c, r
    return None


def classify_pattern(h, style="ansi"):
    """Devuelve dict(name, ptype, scale, angle) en unidades de papel (la escala del
    patron se multiplica luego por el factor del modelo).
    ptype: 'predefined' | 'user' | 'custom'"""
    fam = h["families"]
    cont = all(not f["dashes"] for f in fam)
    if style == "user" and cont:
        if len(fam) == 1:
            return {"name": "_USER", "ptype": "user", "scale": fam[0]["spacing"], "angle": fam[0]["angle"] % 180}
        if len(fam) == 2:
            a, b = fam
            if (_ang_eq(a["angle"] + 90, b["angle"])
                    and abs(a["spacing"] - b["spacing"]) <= 0.04 * max(a["spacing"], b["spacing"])):
                return {"name": "_USER", "ptype": "user", "scale": a["spacing"],
                        "angle": min(a["angle"], b["angle"]) % 180}
    m = match_library(fam)
    if m:
        name, c, r = m
        if name in ("ANSI37", "NET", "SQUARE"):  # simetricos a 90 grados
            r %= 90.0
        return {"name": name, "ptype": "predefined", "scale": c, "angle": r}
    return {"name": "PDF_H%d" % h.get("id", 0), "ptype": "custom", "scale": 1.0, "angle": 0.0}
