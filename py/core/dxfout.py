"""Escritura del resultado a DXF."""
import math
import ezdxf
from ezdxf import colors as ezcolors
from ezdxf import const, units

from . import geom

PT_TO_MM = 25.4 / 72
LINEWEIGHTS = [0, 5, 9, 13, 15, 18, 20, 25, 30, 35, 40, 50, 53, 60, 70, 80, 90, 100,
               106, 120, 140, 158, 200, 211]


def _lineweight(width_pt):
    mm100 = width_pt * PT_TO_MM * 100
    if mm100 <= 0:
        return 0
    return min(LINEWEIGHTS, key=lambda v: abs(v - mm100))


class DxfWriter:
    def __init__(self, res):
        self.res = res
        self.o = res["opts"]
        self.k = res["k"]
        self.doc = ezdxf.new("R2018", setup=True)
        u = self.o.get("units", "mm")
        self.doc.units = {"mm": units.MM, "cm": units.CM, "m": units.M}.get(u, units.MM)
        self.doc.header["$INSUNITS"] = self.doc.units
        self.doc.header["$LTSCALE"] = 1.0
        self.doc.header["$PSLTSCALE"] = 0
        self.doc.header["$LWDISPLAY"] = 1
        self.msp = self.doc.modelspace()
        self.layers = {}
        self.ltypes = {}
        if "PDF" not in self.doc.styles:
            self.doc.styles.add("PDF", font="arial.ttf")

    # ---------------- helpers ----------------
    def X(self, p):
        return (p[0] * self.k, p[1] * self.k)

    def ensure_layer(self, name, color):
        if name in self.layers:
            return
        lay = self.doc.layers.add(name) if name not in self.doc.layers else self.doc.layers.get(name)
        if self.o.get("layer_mode") in ("color", "color_width") and color:
            if tuple(color) == (0, 0, 0):
                lay.color = 7
            else:
                lay.rgb = tuple(color)
        self.layers[name] = color

    def attribs(self, ent):
        layer = ent["layer"]
        color = ent.get("color")
        self.ensure_layer(layer, color)
        a = {"layer": layer}
        if self.o.get("layer_mode") not in ("color", "color_width") and color is not None:
            if tuple(color) == (0, 0, 0):
                a["color"] = 7
            else:
                a["true_color"] = ezcolors.rgb2int(tuple(color))
        if self.o.get("lineweights") and ent.get("width") is not None:
            a["lineweight"] = _lineweight(ent["width"])
        d = ent.get("dashes")
        if d:
            a["linetype"] = self.ensure_ltype(d)
        return a

    def ensure_ltype(self, dashes):
        key = tuple(round(v, 3) for v in dashes)
        if key in self.ltypes:
            return self.ltypes[key]
        name = "PDF_DASH_%d" % (len(self.ltypes) + 1)
        pat = []
        for i, v in enumerate(dashes):
            L = v * self.k
            pat.append(L if i % 2 == 0 else -L)
        total = sum(abs(v) for v in pat)
        self.doc.linetypes.add(name, pattern=[total] + pat,
                               description="PDF " + " ".join("%.2f" % v for v in dashes))
        self.ltypes[key] = name
        return name

    # ---------------- escritura ----------------
    def write(self, progress=None):
        prog = progress or (lambda f, m=None: None)
        r = self.res
        prog(0.0, "Escribiendo hatches y rellenos")
        for f in r["fills"]:
            self._fill(f)
        for h in r["hatches"]:
            if not h.get("rejected"):
                self._hatch(h)
        prog(0.35, "Escribiendo lineas y polilineas")
        for ln in r["lines"]:
            self.msp.add_line(self.X(ln["p1"]), self.X(ln["p2"]), dxfattribs=self.attribs(ln))
        for p in r["polys"]:
            self._poly(p)
        for c in r["circles"]:
            self.msp.add_circle(self.X(c["c"]), c["r"] * self.k, dxfattribs=self.attribs(c))
        for a in r["arcs"]:
            self._arc(a)
        for s in r["splines"]:
            pts = [self.X(p) for p in s["ctrl"]]
            self.msp.add_open_spline(pts, degree=3, knots=[0, 0, 0, 0, 1, 1, 1, 1],
                                     dxfattribs=self.attribs(s))
        prog(0.8, "Escribiendo textos")
        hf = float(self.o.get("text_height_factor", 0.72))
        for t in r["texts"]:
            a = {"layer": t["layer"], "style": "PDF", "rotation": t["angle"],
                 "height": max(1e-4, t["size"] * hf * self.k), "insert": self.X((t["x"], t["y"]))}
            self.ensure_layer(t["layer"], t.get("color"))
            col = t.get("color")
            if self.o.get("layer_mode") not in ("color",) and col is not None:
                if tuple(col) == (0, 0, 0):
                    a["color"] = 7
                else:
                    a["true_color"] = ezcolors.rgb2int(tuple(col))
            self.msp.add_text(t["text"], dxfattribs=a)
        return self.doc

    def _boundary(self, hatch, polygon, layer=None, color=None):
        """Contornos del hatch. Con 'hatch_boundary' cada anillo se crea tambien como
        LWPOLYLINE cerrada y el hatch queda ASOCIATIVO a ella (al editar la polilinea
        en CAD el hatch se actualiza). Las polilineas van a una capa _CONTORNO que no se
        imprime, para no duplicar los muros en el ploteo."""
        from .cleanup import clean_polygon
        polygon = clean_polygon(polygon) or polygon
        polys = [p for p in getattr(polygon, "geoms", [polygon]) if p.geom_type == "Polygon" and not p.is_empty]
        assoc = bool(self.o.get("hatch_boundary")) and layer is not None
        blayer = None
        if assoc:
            blayer = layer + "_CONTORNO"
            if blayer not in self.layers:
                self.ensure_layer(blayer, color)
                self.doc.layers.get(blayer).dxf.plot = 0
            hatch.dxf.associative = 1
        for p in polys:
            rings = [(p.exterior, const.BOUNDARY_PATH_EXTERNAL)] + [(r, const.BOUNDARY_PATH_DEFAULT) for r in p.interiors]
            for ring, flags in rings:
                pts = [self.X(c) for c in list(ring.coords)[:-1]]
                if len(pts) < 3:
                    continue
                path = hatch.paths.add_polyline_path(pts, is_closed=True, flags=flags)
                if assoc:
                    pl = self.msp.add_lwpolyline(pts, close=True, dxfattribs={"layer": blayer})
                    path.source_boundary_objects = [pl.dxf.handle]
                    pl.append_reactor_handle(hatch.dxf.handle)
        return polys

    def _fill(self, f):
        a = self.attribs({"layer": f["layer"], "color": f["color"]})
        h = self.msp.add_hatch(dxfattribs=a)
        col = f["color"]
        if tuple(col) == (0, 0, 0):
            h.set_solid_fill(color=7, style=0)
        else:
            h.set_solid_fill(color=7, style=0, rgb=tuple(col))
        self._boundary(h, f["polygon"], f["layer"], f["color"])

    def _hatch(self, hd):
        a = self.attribs({"layer": hd["layer"], "color": hd["color"]})
        h = self.msp.add_hatch(dxfattribs=a)
        k = self.k
        fams = hd["families"]
        lines = []
        for f in fams:
            th = math.radians(f["angle"])
            s = f["spacing"] * k
            off = (-math.sin(th) * s, math.cos(th) * s)
            dashes = [v * k for v in f["dashes"]] if f["dashes"] else []
            lines.append([f["angle"], self.X(f["base"]), off, dashes])
        pat = hd["pat"]
        name, ptype = pat["name"], pat["ptype"]
        col = a.get("color", 7)
        dummy = [[0, (0, 0), (0, 1), []]]
        if ptype == "predefined":
            h.set_pattern_fill(name, color=col, angle=pat["angle"], scale=pat["scale"] * k, style=0,
                               pattern_type=const.HATCH_TYPE_PREDEFINED, definition=dummy)
        elif ptype == "user":
            h.set_pattern_fill("_USER", color=col, angle=pat["angle"], scale=pat["scale"] * k,
                               double=1 if len(fams) == 2 else 0, style=0,
                               pattern_type=const.HATCH_TYPE_USER_DEFINED, definition=dummy)
        else:
            h.set_pattern_fill(name, color=col, angle=0, scale=1.0, style=0,
                               pattern_type=const.HATCH_TYPE_CUSTOM, definition=dummy)
        h.set_pattern_definition(lines)
        if "true_color" in a:
            h.dxf.true_color = a["true_color"]
        self._boundary(h, hd["polygon"], hd["layer"], hd["color"])

    def _poly(self, p):
        pts = []
        for s in p["segs"]:
            b = geom.bulge(s) if s[0] == "A" else 0.0
            x, y = self.X(s[1])
            pts.append((x, y, 0, 0, b))
        if not p["closed"]:
            x, y = self.X(geom.seg_end(p["segs"][-1]))
            pts.append((x, y, 0, 0, 0))
        self.msp.add_lwpolyline(pts, format="xyseb", close=p["closed"], dxfattribs=self.attribs(p))

    def _arc(self, a):
        c = self.X(a["c"])
        p1, p2 = self.X(a["p1"]), self.X(a["p2"])
        a1 = math.degrees(math.atan2(p1[1] - c[1], p1[0] - c[0]))
        a2 = math.degrees(math.atan2(p2[1] - c[1], p2[0] - c[0]))
        if not a["ccw"]:
            a1, a2 = a2, a1
        self.msp.add_arc(c, a["r"] * self.k, a1, a2, dxfattribs=self.attribs(a))


def export(res, out_path, fmt="dxf", progress=None):
    """Escribe el DXF (R2018). fmt se mantiene por compatibilidad: solo 'dxf'."""
    prog = progress or (lambda f, m=None: None)
    doc = DxfWriter(res).write(lambda f, m=None: prog(0.8 * f, m))
    prog(0.85, "Guardando archivo DXF")
    doc.saveas(out_path)
    prog(1.0, "Archivo listo")
    return out_path
