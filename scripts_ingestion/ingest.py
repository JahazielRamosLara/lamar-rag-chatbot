"""
Pipeline de ingesta completo: PDF -> chunks jerarquicos -> embeddings -> pgvector.

Uso tipico:

    python scripts_ingestion/ingest.py --pdf data/raw/reglamento_sep_r0.pdf

Opciones utiles:

    --dry-run     Segmenta e imprime el reporte, sin llamar a Bedrock ni a la BD.
                  Corre esto SIEMPRE antes de la ingesta real.
    --recrear     Borra la tabla y la vuelve a crear desde cero.
    --limite N    Procesa solo los primeros N chunks (para probar la conexion).

El script es idempotente: `ON CONFLICT (doc_id, chunk_uid) DO UPDATE` hace que
reingestar el mismo PDF actualice las filas en vez de duplicarlas.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))
sys.path.insert(0, str(RAIZ / "scripts_ingestion"))

import psycopg  # noqa: E402

from backend.bedrock import embed_lote  # noqa: E402
from backend.config import settings  # noqa: E402
from chunker import Chunk, resumen, segmentar  # noqa: E402
from pdf_extract import extraer_texto  # noqa: E402

SCHEMA_SQL = RAIZ / "infra" / "schema.sql"
LOTE = 25  # chunks por lote de embeddings/inserción


# ---------------------------------------------------------------------------
# Base de datos
# ---------------------------------------------------------------------------


def crear_esquema(conexion, recrear: bool = False) -> None:
    """Aplica infra/schema.sql. Con --recrear elimina la tabla primero."""
    with conexion.cursor() as cur:
        if recrear:
            print("Eliminando tabla existente (--recrear)...")
            cur.execute(f"DROP TABLE IF EXISTS {settings.db_table} CASCADE;")

        sql = SCHEMA_SQL.read_text(encoding="utf-8")

        # Permite renombrar la tabla desde .env sin editar el .sql
        if settings.db_table != "reglamento_chunks":
            sql = sql.replace("reglamento_chunks", settings.db_table)

        # La dimensión del vector debe coincidir con el modelo de embeddings
        sql = sql.replace("VECTOR(1024)", f"VECTOR({settings.embed_dim})")

        cur.execute(sql)
    conexion.commit()
    print(f"Esquema listo (tabla `{settings.db_table}`, VECTOR({settings.embed_dim})).")


def insertar_lote(conexion, chunks: list[Chunk], vectores: list[list[float]]) -> None:
    """Inserta o actualiza un lote de chunks con su vector."""
    sql = f"""
        INSERT INTO {settings.db_table} (
            doc_id, chunk_uid, titulo, titulo_num, capitulo, capitulo_num,
            articulo, articulo_sufijo, fragmento, total_fragmentos,
            encabezado, tipo, contenido, texto_embebido, metadata, embedding
        )
        VALUES (
            %(doc_id)s, %(chunk_uid)s, %(titulo)s, %(titulo_num)s,
            %(capitulo)s, %(capitulo_num)s, %(articulo)s, %(articulo_sufijo)s,
            %(fragmento)s, %(total_fragmentos)s, %(encabezado)s, %(tipo)s,
            %(contenido)s, %(texto_embebido)s,
            -- Los casts explicitos son obligatorios: psycopg manda estos dos
            -- parametros como texto y PostgreSQL no castea texto -> jsonb ni
            -- texto -> vector de forma implicita.
            %(metadata)s::jsonb, %(embedding)s::vector
        )
        ON CONFLICT (doc_id, chunk_uid) DO UPDATE SET
            titulo           = EXCLUDED.titulo,
            titulo_num       = EXCLUDED.titulo_num,
            capitulo         = EXCLUDED.capitulo,
            capitulo_num     = EXCLUDED.capitulo_num,
            articulo         = EXCLUDED.articulo,
            articulo_sufijo  = EXCLUDED.articulo_sufijo,
            fragmento        = EXCLUDED.fragmento,
            total_fragmentos = EXCLUDED.total_fragmentos,
            encabezado       = EXCLUDED.encabezado,
            tipo             = EXCLUDED.tipo,
            contenido        = EXCLUDED.contenido,
            texto_embebido   = EXCLUDED.texto_embebido,
            metadata         = EXCLUDED.metadata,
            embedding        = EXCLUDED.embedding,
            creado_en        = now();
    """

    filas = []
    for chunk, vector in zip(chunks, vectores):
        filas.append(
            {
                "doc_id": "SEP-R-0",
                "chunk_uid": chunk.chunk_uid,
                "titulo": chunk.titulo,
                "titulo_num": chunk.titulo_num,
                "capitulo": chunk.capitulo,
                "capitulo_num": chunk.capitulo_num,
                "articulo": chunk.articulo,
                "articulo_sufijo": chunk.articulo_sufijo,
                "fragmento": chunk.fragmento,
                "total_fragmentos": chunk.total_fragmentos,
                "encabezado": chunk.encabezado,
                "tipo": chunk.tipo,
                "contenido": chunk.contenido,
                "texto_embebido": chunk.texto_embebido,
                "metadata": json.dumps(chunk.metadata(), ensure_ascii=False),
                # pgvector acepta el literal '[0.1,0.2,...]'
                "embedding": "[" + ",".join(f"{v:.6f}" for v in vector) + "]",
            }
        )

    with conexion.cursor() as cur:
        cur.executemany(sql, filas)
    conexion.commit()


def verificar(conexion) -> None:
    """Consulta de comprobación — útil como captura de evidencia."""
    with conexion.cursor() as cur:
        cur.execute(f"SELECT count(*), count(DISTINCT articulo) FROM {settings.db_table};")
        total, articulos = cur.fetchone()
        cur.execute(
            f"SELECT vector_dims(embedding) FROM {settings.db_table} LIMIT 1;"
        )
        fila = cur.fetchone()
        dims = fila[0] if fila else 0

    print("\n--- Verificación en la base de datos ---")
    print(f"Filas insertadas    : {total}")
    print(f"Artículos distintos : {articulos}")
    print(f"Dimensión del vector: {dims}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="Ingesta del Reglamento SEP R-0")
    parser.add_argument("--pdf", help="Ruta al PDF del reglamento")
    parser.add_argument("--txt", help="Ruta a un .txt ya extraído (alternativa al PDF)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Solo segmenta y reporta; no llama a Bedrock ni a la BD")
    parser.add_argument("--recrear", action="store_true",
                        help="Elimina y recrea la tabla antes de ingestar")
    parser.add_argument("--limite", type=int, help="Procesa solo los primeros N chunks")
    args = parser.parse_args()

    if not args.pdf and not args.txt:
        parser.error("Indica --pdf o --txt")

    # ---- Fase 1: extracción y segmentación ----
    print("[1/3] Extrayendo y segmentando el documento...")
    if args.pdf:
        texto = extraer_texto(args.pdf)
    else:
        texto = Path(args.txt).read_text(encoding="utf-8")

    chunks = segmentar(texto)
    if not chunks:
        print("ERROR: no se generó ningún chunk. Revisa la extracción del PDF.",
              file=sys.stderr)
        return 1

    print(resumen(chunks))

    if args.limite:
        chunks = chunks[: args.limite]
        print(f"\n(--limite) Se procesarán solo {len(chunks)} chunks.")

    if args.dry_run:
        print("\n(--dry-run) No se llamó a Bedrock ni a la base de datos.")
        return 0

    # ---- Fase 2: base de datos ----
    print(f"\n[2/3] Conectando a {settings.pghost}:{settings.pgport}/{settings.pgdatabase} ...")
    with psycopg.connect(settings.dsn) as conexion:
        crear_esquema(conexion, recrear=args.recrear)

        # ---- Fase 3: embeddings + inserción ----
        print(f"\n[3/3] Generando embeddings con {settings.bedrock_embed_model_id} ...")
        inicio = time.time()
        procesados = 0

        for i in range(0, len(chunks), LOTE):
            lote = chunks[i : i + LOTE]
            vectores = embed_lote([c.texto_embebido for c in lote])
            insertar_lote(conexion, lote, vectores)
            procesados += len(lote)
            print(f"    {procesados}/{len(chunks)} chunks ingestados", flush=True)

        print(f"\nListo en {time.time() - inicio:.1f}s.")
        verificar(conexion)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
