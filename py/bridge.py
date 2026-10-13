"""Puente entre el navegador (worker.js) y el motor de conversion (core/).
Se ejecuta dentro de Pyodide (Python compilado a WebAssembly)."""
import json
import os
import re
import time

import pymupdf

from core.analyze import analyze
from core.dxfout import export
from core.extract import extract_page
from core.pipeline import convert
from core.preview import to_preview

DOCS = {}    # doc_id -> {"doc", "name"}
PAGES = {}   # (doc_id, page, clip) -> PageData


def _sub(prog, a, b):
    return lambda f, m=None: prog(a + (b - a) * f, m)


def open_pdf(doc_id, data, name):
    raw = bytes(data.to_py()) if hasattr(data, "to_py") else bytes(data)
    try:
        doc = pymupdf.open(stream=raw, filetype="pdf")
    except Exception:
        raise ValueError("El archivo no es un PDF valido")
    if doc.needs_pass:
        raise ValueError("El PDF esta protegido con contrasena")
    DOCS[doc_id] = {"doc": doc, "name": name}
    return json.dumps({"doc_id": doc_id, "name": name,
                       "pages": [{"w": p.rect.width, "h": p.rect.height} for p in doc]})


def _page(doc_id, page, clip, prog):
    key = (doc_id, page, bool(clip))
    if key not in PAGES:
        PAGES[key] = extract_page(DOCS[doc_id]["doc"], page, clip_geometry=clip, progress=prog)
    return PAGES[key]


def job(kind, doc_id, page, opts_json, prog):
    """kind: load | process | export. Devuelve JSON (o bytes del DXF en export)."""
    t0 = time.time()
    opts = json.loads(opts_json or "{}")
    page = int(page)
    if kind == "load":
        pd = _page(doc_id, page, True, _sub(prog, 0.0, 0.25))
        prog(0.25, "Analizando el plano")
        an = analyze(pd, progress=_sub(prog, 0.25, 0.7))
        o = dict(opts)
        o.update(an.get("suggested", {}))
        res = convert(pd, o, progress=_sub(prog, 0.7, 0.96))
        prog(0.97, "Preparando la vista previa")
        return json.dumps({"analysis": an, "preview": to_preview(res), "elapsed": time.time() - t0})
    if kind == "process":
        pd = _page(doc_id, page, opts.get("clip_paths", True), _sub(prog, 0.0, 0.3))
        res = convert(pd, opts, progress=_sub(prog, 0.3, 0.95))
        prog(0.96, "Preparando la vista previa")
        return json.dumps({"preview": to_preview(res), "elapsed": time.time() - t0})
    if kind == "export":
        pd = _page(doc_id, page, opts.get("clip_paths", True), _sub(prog, 0.0, 0.2))
        res = convert(pd, opts, progress=_sub(prog, 0.2, 0.5))
        out = "/tmp/plano.dxf"
        export(res, out, "dxf", progress=_sub(prog, 0.5, 1.0))
        info = DOCS[doc_id]
        stem = re.sub(r"[^\w\-. ]", "_", os.path.splitext(info["name"])[0])
        if info["doc"].page_count > 1:
            stem += "_p%d" % (page + 1)
        with open(out, "rb") as f:
            data = f.read()
        os.remove(out)
        return json.dumps({"name": stem + ".dxf", "elapsed": time.time() - t0}), data
    raise ValueError("tarea desconocida: %s" % kind)


def render(doc_id, page, dpi):
    pix = DOCS[doc_id]["doc"][int(page)].get_pixmap(dpi=int(dpi), alpha=False)
    return pix.tobytes("png")
