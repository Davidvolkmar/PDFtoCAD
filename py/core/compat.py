"""Compatibilidad entre versiones de Shapely/GEOS (escritorio vs. navegador/Pyodide)."""
import shapely

# La union de cobertura (muy rapida) es fiable con GEOS >= 3.12 / Shapely >= 2.1.
# En versiones anteriores (p.ej. Pyodide con Shapely 2.0.x) puede abortar el proceso
# en lugar de lanzar una excepcion, asi que alli se usa siempre la union general.
FAST_COVERAGE = shapely.geos_version >= (3, 12, 0) and tuple(
    int(x) for x in shapely.__version__.split(".")[:2]) >= (2, 1)


def coverage_union(arr):
    """Union de poligonos que (se espera) no se solapan. Devuelve None si no aplica."""
    if not FAST_COVERAGE:
        return None
    try:
        return shapely.coverage_union_all(arr)
    except Exception:
        return None
