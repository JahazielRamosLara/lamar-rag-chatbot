"""
Verifica la segmentacion sin tocar AWS ni la base de datos.

Uso:
    python scripts_ingestion/probar_chunker.py                        # usa la muestra
    python scripts_ingestion/probar_chunker.py --pdf data/raw/reg.pdf # usa el PDF real
    python scripts_ingestion/probar_chunker.py --ver 4                # imprime el articulo 4
    python scripts_ingestion/probar_chunker.py --json salida.json     # exporta los chunks

Es el paso que conviene correr primero: si el conteo de articulos y capitulos
no cuadra con el documento, el problema esta en la extraccion del PDF, no en el
RAG, y arreglarlo ahora ahorra reprocesar embeddings.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from chunker import segmentar, resumen  # noqa: E402

RAIZ = Path(__file__).resolve().parent.parent
MUESTRA = RAIZ / "data" / "muestra" / "reglamento_muestra.txt"


def main() -> int:
    parser = argparse.ArgumentParser(description="Prueba del chunker jerárquico")
    parser.add_argument("--pdf", help="Ruta al PDF del reglamento")
    parser.add_argument("--txt", help="Ruta a un .txt ya extraído")
    parser.add_argument("--ver", type=int, help="Imprime el artículo con este número")
    parser.add_argument("--json", help="Exporta todos los chunks a este archivo JSON")
    parser.add_argument("--listar", action="store_true", help="Lista todos los chunks")
    args = parser.parse_args()

    if args.pdf:
        from pdf_extract import extraer_texto

        texto = extraer_texto(args.pdf)
        origen = args.pdf
    else:
        ruta = Path(args.txt) if args.txt else MUESTRA
        if not ruta.exists():
            print(f"No existe {ruta}", file=sys.stderr)
            return 1
        texto = ruta.read_text(encoding="utf-8")
        origen = str(ruta)

    chunks = segmentar(texto)

    print(f"Fuente: {origen}")
    print(f"Caracteres extraídos: {len(texto):,}")
    print("-" * 62)
    print(resumen(chunks))
    print("-" * 62)

    if args.listar:
        for chunk in chunks:
            etiqueta = chunk.encabezado or chunk.tipo
            capitulo = chunk.capitulo or "(sin capítulo)"
            print(f"  [{chunk.chunk_uid:<16}] {etiqueta:<34} | {capitulo}")

    if args.ver is not None:
        encontrados = [c for c in chunks if c.articulo == args.ver]
        if not encontrados:
            print(f"\nNo se encontró el artículo {args.ver}.")
        for chunk in encontrados:
            print(f"\n=== {chunk.encabezado} ===")
            print(f"Título  : {chunk.titulo}")
            print(f"Capítulo: {chunk.capitulo}")
            print(f"Incisos : {chunk.incisos or '—'}")
            print(f"Numerales: {chunk.numerales or '—'}")
            print(f"\n{chunk.contenido}\n")

    if args.json:
        salida = Path(args.json)
        salida.parent.mkdir(parents=True, exist_ok=True)
        salida.write_text(
            json.dumps([c.to_dict() for c in chunks], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"\nChunks exportados a {salida}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
