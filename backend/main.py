"""
API FastAPI del chatbot del Reglamento General de Alumnos (SEP R-0).

Además de exponer /api/chat, este servidor sirve el frontend estático, de modo
que toda la práctica corre con un solo proceso:

    uvicorn backend.main:app --reload

    http://localhost:8000        -> interfaz de chat
    http://localhost:8000/docs   -> documentación interactiva (Swagger)
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import db
from .config import settings
from .rag import responder
from .retrieval import detectar_referencias
from .schemas import ChatRequest, ChatResponse, SaludResponse

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("reglamento")

FRONTEND = Path(__file__).resolve().parent.parent / "frontend"


# Guarda el motivo real por el que falló la conexión al arrancar, para poder
# mostrarlo en /api/salud en vez del genérico "el pool no está iniciado".
_error_arranque: str | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """El pool de conexiones se abre al arrancar y se cierra al apagar."""
    global _error_arranque
    try:
        await db.iniciar_pool()
        _error_arranque = None
    except Exception as error:
        # No abortamos el arranque: así /api/salud puede explicar qué falló
        # en vez de que el proceso muera sin mensaje útil.
        _error_arranque = f"{type(error).__name__}: {error}"
        log.error("No se pudo conectar a PostgreSQL: %s", error)
    yield
    await db.cerrar_pool()


def _pista(mensaje: str) -> str:
    """Traduce los fallos de conexión más comunes a una acción concreta."""
    texto = mensaje.lower()
    if "getaddrinfo" in texto or "name or service" in texto:
        return " → revisa PGHOST en tu .env (endpoint de RDS)"
    if "timeout" in texto or "timed out" in texto:
        return " → abre el puerto 5432 a tu IP en el security group de RDS"
    if "password" in texto or "authentication" in texto:
        return " → revisa PGUSER / PGPASSWORD"
    if "does not exist" in texto:
        return " → la base o la tabla no existen; corre scripts_ingestion/ingest.py"
    return ""


app = FastAPI(
    title="Chatbot Reglamento SEP R-0 — Universidad LAMAR",
    description=(
        "Arquitectura RAG sobre AWS: PostgreSQL + pgvector para la búsqueda "
        "vectorial y AWS Bedrock para embeddings y generación."
    ),
    version="1.0.0",
    lifespan=lifespan,
)

# El frontend se sirve desde el mismo origen, pero se deja CORS abierto por si
# el equipo prefiere levantarlo aparte durante el desarrollo.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.post("/api/chat", response_model=ChatResponse, tags=["chat"])
async def chat(peticion: ChatRequest) -> ChatResponse:
    """
    Endpoint principal del RAG.

    1. Detecta si la pregunta cita un artículo o capítulo concreto.
    2. Recupera los fragmentos relevantes (metadata + vectorial + léxica).
    3. Le pasa esos fragmentos a Claude para que redacte la respuesta citada.
    """
    try:
        resultado = await responder(
            pregunta=peticion.pregunta,
            historial=[m.model_dump() for m in peticion.historial],
            top_k=peticion.top_k,
        )
    except RuntimeError as error:
        # Pool sin iniciar: casi siempre es la base de datos inalcanzable.
        raise HTTPException(status_code=503, detail=str(error)) from error
    except Exception as error:
        log.exception("Fallo procesando la consulta")
        raise HTTPException(status_code=500, detail=str(error)) from error

    return ChatResponse(**resultado)


@app.get("/api/salud", response_model=SaludResponse, tags=["diagnóstico"])
async def salud() -> SaludResponse:
    """Comprueba de un vistazo que la BD y la configuración estén en orden."""
    try:
        stats = await db.estadisticas()
        estado_bd = "conectada"
    except Exception as error:
        stats = {"chunks": 0, "dimension": 0}
        # Si el pool nunca se abrió, el error útil es el del arranque.
        causa = _error_arranque or f"{type(error).__name__}: {error}"
        estado_bd = f"error: {causa}{_pista(causa)}"

    return SaludResponse(
        estado="ok" if estado_bd == "conectada" and stats["chunks"] else "degradado",
        base_datos=estado_bd,
        chunks=stats["chunks"],
        modelo_embeddings=settings.bedrock_embed_model_id,
        modelo_llm=settings.llm_model_id,
        dimension=stats["dimension"],
    )


@app.get("/api/articulo/{numero}", tags=["consulta directa"])
async def obtener_articulo(numero: int) -> dict:
    """
    Devuelve un artículo tal cual, sin pasar por el LLM.

    Sirve para comprobar en el video que el filtrado por metadata funciona con
    independencia de la generación.
    """
    chunks = await db.buscar_por_articulo(numero)
    if not chunks:
        raise HTTPException(status_code=404, detail=f"No existe el artículo {numero}")
    return {"articulo": numero, "fragmentos": chunks}


@app.get("/api/intencion", tags=["diagnóstico"])
async def intencion(pregunta: str) -> dict:
    """Muestra qué referencias explícitas detecta el router en una pregunta."""
    return {"pregunta": pregunta, **detectar_referencias(pregunta)}


# ---------------------------------------------------------------------------
# Frontend estático
# ---------------------------------------------------------------------------

if FRONTEND.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND)), name="static")

    @app.get("/", include_in_schema=False)
    async def raiz() -> FileResponse:
        return FileResponse(str(FRONTEND / "index.html"))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("backend.main:app", host=settings.api_host, port=settings.api_port,
                reload=True)
