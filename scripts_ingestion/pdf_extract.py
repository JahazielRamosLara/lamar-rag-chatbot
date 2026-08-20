"""
Extraccion y limpieza del texto del PDF del reglamento.

pdfplumber respeta mejor el flujo de lectura que pypdf en documentos con
columnas o tablas, asi que es el extractor principal; pypdf queda como plan B
por si pdfplumber falla con algun PDF.

Lo importante aqui no es solo sacar el texto, sino dejarlo en un formato que el
chunker pueda parsear: encabezados al inicio de linea, sin guiones de corte de
palabra y sin encabezados/pies de pagina repetidos.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

# Ligaduras y comillas tipograficas que rompen los regex del chunker.
REEMPLAZOS = {
    "ﬁ": "fi", "ﬂ": "fl",
    "‘": "'", "’": "'",
    "“": '"', "”": '"',
    " ": " ",   # espacio duro
    "–": "–", "—": "—",
}

# "inscrip-\ncion" -> "inscripcion"
RE_GUION_CORTE = re.compile(r"(\w)[-‐‑]\s*\n\s*(\w)")
# Espacios repetidos dentro de la linea
RE_ESPACIOS = re.compile(r"[ \t]{2,}")
# Mas de dos saltos de linea seguidos
RE_SALTOS = re.compile(r"\n{3,}")


def _extraer_pdfplumber(ruta: Path) -> list[str]:
    import pdfplumber

    paginas: list[str] = []
    with pdfplumber.open(str(ruta)) as pdf:
        for pagina in pdf.pages:
            paginas.append(pagina.extract_text() or "")
    return paginas


def _extraer_pypdf(ruta: Path) -> list[str]:
    from pypdf import PdfReader

    lector = PdfReader(str(ruta))
    return [pagina.extract_text() or "" for pagina in lector.pages]


def _quita_encabezados_repetidos(paginas: list[str]) -> list[str]:
    """
    Detecta lineas que se repiten en la mayoria de las paginas (encabezado
    institucional, pie de pagina, folio) y las elimina. El umbral de 60% evita
    borrar texto legitimo que casualmente se repita dos o tres veces.
    """
    if len(paginas) < 4:
        return paginas

    candidatas: Counter[str] = Counter()
    for pagina in paginas:
        lineas = [l.strip() for l in pagina.splitlines() if l.strip()]
        # Solo las 2 primeras y 2 ultimas lineas pueden ser encabezado/pie.
        for linea in set(lineas[:2] + lineas[-2:]):
            if len(linea) < 80:  # un parrafo largo no es un encabezado
                candidatas[linea] += 1

    umbral = int(len(paginas) * 0.6)
    basura = {linea for linea, veces in candidatas.items() if veces >= umbral}
    if not basura:
        return paginas

    limpias: list[str] = []
    for pagina in paginas:
        limpias.append(
            "\n".join(l for l in pagina.splitlines() if l.strip() not in basura)
        )
    return limpias


def limpiar(texto: str) -> str:
    """Normaliza el texto crudo para que los regex del chunker funcionen."""
    for origen, destino in REEMPLAZOS.items():
        texto = texto.replace(origen, destino)

    texto = RE_GUION_CORTE.sub(r"\1\2", texto)
    texto = "\n".join(RE_ESPACIOS.sub(" ", l).rstrip() for l in texto.splitlines())
    texto = RE_SALTOS.sub("\n\n", texto)
    return texto.strip()


def extraer_texto(ruta_pdf: str | Path, motor: str = "pdfplumber") -> str:
    """
    Devuelve el texto completo del PDF, limpio y listo para segmentar.

    `motor` acepta 'pdfplumber' (default) o 'pypdf'.
    """
    ruta = Path(ruta_pdf)
    if not ruta.exists():
        raise FileNotFoundError(
            f"No encontré el PDF en {ruta}.\n"
            "Coloca el Reglamento SEP R-0 en data/raw/ y vuelve a intentar."
        )

    if motor == "pypdf":
        paginas = _extraer_pypdf(ruta)
    else:
        try:
            paginas = _extraer_pdfplumber(ruta)
        except Exception as error:  # PDF raro -> intentamos con pypdf
            print(f"[aviso] pdfplumber falló ({error}); reintentando con pypdf...")
            paginas = _extraer_pypdf(ruta)

    if not any(p.strip() for p in paginas):
        raise ValueError(
            "El PDF no devolvió texto. Probablemente sea un PDF escaneado "
            "(solo imágenes) y necesite OCR antes de procesarse."
        )

    paginas = _quita_encabezados_repetidos(paginas)
    return limpiar("\n".join(paginas))
