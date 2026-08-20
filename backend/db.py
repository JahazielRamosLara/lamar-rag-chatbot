"""
Acceso a la base vectorial (AWS RDS PostgreSQL + pgvector) con asyncpg.

Aquí vive la consulta que le da nombre a la práctica: la búsqueda por
similitud coseno con el operador `<=>` de pgvector, combinada con búsqueda
léxica y con filtrado por metadata.
"""

from __future__ import annotations

import logging

import asyncpg

from .config import settings

log = logging.getLogger(__name__)

_pool: asyncpg.Pool | None = None

# Columnas que devuelven todas las consultas, para armar las fuentes citadas.
_COLUMNAS = """
    chunk_uid, encabezado, titulo, capitulo, articulo, articulo_sufijo,
    fragmento, total_fragmentos, contenido, metadata
"""


async def iniciar_pool() -> None:
    """Crea el pool de conexiones al arrancar la API."""
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            min_size=1,
            max_size=8,
            command_timeout=30,
            **settings.asyncpg_kwargs,
        )
        log.info("Pool de conexiones abierto contra %s", settings.pghost)


async def cerrar_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def _pool_activo() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("El pool no está iniciado. Llama a iniciar_pool() primero.")
    return _pool


def _a_literal(vector: list[float]) -> str:
    """pgvector recibe el vector como el literal de texto '[0.1,0.2,...]'."""
    return "[" + ",".join(f"{v:.6f}" for v in vector) + "]"


def _fila_a_dict(fila: asyncpg.Record, similitud: float, via: str) -> dict:
    return {
        "chunk_uid": fila["chunk_uid"],
        "encabezado": fila["encabezado"],
        "titulo": fila["titulo"],
        "capitulo": fila["capitulo"],
        "articulo": fila["articulo"],
        "articulo_sufijo": fila["articulo_sufijo"],
        "fragmento": fila["fragmento"],
        "total_fragmentos": fila["total_fragmentos"],
        "contenido": fila["contenido"],
        "similitud": round(float(similitud), 4),
        "via": via,
    }


# ---------------------------------------------------------------------------
# 1. Filtrado por metadata — consultas explícitas
# ---------------------------------------------------------------------------


async def buscar_por_articulo(numero: int, sufijo: str | None = None) -> list[dict]:
    """
    Recupera un artículo completo por su número.

    Este es el camino de "dame el artículo 4": no hay búsqueda semántica, hay
    un índice B-tree sobre la columna `articulo`. Devuelve todos los fragmentos
    en orden si el artículo fue partido durante la ingesta.
    """
    sql = f"""
        SELECT {_COLUMNAS}
        FROM {settings.db_table}
        WHERE articulo = $1
          AND ($2::text IS NULL OR articulo_sufijo IS NOT DISTINCT FROM $2)
        ORDER BY articulo_sufijo NULLS FIRST, fragmento
    """
    async with _pool_activo().acquire() as con:
        filas = await con.fetch(sql, numero, sufijo)
    # Similitud 1.0: es una coincidencia exacta, no una aproximación.
    return [_fila_a_dict(f, 1.0, "metadata") for f in filas]


async def buscar_por_capitulo(numero: int) -> list[dict]:
    """Recupera todos los artículos de un capítulo."""
    sql = f"""
        SELECT {_COLUMNAS}
        FROM {settings.db_table}
        WHERE capitulo_num = $1
        ORDER BY articulo NULLS FIRST, fragmento
    """
    async with _pool_activo().acquire() as con:
        filas = await con.fetch(sql, numero)
    return [_fila_a_dict(f, 1.0, "metadata") for f in filas]


# ---------------------------------------------------------------------------
# 2. Búsqueda vectorial — consultas semánticas
# ---------------------------------------------------------------------------


async def buscar_vectorial(vector: list[float], k: int) -> list[dict]:
    """
    Vecinos más cercanos por distancia coseno.

    `embedding <=> $1` devuelve la distancia coseno en [0, 2]; la convertimos a
    similitud con `1 - distancia` para que el umbral del backend se lea como
    un porcentaje de parecido.
    """
    sql = f"""
        SELECT {_COLUMNAS},
               1 - (embedding <=> $1::vector) AS similitud
        FROM {settings.db_table}
        ORDER BY embedding <=> $1::vector
        LIMIT $2
    """
    async with _pool_activo().acquire() as con:
        filas = await con.fetch(sql, _a_literal(vector), k)
    return [_fila_a_dict(f, f["similitud"], "vectorial") for f in filas]


async def buscar_lexica(consulta: str, k: int) -> list[dict]:
    """
    Búsqueda de texto completo en español (índice GIN sobre to_tsvector).

    Complementa a la vectorial cuando el alumno usa un término literal del
    reglamento que el embedding podría diluir entre artículos parecidos.
    """
    sql = f"""
        SELECT {_COLUMNAS},
               ts_rank(to_tsvector('spanish', contenido),
                       plainto_tsquery('spanish', $1)) AS similitud
        FROM {settings.db_table}
        WHERE to_tsvector('spanish', contenido) @@ plainto_tsquery('spanish', $1)
        ORDER BY similitud DESC
        LIMIT $2
    """
    async with _pool_activo().acquire() as con:
        filas = await con.fetch(sql, consulta, k)
    return [_fila_a_dict(f, f["similitud"], "lexica") for f in filas]


# ---------------------------------------------------------------------------
# 3. Búsqueda híbrida
# ---------------------------------------------------------------------------


async def buscar_hibrida(vector: list[float], consulta: str, k: int) -> list[dict]:
    """
    Fusiona el ranking vectorial y el léxico con Reciprocal Rank Fusion (RRF).

    RRF suma 1/(60 + posición) de cada lista. La ventaja frente a promediar
    scores es que no hay que normalizar dos escalas distintas (similitud coseno
    vs. ts_rank), que no son comparables entre sí.
    """
    # Se piden más candidatos de los necesarios para que la fusión tenga
    # material con el cual reordenar.
    amplitud = max(k * 3, 12)

    vectoriales = await buscar_vectorial(vector, amplitud)
    lexicos = await buscar_lexica(consulta, amplitud)

    K_RRF = 60
    puntajes: dict[str, float] = {}
    documentos: dict[str, dict] = {}

    for lista in (vectoriales, lexicos):
        for posicion, doc in enumerate(lista):
            uid = doc["chunk_uid"]
            puntajes[uid] = puntajes.get(uid, 0.0) + 1.0 / (K_RRF + posicion + 1)
            # Conservamos la similitud coseno (interpretable) para mostrarla.
            if uid not in documentos or doc["via"] == "vectorial":
                documentos[uid] = doc

    ordenados = sorted(puntajes.items(), key=lambda par: par[1], reverse=True)

    resultado: list[dict] = []
    for uid, _ in ordenados[:k]:
        doc = dict(documentos[uid])
        doc["via"] = "hibrida"
        resultado.append(doc)
    return resultado


# ---------------------------------------------------------------------------
# 4. Salud del sistema
# ---------------------------------------------------------------------------


async def estadisticas() -> dict:
    """Conteo de chunks y dimensión del vector, para el endpoint /api/salud."""
    async with _pool_activo().acquire() as con:
        total = await con.fetchval(f"SELECT count(*) FROM {settings.db_table}")
        dims = await con.fetchval(
            f"SELECT vector_dims(embedding) FROM {settings.db_table} LIMIT 1"
        )
    return {"chunks": total or 0, "dimension": dims or 0}
