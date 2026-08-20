"""
Cliente de AWS Bedrock: embeddings (Amazon Titan) + LLM (Anthropic Claude).

Se usa desde dos lados y por eso vive en un solo archivo:
  * scripts_ingestion/ingest.py  -> incrusta cada chunk del reglamento
  * backend/rag.py               -> incrusta la consulta y redacta la respuesta

Regla de oro del RAG: el reglamento y la pregunta del alumno DEBEN pasar por el
mismo modelo de embeddings. Por eso ambos lados llaman a `embed_texto`.
"""

from __future__ import annotations

import json
import logging
import threading

import boto3
from botocore.config import Config

from .config import settings

log = logging.getLogger(__name__)

# Reintentos con backoff: Bedrock devuelve ThrottlingException seguido cuando se
# incrusta el reglamento completo de golpe.
_BOTO_CONFIG = Config(
    region_name=settings.aws_region,
    retries={"max_attempts": 8, "mode": "adaptive"},
    read_timeout=120,
    connect_timeout=15,
)

_lock = threading.Lock()
_runtime = None
_claude = None

# Si el modelo elegido no acepta `output_config.effort` (p. ej. Haiku 4.5),
# Bedrock responde 400. Lo detectamos una vez y dejamos de mandarlo.
_soporta_effort = True


def _get_runtime():
    """Cliente boto3 de bedrock-runtime, usado para los embeddings de Titan."""
    global _runtime
    if _runtime is None:
        with _lock:
            if _runtime is None:
                _runtime = boto3.client("bedrock-runtime", config=_BOTO_CONFIG)
    return _runtime


def _get_claude():
    """
    Cliente de Claude, segun LLM_PROVIDER.

    Los dos clientes exponen la misma interfaz `messages.create`, asi que el
    resto del proyecto no se entera de cual esta activo; lo unico que cambia es
    el nombre del modelo, y de eso se encarga `settings.llm_model_id`.
    """
    global _claude
    if _claude is not None:
        return _claude

    with _lock:
        if _claude is None:
            import anthropic

            if settings.llm_provider == "anthropic":
                if not settings.anthropic_api_key:
                    raise RuntimeError(
                        "LLM_PROVIDER=anthropic pero ANTHROPIC_API_KEY esta vacia. "
                        "Genera una key en https://console.anthropic.com y ponla "
                        "en el .env."
                    )
                _claude = anthropic.Anthropic(api_key=settings.anthropic_api_key)
                log.info("Cliente de Anthropic: API directa")
            else:
                # El SDK expone el cliente moderno (`AnthropicBedrockMantle`) y
                # el anterior (`AnthropicBedrock`); tomamos el que exista en la
                # version instalada para no amarrar el proyecto a una version
                # exacta del paquete.
                for nombre in ("AnthropicBedrockMantle", "AnthropicBedrock"):
                    cls = getattr(anthropic, nombre, None)
                    if cls is not None:
                        _claude = cls(aws_region=settings.aws_region)
                        log.info("Cliente de Anthropic: Bedrock (%s)", nombre)
                        break
                else:
                    raise RuntimeError(
                        "El paquete `anthropic` instalado no trae cliente de "
                        "Bedrock. Instala con: pip install 'anthropic[bedrock]'"
                    )
    return _claude


# ---------------------------------------------------------------------------
# Embeddings — Amazon Titan Text Embeddings V2
# ---------------------------------------------------------------------------


def embed_texto(texto: str) -> list[float]:
    """
    Convierte un texto en un vector de `settings.embed_dim` dimensiones.

    `normalize: true` deja el vector con norma 1, que es lo que necesita la
    distancia coseno (`<=>`) de pgvector para comportarse bien.
    """
    if not texto or not texto.strip():
        raise ValueError("No se puede incrustar un texto vacío.")

    cuerpo = json.dumps(
        {
            "inputText": texto.strip(),
            "dimensions": settings.embed_dim,
            "normalize": True,
        }
    )

    respuesta = _get_runtime().invoke_model(
        modelId=settings.bedrock_embed_model_id,
        contentType="application/json",
        accept="application/json",
        body=cuerpo,
    )
    datos = json.loads(respuesta["body"].read())
    vector = datos["embedding"]

    if len(vector) != settings.embed_dim:
        raise ValueError(
            f"Titan devolvió {len(vector)} dimensiones pero EMBED_DIM={settings.embed_dim}. "
            "Ajusta EMBED_DIM en .env y recrea la columna VECTOR(n) de la tabla."
        )
    return vector


def embed_lote(textos: list[str], workers: int = 4) -> list[list[float]]:
    """
    Incrusta varios textos en paralelo.

    Titan procesa un texto por llamada, asi que la unica forma de acelerar la
    ingesta es paralelizar. 4 hilos es un punto seguro frente al throttling de
    una cuenta nueva; se puede subir si tu cuota lo permite.
    """
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(embed_texto, textos))


# ---------------------------------------------------------------------------
# LLM — Claude en Bedrock
# ---------------------------------------------------------------------------


def generar_respuesta(system: str, mensajes: list[dict]) -> str:
    """
    Manda el prompt RAG a Claude y devuelve el texto de la respuesta.

    `mensajes` usa el formato de la Messages API:
        [{"role": "user", "content": "..."}]
    """
    global _soporta_effort

    cliente = _get_claude()

    parametros: dict = {
        "model": settings.llm_model_id,
        "max_tokens": settings.llm_max_tokens,
        "system": system,
        "messages": mensajes,
    }
    if _soporta_effort and settings.llm_effort:
        # Un chatbot de consulta normativa no necesita razonamiento profundo:
        # el contexto ya viene recuperado. `effort` bajo reduce latencia y costo.
        parametros["output_config"] = {"effort": settings.llm_effort}

    try:
        respuesta = cliente.messages.create(**parametros)
    except Exception as error:
        # `output_config` puede fallar por dos motivos distintos, y hay que
        # cubrir los dos o el chat se cae en la primera consulta:
        #   * TypeError  -> el SDK instalado es anterior al parametro y ni
        #                   siquiera lo reconoce (no llega a salir a la red).
        #   * HTTP 400   -> el SDK lo manda pero el modelo no lo acepta.
        rechazo_de_effort = (
            _soporta_effort
            and "output_config" in parametros
            and (isinstance(error, TypeError) or "400" in str(error))
        )
        if rechazo_de_effort:
            log.warning(
                "output_config.effort no se pudo usar con %s (%s); se desactiva.",
                settings.llm_model_id,
                type(error).__name__,
            )
            _soporta_effort = False
            parametros.pop("output_config")
            respuesta = cliente.messages.create(**parametros)
        else:
            raise

    if getattr(respuesta, "stop_reason", None) == "refusal":
        return (
            "No puedo responder esa consulta. Reformúlala en términos del "
            "Reglamento General de Alumnos."
        )

    partes = [b.text for b in respuesta.content if getattr(b, "type", None) == "text"]
    return "\n".join(partes).strip()
