"""
Averigua que modelo de Anthropic acepta esta cuenta de Bedrock.

Uso:
    python scripts_ingestion/probar_modelos.py

El catalogo de la consola lista los modelos que *existen* en la region, no los
que tu cuenta puede *invocar*: son dos cosas distintas y por eso un modelo
puede aparecer en el catalogo y aun asi devolver "is not available for this
account". La unica forma de saberlo es intentar.

Este script manda una peticion minima (max_tokens=16) a cada candidato y
reporta cual contesta. Cuesta fracciones de centavo. Cuando encuentres uno
que diga OK, ponlo en BEDROCK_LLM_MODEL_ID dentro de tu .env.

Tambien intenta enumerar el catalogo. Si tu politica IAM incluye
bedrock:ListFoundationModels veras la lista real de IDs; si no, se salta ese
paso sin ruido.
"""

from __future__ import annotations

import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

from backend.config import settings  # noqa: E402

# Candidatos en el formato del endpoint Mantle: 'anthropic.' + id del modelo.
# Ordenados del mas capaz al mas accesible: los modelos chicos y antiguos
# suelen estar disponibles en cuentas donde los nuevos todavia no.
CANDIDATOS = [
    "anthropic.claude-opus-5",
    "anthropic.claude-sonnet-5",
    "anthropic.claude-opus-4-8",
    "anthropic.claude-opus-4-7",
    "anthropic.claude-sonnet-4-6",
    "anthropic.claude-opus-4-6",
    "anthropic.claude-haiku-4-5",
]


def _resumen_error(error: Exception) -> str:
    """Reduce el error a una etiqueta corta para que la tabla se lea."""
    texto = str(error)
    if "is not available for this account" in texto:
        return "no habilitado para la cuenta"
    if "not authorized" in texto or "AccessDenied" in texto:
        return "sin permiso IAM"
    if "ValidationException" in texto or "not found" in texto.lower():
        return "ID invalido en esta region"
    if "ThrottlingException" in texto:
        return "throttling (reintenta)"
    return f"{type(error).__name__}: {texto[:80]}"


def enumerar_catalogo() -> None:
    """Lista los IDs reales del catalogo, si la politica IAM lo permite."""
    import boto3
    from botocore.config import Config

    print("Catalogo de Anthropic en la region")
    print("-" * 62)
    try:
        cliente = boto3.client(
            "bedrock", config=Config(region_name=settings.aws_region)
        )
        respuesta = cliente.list_foundation_models(byProvider="anthropic")
    except Exception as error:
        if "ListFoundationModels" in str(error):
            print("  (sin permiso bedrock:ListFoundationModels; se omite)")
        else:
            print(f"  (no se pudo consultar: {type(error).__name__})")
        print()
        return

    for modelo in respuesta.get("modelSummaries", []):
        print(f"  {modelo['modelId']}")
    print()


def probar_candidatos() -> str | None:
    """Invoca cada candidato y devuelve el primero que responda."""
    from backend.bedrock import _get_claude

    print("Prueba de invocacion")
    print("-" * 62)

    cliente = _get_claude()
    ganador: str | None = None

    for modelo in CANDIDATOS:
        try:
            cliente.messages.create(
                model=modelo,
                max_tokens=16,
                messages=[{"role": "user", "content": "Di: hola"}],
            )
        except Exception as error:
            print(f"  [  ] {modelo:<34} {_resumen_error(error)}")
            continue

        print(f"  [OK] {modelo:<34} responde")
        if ganador is None:
            ganador = modelo

    return ganador


def main() -> int:
    print(f"Region: {settings.aws_region}")
    print(f"Modelo actual en .env: {settings.llm_model_id}\n")

    enumerar_catalogo()
    ganador = probar_candidatos()

    print("\n" + "-" * 62)
    if ganador:
        print("Modelo utilizable encontrado. Pon esta linea en tu .env:\n")
        print(f"    BEDROCK_LLM_MODEL_ID={ganador}\n")
        print("Luego confirma con: python scripts_ingestion/probar_bedrock.py")
        return 0

    print("Ningun candidato respondio.")
    print("Si todos dicen 'no habilitado para la cuenta', falta el formulario")
    print("de caso de uso de Anthropic (Bedrock -> Catalogo de modelos ->")
    print("'Submit use case details'). Puede tardar en aplicarse.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
