"""Analiza una pagina y propone ajustes de conversion."""
import re
from collections import Counter

from .pipeline import PT_TO_MM, convert

PAPER_SIZES = {  # mm (lado corto, lado largo)
    "A4": (210, 297), "A3": (297, 420), "A2": (420, 594), "A1": (594, 841), "A0": (841, 1189),
    "Carta": (216, 279), "Oficio": (216, 356), "Tabloide": (279, 432),
    "ARCH D": (610, 914), "ARCH E": (914, 1219),
}

SCALE_RE = re.compile(r"(?<![\d.,])1\s*[:/]\s*(\d{1,5})(?![\d.,])")
SCALE_KW = re.compile(r"\b(esc(ala)?|scale|maßstab)\b", re.I)


def paper_name(w_pt, h_pt):
    a, b = sorted((w_pt * PT_TO_MM, h_pt * PT_TO_MM))
    for name, (s, l) in PAPER_SIZES.items():
        if abs(a - s) < 6 and abs(b - l) < 6:
            return name
    return "%.0f x %.0f mm" % (b, a)


def detect_scale(texts):
    votes = Counter()
    for t in texts:
        s = t["text"]
        for m in SCALE_RE.finditer(s):
            n = int(m.group(1))
            if n < 1 or n > 20000:
                continue
            w = 3 if SCALE_KW.search(s) else 1
            votes[n] += w
    if not votes:
        return None, []
    ranked = [n for n, _ in votes.most_common()]
    return ranked[0], ranked


