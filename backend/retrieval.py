"""
Capa de recuperación: decide CÓMO buscar antes de buscar.

El reto de la práctica es que el chatbot responda igual de bien a:

    "¿Qué dice el artículo 4 del reglamento?"   -> petición explícita
    "Dame el artículo 4"                        -> petición explícita
    "¿Cuántas faltas puedo tener?"              -> pregunta semántica
    "¿Qué necesito para inscribirme?"           -> pregunta semántica

Una búsqueda puramente vectorial falla en el primer caso: los embeddings de
"artículo 4" y "artículo 40" son casi idénticos. Por eso primero detectamos la
intención con expresiones regulares y, si el alumno nombró un artículo o un
capítulo, resolvemos por metadata (índice B-tree) y sólo complementamos con
vectores.
"""

from __future__ import annotations

import re
import unicodedata

from . import db
from .bedrock import embed_texto
from .config import settings

# ---------------------------------------------------------------------------
# Detección de referencias explícitas
# ---------------------------------------------------------------------------

# "artículo 4", "art. 12", "art 7 bis", "articulo 16 BIS"
RE_ARTICULO = re.compile(
    r"\b(?:art[ií]culos?|arts?\.?)\s*"
    r"(\d{1,3})\s*"
    r"(?:[°ºo]\b)?\s*"
    r"(bis|ter|qu[aá]ter|quinquies)?",
    re.IGNORECASE,
)

# Enumeraciones: "los artículos 7 y 12", "artículos 3, 5 y 9".
# El regex de arriba sólo captura el primer número porque exige la palabra
# "artículo" pegada; este cubre la lista completa.
RE_ARTICULOS_LISTA = re.compile(
    r"\bart[ií]culos\s+(\d{1,3}(?:\s*(?:,|y|e)\s*\d{1,3})+)",
    re.IGNORECASE,
)

# "artículo cuarto", "artículo primero"
RE_ARTICULO_PALABRA = re.compile(
    r"\b(?:art[ií]culos?|arts?\.?)\s+"
    r"(primero|segundo|tercero|cuarto|quinto|sexto|s[eé]ptimo|octavo|noveno|"
    r"d[eé]cimo|und[eé]cimo|duod[eé]cimo)\b",
    re.IGNORECASE,
)

# "capítulo III", "capitulo 3", "capítulo tercero"
RE_CAPITULO = re.compile(
    r"\bcap[ií]tulos?\s+"
    r"([ivxlcdm]{1,7}|\d{1,2}|primero|segundo|tercero|cuarto|quinto|sexto|"
    r"s[eé]ptimo|octavo|noveno|d[eé]cimo)\b",
    re.IGNORECASE,
)

PALABRA_A_NUMERO = {
    "primero": 1, "segundo": 2, "tercero": 3, "cuarto": 4, "quinto": 5,
    "sexto": 6, "septimo": 7, "octavo": 8, "noveno": 9, "decimo": 10,
    "undecimo": 11, "duodecimo": 12,
}

VALOR_ROMANO = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100, "d": 500, "m": 1000}


def _sin_acentos(texto: str) -> str:
    descompuesto = unicodedata.normalize("NFD", texto)
    return "".join(c for c in descompuesto if unicodedata.category(c) != "Mn")


def _romano(token: str) -> int | None:
    token = token.lower()
    if not token or any(c not in VALOR_ROMANO for c in token):
        return None
    total, previo = 0, 0
    for caracter in reversed(token):
        valor = VALOR_ROMANO[caracter]
        total = total - valor if valor < previo else total + valor
        previo = max(previo, valor)
    return total or None


def detectar_referencias(pregunta: str) -> dict:
    """
    Extrae de la pregunta los artículos y capítulos mencionados explícitamente.

    Devuelve, por ejemplo:
        {"articulos": [(4, None), (16, "BIS")], "capitulos": [3]}
    """
    articulos: list[tuple[int, str | None]] = []
    capitulos: list[int] = []

    for match in RE_ARTICULO.finditer(pregunta):
        numero = int(match.group(1))
        sufijo = match.group(2).upper() if match.group(2) else None
        if 1 <= numero <= 999 and (numero, sufijo) not in articulos:
            articulos.append((numero, sufijo))

    for match in RE_ARTICULOS_LISTA.finditer(pregunta):
        for token in re.split(r"\s*(?:,|y|e)\s*", match.group(1)):
            if token.isdigit():
                numero = int(token)
                if 1 <= numero <= 999 and (numero, None) not in articulos:
                    articulos.append((numero, None))

    for match in RE_ARTICULO_PALABRA.finditer(pregunta):
        numero = PALABRA_A_NUMERO.get(_sin_acentos(match.group(1)).lower())
        if numero and (numero, None) not in articulos:
            articulos.append((numero, None))

    for match in RE_CAPITULO.finditer(pregunta):
        token = match.group(1)
        numero = (
            int(token) if token.isdigit()
            else PALABRA_A_NUMERO.get(_sin_acentos(token).lower())
            or _romano(token)
        )
        if numero and numero not in capitulos:
            capitulos.append(numero)

    return {"articulos": articulos, "capitulos": capitulos}


# ---------------------------------------------------------------------------
# Orquestación de la búsqueda
# ---------------------------------------------------------------------------


async def recuperar(pregunta: str, top_k: int | None = None) -> tuple[list[dict], str]:
    """
    Devuelve (chunks_relevantes, estrategia_usada).

    Estrategia:
      1. Si la pregunta nombra artículos o capítulos -> filtrado por metadata.
         Es una coincidencia exacta, así que esos chunks van primero y completos.
      2. Siempre se corre además la búsqueda híbrida (vectorial + léxica), que
         aporta contexto relacionado. Si la pregunta era puramente semántica,
         este es el único camino.
      3. Se deduplica por chunk_uid conservando el orden de prioridad.
    """
    k = top_k or settings.top_k
    referencias = detectar_referencias(pregunta)

    resultados: list[dict] = []
    vistos: set[str] = set()
    usadas: list[str] = []

    # --- Paso 1: coincidencias exactas por metadata ---
    for numero, sufijo in referencias["articulos"]:
        for chunk in await db.buscar_por_articulo(numero, sufijo):
            if chunk["chunk_uid"] not in vistos:
                vistos.add(chunk["chunk_uid"])
                resultados.append(chunk)

    for numero in referencias["capitulos"]:
        # Un capítulo puede traer muchos artículos: se acota para no saturar
        # la ventana de contexto del LLM.
        for chunk in (await db.buscar_por_capitulo(numero))[: k * 2]:
            if chunk["chunk_uid"] not in vistos:
                vistos.add(chunk["chunk_uid"])
                resultados.append(chunk)

    if resultados:
        usadas.append("metadata")

    # --- Paso 2: búsqueda semántica + léxica ---
    vector = embed_texto(pregunta)
    for chunk in await db.buscar_hibrida(vector, pregunta, k):
        if chunk["similitud"] < settings.min_similarity and resultados:
            # Ya tenemos coincidencias exactas; no metemos ruido de baja
            # similitud que pueda desviar la respuesta del LLM.
            continue
        if chunk["chunk_uid"] not in vistos:
            vistos.add(chunk["chunk_uid"])
            resultados.append(chunk)
    usadas.append("hibrida")

    return resultados, "+".join(usadas)
