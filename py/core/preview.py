"""Serializa el resultado para la previsualizacion web (coordenadas de papel, pt, Y arriba)."""
from collections import defaultdict

from . import geom


def _r(v):
    return round(v, 2)


def _flat(pts):
    out = []
    for x, y in pts:
        out.append(_r(x))
        out.append(_r(y))
    return out


def _hex(c):
    if c is None:
        return "#000000"
    return "#%02x%02x%02x" % tuple(c)


def _rings(polygon):
    rings = []
    for p in getattr(polygon, "geoms", [polygon]):
        if p.geom_type != "Polygon":
            continue
        rings.append(_flat(list(p.exterior.coords)))
        for i in p.interiors:
            rings.append(_flat(list(i.coords)))
    return rings


def to_preview(res):
    k = res["k"]
    groups = defaultdict(list)

    def add(ent, pts):
        key = (ent["layer"], _hex(ent.get("color")), round(ent.get("width") or 0, 2),
               bool(ent.get("dashes")))
        groups[key].append(_flat(pts))

    for ln in res["lines"]:
        add(ln, [ln["p1"], ln["p2"]])
    for p in res["polys"]:
        pts = geom.flatten_subpath(p["segs"])
        if p["closed"]:
            pts.append(pts[0])
        add(p, pts)
    for c in res["circles"]:
        cx, cy = c["c"]
        pts = geom.flatten_arc((cx + c["r"], cy), (cx + c["r"], cy), c["c"], c["r"], True)
        add(c, pts)
    for a in res["arcs"]:
        add(a, geom.flatten_arc(a["p1"], a["p2"], a["c"], a["r"], a["ccw"]))
    for s in res["splines"]:
        add(s, geom.flatten_bezier(*s["ctrl"]))

    strokes = [{"layer": key[0], "color": key[1], "width": key[2], "dashed": key[3], "paths": paths}
               for key, paths in groups.items()]

    hatches = []
    for h in res["hatches"]:
        hatches.append({
            "id": h["id"], "layer": h["layer"], "color": _hex(h["color"]),
            "pattern": h["pattern"], "ptype": h["ptype"],
            "pscale": round(h["pat"]["scale"] * (k if h["ptype"] != "custom" else 1), 4),
            "pangle": round(h["pat"]["angle"], 2), "rejected": h.get("rejected", False),
            "suspect": h.get("suspect", False), "snapped": h.get("snapped", False),
            "n_lines": h["n_lines"],
            "area": round(h["polygon"].area * k * k, 4),
            "rings": _rings(h["polygon"]),
            "families": [{"angle": round(f["angle"], 3), "spacing": round(f["spacing"], 4),
                          "base": [_r(f["base"][0]), _r(f["base"][1])],
                          "dashes": [round(v, 3) for v in f["dashes"]],
                          "spacing_model": round(f["spacing"] * k, 4)} for f in h["families"]],
        })
    fills = [{"layer": f["layer"], "color": _hex(f["color"]), "rings": _rings(f["polygon"])}
             for f in res["fills"]]
    texts = [{"x": _r(t["x"]), "y": _r(t["y"]), "size": round(t["size"], 2), "angle": t["angle"],
              "text": t["text"], "color": _hex(t["color"]), "layer": t["layer"]} for t in res["texts"]]
    layers = [{"name": l["name"], "count": l["count"], "color": _hex(l["color"]),
               "excluded": l["excluded"], "rename": l["rename"]} for l in res.get("layers", [])]
    return {"page": res["page"], "k": k, "stats": res["stats"], "strokes": strokes,
            "hatches": hatches, "fills": fills, "texts": texts, "layers": layers}