def analyze(pd, progress=None):
    prog = progress or (lambda f, m=None: None)
    prog(0.0, "Analizando el plano")
    page_area = pd.width * pd.height
    n_seg = sum(len(sp["segs"]) for p in pd.paths for sp in p["subpaths"])
    n_curves = sum(1 for p in pd.paths for sp in p["subpaths"] for s in sp["segs"] if s[0] == "C")
    n_fill = sum(1 for p in pd.paths if p["kind"] in ("fill", "both"))
    n_white = sum(1 for p in pd.paths if p["fill"] is not None and min(p["fill"]) >= 250)
    n_dash = sum(1 for p in pd.paths if p["dashes"])
    tiny = sum(1 for p in pd.paths for sp in p["subpaths"] for s in sp["segs"]
               if s[0] == "L" and ((s[1][0] - s[2][0]) ** 2 + (s[1][1] - s[2][1]) ** 2) < 1.5 ** 2)
    img_area = sum(max(0, (x1 - x0)) * max(0, (y1 - y0)) for x0, y0, x1, y1 in pd.images)
    layers_used = sorted({p["layer"] for p in pd.paths if p["layer"]})

    recs = []
    sug = {}

    def rec(level, title, detail, apply=None, rid=None):
        recs.append({"id": rid or title, "level": level, "title": title, "detail": detail,
                     "apply": apply or {}})
        if apply:
            sug.update(apply)

    # --- escaneado ---
    if img_area > 0.5 * page_area and n_seg < 300:
        rec("error", "El PDF parece escaneado (imagen)",
            "La pagina es practicamente una imagen y casi no tiene vectores. No se puede obtener un CAD "
            "fiel: haria falta vectorizar la imagen. Se exportara solo lo vectorial que exista.",
            rid="raster")
    elif pd.images:
        rec("info", "Contiene %d imagen(es)" % len(pd.images),
            "Las imagenes (logos, fotos, rellenos raster) no se convierten a vectores.", rid="images")

    # --- escala ---
    scale, ranked = detect_scale(pd.texts)
    if scale:
        alt = ", ".join("1:%d" % n for n in ranked[1:4])
        rec("ok", "Escala detectada 1:%d" % scale,
            "Se encontro en los textos del plano." + (" Otras escalas presentes: %s (si hay varios "
            "dibujos a distinta escala, calibra con una cota conocida)." % alt if alt else "") +
            " Verificala con la herramienta Calibrar midiendo una cota.",
            {"scale_mode": "ratio", "scale_ratio": scale}, rid="scale")
    else:
        rec("warn", "No se encontro la escala",
            "No hay textos tipo 'ESC 1:50'. Usa Calibrar: marca dos puntos de una cota conocida y "
            "escribe su medida real. Mientras tanto se exporta a escala 1:1 (mm de papel).",
            rid="scale")

    # --- capas ---
    if layers_used:
        rec("ok", "El PDF trae %d capas" % len(layers_used),
            "Se conservaran los nombres de capa originales (%s%s)." %
            (", ".join(layers_used[:6]), "..." if len(layers_used) > 6 else ""),
            {"layer_mode": "ocg"}, rid="layers")
    else:
        cols = Counter(p["color"] or p["fill"] for p in pd.paths)
        rec("info", "Sin capas en el PDF: se crearan por color",
            "El PDF no tiene capas (OCG). Se agrupara por color (%d colores distintos), que suele "
            "corresponder a los plumines/capas del CAD original." % len(cols),
            {"layer_mode": "color"}, rid="layers")

    # --- hatches (prueba con valores por defecto) ---
    trial = convert(pd, {"fills": False, "texts": False, "hatch_snap": False},
                    progress=lambda f, m=None: prog(0.05 + 0.9 * f, m))
    hs = trial["hatches"]
    if hs:
        n_abs = sum(h["n_lines"] for h in hs)
        n_susp = sum(1 for h in hs if h.get("suspect"))
        sps = sorted(f["spacing"] * PT_TO_MM for h in hs for f in h["families"])
        detail = ("Se pueden reconstruir %d hatches a partir de %d lineas sueltas. Espaciados en papel "
                  "entre %.2f y %.2f mm." % (len(hs), n_abs, sps[0], sps[-1]))
        apply = {"detect_hatches": True, "hatch_snap": True}
        if sps[-1] > 10:
            apply["hatch_max_spacing_mm"] = round(min(40, sps[-1] * 1.3), 1)
        rec("ok", "Hatches explotados detectados", detail, apply, rid="hatches")
        if n_susp:
            rec("warn", "%d hatch(es) dudosos" % n_susp,
                "Lineas paralelas de igual longitud o muy separadas (escaleras, persianas, "
                "celosias...). Aparecen marcados en naranja: revisalos y rechaza los que no sean hatch.",
                rid="suspect")
    else:
        rec("info", "No se detectaron hatches de lineas",
            "Si sabes que hay hatches, prueba a subir 'Espaciado maximo' o bajar 'Lineas minimas'.",
            rid="hatches")

    # --- rellenos ---
    if n_fill:
        apply = {"fills": True, "merge_fills": True}
        if n_fill > 20:
            rec("ok", "%d rellenos solidos fragmentados" % n_fill,
                "Se fusionaran los trozos adyacentes del mismo color en un solo HATCH SOLID.",
                apply, rid="fills")
        else:
            rec("info", "%d rellenos solidos" % n_fill, "Se convertiran a HATCH SOLID.", apply, rid="fills")
    if n_white:
        rec("info", "%d rellenos blancos (mascaras)" % n_white,
            "Suelen ser wipeouts/fondos que tapan geometria; se recomienda omitirlos.",
            {"skip_white_fills": True}, rid="white")

    # --- curvas ---
    if n_curves:
        rec("ok", "%d curvas Bezier" % n_curves,
            "Se convertiran a arcos y circulos reales cuando coincidan (lo normal en planos CAD); "
            "el resto queda como SPLINE.", {"curves_to_arcs": True}, rid="curves")

    if n_dash:
        rec("ok", "%d trazos discontinuos" % n_dash,
            "Se crearan tipos de linea equivalentes en lugar de explotar los guiones.",
            {"linetypes": True}, rid="dashes")

    if pd.clips:
        rec("info", "%d trazados de recorte" % len(pd.clips),
            "Hay geometria recortada (tipico de Revit/patrones). Se aplicara el recorte para no "
            "exportar lineas que sobresalen.", {"clip_paths": True}, rid="clips")

    if not pd.texts and tiny > 500:
        rec("warn", "Textos vectorizados",
            "No hay texto real y si muchos trazos diminutos (%d): probablemente los textos son fuentes "
            "SHX convertidas a lineas. Se exportaran como geometria." % tiny, rid="shx")
    elif pd.texts:
        rec("ok", "%d textos editables" % len(pd.texts),
            "Se exportaran como TEXT conservando posicion, rotacion y altura.", {"texts": True},
            rid="texts")

    rec("info", "Merge de lineas colineales",
        "Los PDF suelen partir lineas largas en tramos; se unen los tramos colineales y se eliminan "
        "duplicados.", {"merge_collinear": True, "join_polylines": True}, rid="collinear")

    return {
        "paper": paper_name(pd.width, pd.height),
        "metrics": {
            "trazados": pd.n_raw_paths, "segmentos": n_seg, "curvas": n_curves, "rellenos": n_fill,
            "textos": len(pd.texts), "imagenes": len(pd.images), "capas": len(layers_used),
            "recortes": len(pd.clips),
        },
        "recommendations": recs,
        "suggested": sug,
    }
