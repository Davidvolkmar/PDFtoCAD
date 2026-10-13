/* Motor de conversion: todo se calcula en el equipo del usuario con Python compilado a
   WebAssembly (Pyodide) dentro de un Web Worker (worker.js). */
"use strict";

window.Backend = (() => {
  const worker = new Worker("assets/js/worker.js?v=61e3f2d81f");  // ruta relativa a index.html
  const pending = new Map();
  let seq = 0;
  let ready = null;

  worker.onmessage = (ev) => {
    const { id, type } = ev.data;
    const p = pending.get(id);
    if (!p) return;
    if (type === "progress") {
      p.onProgress(ev.data.f, ev.data.msg, (performance.now() - p.t0) / 1000);
    } else {
      pending.delete(id);
      if (type === "result") p.resolve(ev.data.data);
      else p.reject(new Error(ev.data.message));
    }
  };
  worker.onerror = (e) => {
    for (const p of pending.values()) p.reject(new Error(e.message || "Error en el motor"));
    pending.clear();
  };

  function call(cmd, args, onProgress = () => { }, transfer) {
    const id = ++seq;
    return new Promise((resolve, reject) => {
      pending.set(id, { resolve, reject, onProgress, t0: performance.now() });
      worker.postMessage({ id, cmd, args }, transfer || []);
    });
  }

  return {

    /** Descarga y arranca Python + librerias (cacheado por el navegador tras la 1a vez). */
    init(onProgress) {
      ready = ready || call("init", {}, onProgress || (() => { }));
      return ready;
    },

    async upload(file, onProgress) {
      await this.init();
      onProgress(0.3);
      const buf = await file.arrayBuffer();
      onProgress(0.7);
      const doc_id = Math.random().toString(36).slice(2, 12);
      const data = new Uint8Array(buf);
      return call("open", { doc_id, data, name: file.name }, () => { }, [data.buffer]);
    },

    async job(kind, body, onProgress, isCurrent = () => true) {
      await this.init();
      const t0 = performance.now();
      const r = await call("job", { kind, doc_id: body.doc_id, page: body.page, opts: body.opts || {} },
        onProgress);
      if (!isCurrent()) throw new Error("cancelado");
      r.elapsed = r.elapsed || (performance.now() - t0) / 1000;
      if (kind === "export") {
        const blob = new Blob([r.bytes], { type: "application/dxf" });
        r.url = URL.createObjectURL(blob);
        setTimeout(() => URL.revokeObjectURL(r.url), 60000);
      }
      return r;
    },

    async render(docId, page, dpi) {
      const bytes = await call("render", { doc_id: docId, page, dpi });
      return URL.createObjectURL(new Blob([bytes], { type: "image/png" }));
    },
  };
})();
