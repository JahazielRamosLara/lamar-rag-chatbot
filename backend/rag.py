"""
Construcción del prompt RAG y generación de la respuesta final.

El prompt es deliberadamente estricto: un chatbot normativo que inventa un
artículo es peor que uno que dice "no lo encontré". Por eso el system prompt
prohíbe responder fuera del contexto recuperado y exige citar el artículo.
"""

from __future__ import annotations

import re
import time

from .bedrock import generar_respuesta
from .config import settings
from .retrieval import recuperar

SYSTEM_PROMPT = """\
Eres el asistente normativo de la Universidad LAMAR. Respondes exclusivamente \
sobre el Reglamento General de Alumnos de Licenciaturas y Posgrados (SEP R-0).

Reglas:

1. Responde ÚNICAMENTE con la información de los fragmentos del reglamento que \
te entrego en el contexto. No uses conocimiento externo ni supongas contenido.
2. Si el contexto no contiene la respuesta, dilo con claridad: "El reglamento \
no aborda ese punto en los artículos consultados" y sugiere cómo reformular la \
pregunta. Nunca inventes un número de artículo ni su contenido.
3. Cita siempre el artículo del que sale cada afirmación, con el formato \
(Artículo N°). Si aplica más de uno, cítalos todos.
4. Cuando el alumno pida un artículo específico ("dame el artículo 4"), \
transcribe su contenido de forma fiel y completa, respetando incisos y \
numerales, y añade después una explicación breve en lenguaje sencillo.
5. Cuando la pregunta sea abierta, responde primero de forma directa y después \
respalda con la cita. No transcribas artículos completos si no hacen falta.
6. Escribe en español, en tono claro y respetuoso, sin tecnicismos innecesarios.
7. Sé conciso: responde lo que se preguntó. No agregues secciones de relleno, \
resúmenes redundantes ni advertencias genéricas.
"""

RE_CITA_ARTICULO = re.compile(r"art[ií]culo\s+(\d{1,3})", re.IGNORECASE)


def _formatear_contexto(chunks: list[dict]) -> str:
    """
    Arma el bloque de contexto que ve el LLM.

    Cada fragmento lleva su jerarquía completa para que el modelo pueda citar
    con precisión y distinga un artículo de otro parecido.
    """
    bloques: list[str] = []
    for indice, chunk in enumerate(chunks, start=1):
        cabecera = [f"[FRAGMENTO {indice}]"]
        if chunk.get("titulo"):
            cabecera.append(f"Título: {chunk['titulo']}")
        if chunk.get("capitulo"):
            cabecera.append(f"Capítulo: {chunk['capitulo']}")
        if chunk.get("encabezado"):
            cabecera.append(f"Referencia: {chunk['encabezado']}")
        bloques.append("\n".join(cabecera) + "\n" + chunk["contenido"])
    return "\n\n---\n\n".join(bloques)


def _construir_mensajes(
    pregunta: str, contexto: str, historial: list[dict]
) -> list[dict]:
    """
    Historial acotado + turno actual con el contexto embebido.

    El contexto va dentro del turno del usuario (no en el system prompt) porque
    cambia en cada consulta: dejarlo fuera del prefijo estable evita invalidar
    la caché del system prompt en cada petición.
    """
    mensajes: list[dict] = []

    # Sólo los últimos turnos: el historial largo no aporta y encarece.
    for turno in historial[-6:]:
        if turno.get("content"):
            mensajes.append({"role": turno["role"], "content": turno["content"]})

    if contexto:
        contenido = (
            "Fragmentos del Reglamento General de Alumnos recuperados para esta "
            f"consulta:\n\n{contexto}\n\n"
            f"Pregunta del alumno: {pregunta}"
        )
    else:
        contenido = (
            "No se recuperó ningún fragmento del reglamento para esta consulta.\n\n"
            f"Pregunta del alumno: {pregunta}"
        )

    mensajes.append({"role": "user", "content": contenido})

    # La Messages API exige que el primer mensaje sea del usuario.
    while mensajes and mensajes[0]["role"] != "user":
        mensajes.pop(0)

    return mensajes


async def responder(
    pregunta: str,
    historial: list[dict] | None = None,
    top_k: int | None = None,
) -> dict:
    """Ciclo RAG completo: recuperar -> armar prompt -> generar -> citar."""
    inicio = time.perf_counter()

    chunks, estrategia = await recuperar(pregunta, top_k)
    contexto = _formatear_contexto(chunks)
    mensajes = _construir_mensajes(pregunta, contexto, historial or [])

    texto = generar_respuesta(SYSTEM_PROMPT, mensajes)

    # Artículos que el modelo realmente citó: sirven para resaltarlos en la UI.
    citados = sorted({int(n) for n in RE_CITA_ARTICULO.findall(texto)})

    fuentes = [
        {
            "chunk_uid": c["chunk_uid"],
            "encabezado": c["encabezado"] or c["chunk_uid"],
            "articulo": c["articulo"],
            "capitulo": c["capitulo"],
            "titulo": c["titulo"],
            "similitud": c["similitud"],
            "extracto": (
                c["contenido"][:300] + "…" if len(c["contenido"]) > 300
                else c["contenido"]
            ),
        }
        for c in chunks
    ]

    return {
        "respuesta": texto,
        "fuentes": fuentes,
        "estrategia": estrategia,
        "articulos_citados": citados,
        "ms": int((time.perf_counter() - inicio) * 1000),
    }
