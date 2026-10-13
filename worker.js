/* Web Worker: ejecuta el motor de conversion (Python + PyMuPDF + Shapely + ezdxf)
   compilado a WebAssembly con Pyodide, fuera del hilo de la interfaz. */
"use strict";

const PYODIDE_VERSION = "0.28.3";
importScripts(`https://cdn.jsdelivr.net/pyodide/v${PYODIDE_VERSION}/full/pyodide.js`);

let py = null;
let bridge = null;
let initPromise = null;
const queue = [];
let busy = false;

function post(msg, transfer) { self.postMessage(msg, transfer || []); }

async function init(id) {
  const prog = (f, m) => post({ id, type: "progress", f, msg: m });
  prog(0.02, "Descargando Python para el navegador (solo la primera vez)");
  py = await loadPyodide();
  prog(0.3, "Cargando PyMuPDF, Shapely y NumPy");
  const pkgs = ["numpy", "shapely", "pymupdf", "micropip"];
  let n = 0;
  await py.loadPackage(pkgs, {
    messageCallback: (m) => {
      if (/^Loaded /.test(m)) { n++; prog(0.3 + 0.45 * Math.min(1, n / 8), m.replace("Loaded", "Cargado")); }
    },
  });
  prog(0.78, "Instalando ezdxf (escritura DXF)");
  // files.json sin cache; el resto se pide con ?v=<version> para no usar copias viejas
  const files = await (await fetch("py/files.json", { cache: "no-cache" })).json();
  const v = encodeURIComponent(files.version || "1");
  const micropip = py.pyimport("micropip");
  await micropip.install(new URL(`py/${files.ezdxf_wheel}`, self.location).href);  // (rueda versionada por nombre)
  prog(0.9, "Cargando el motor PDF a CAD");
  const root = "/home/pyodide/app";
  for (const f of files.sources) {
    const txt = await (await fetch(`py/${f}?v=${v}`)).text();
    const dir = (root + "/" + f).split("/").slice(0, -1).join("/");
    py.FS.mkdirTree(dir);
    py.FS.writeFile(`${root}/${f}`, txt);
  }
  py.runPython(`import sys; sys.path.insert(0, "${root}")`);
  bridge = py.pyimport("bridge");
  prog(1, "Motor listo");
}

function errMsg(e) {
  // de una traza de Python, quedarse con la ultima linea legible
  const s = String((e && e.message) || e);
  const lines = s.trim().split("\n").filter(Boolean);
  const last = lines[lines.length - 1] || s;
  return last.replace(/^\w+(Error|Exception):\s*/, "");
}

async function run(task) {
  const { id, cmd, args } = task;
  const prog = (f, m) => post({ id, type: "progress", f, msg: m });
  try {
    if (cmd === "init") {
      initPromise = initPromise || init(id);
      await initPromise;
      post({ id, type: "result", data: null });
      return;
    }
    await initPromise;
    if (cmd === "open") {
      const r = bridge.open_pdf(args.doc_id, args.data, args.name);
      post({ id, type: "result", data: JSON.parse(r) });
    } else if (cmd === "job") {
      const out = bridge.job(args.kind, args.doc_id, args.page, JSON.stringify(args.opts), prog);
      if (args.kind === "export") {
        const meta = JSON.parse(out.get(0));
        const bytes = out.get(1).toJs();
        out.destroy();
        post({ id, type: "result", data: { ...meta, bytes } }, [bytes.buffer]);
      } else {
        post({ id, type: "result", data: JSON.parse(out) });
      }
    } else if (cmd === "render") {
      const r = bridge.render(args.doc_id, args.page, args.dpi);
      const bytes = r.toJs();
      r.destroy();
      post({ id, type: "result", data: bytes }, [bytes.buffer]);
    }
  } catch (e) {
    post({ id, type: "error", message: errMsg(e) });
  }
}

async function pump() {
  if (busy) return;
  busy = true;
  while (queue.length) {
    const task = queue.shift();
    // reprocesos seguidos: solo vale el ultimo (los anteriores se descartan)
    if (task.cmd === "job" && task.args.kind === "process" &&
        queue.some((t) => t.cmd === "job" && t.args.kind === "process")) {
      post({ id: task.id, type: "error", message: "cancelado" });
      continue;
    }
    await run(task);
    // ceder el turno para recibir mensajes pendientes antes de la siguiente tarea
    await new Promise((r) => setTimeout(r, 0));
  }
  busy = false;
}

self.onmessage = (ev) => {
  queue.push(ev.data);
  pump();
};
