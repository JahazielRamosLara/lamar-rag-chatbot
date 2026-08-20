"""
Comprueba que AWS Bedrock responde, antes de gastar una ingesta completa.

Uso:
    python scripts_ingestion/probar_bedrock.py

Invoca los dos modelos que usa el proyecto —Titan para los embeddings y Claude
para redactar— con el mismo codigo que corre en produccion. Es la unica prueba
que vale: desde que AWS retiro la pagina de *Model access*, la consola ya no
muestra si un modelo esta habilitado, porque se habilita solo en la primera
llamada. Si esto pasa, la ingesta va a pasar.

No toca la base de datos: sirve para separar "Bedrock no responde" de "RDS no
conecta" cuando algo falla.
"""

from __future__ import annotations

import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

from backend.config import settings  # noqa: E402


# Errores tipicos de Bedrock traducidos a la accion que los resuelve.
PISTAS = {
    "AccessDeniedException": (
        "El usuario IAM no tiene permiso de bedrock:InvokeModel sobre este "
        "modelo.\n  Revisa la politica IAM de la Parte 1 de infra/aws_setup.md."
    ),
    # Claude entra por el endpoint Mantle, que es un servicio IAM distinto de
    # bedrock-runtime: dar bedrock:InvokeModel no alcanza para este.
    "bedrock-mantle": (
        "Falta el permiso bedrock-mantle:CreateInference.\n"
        "  Es una accion aparte de bedrock:InvokeModel; la politica completa\n"
        "  esta en la Parte 1 de infra/aws_setup.md."
    ),
    "ValidationException": (
        "El ID del modelo no es valido en esta region.\n"
        "  Revisa BEDROCK_EMBED_MODEL_ID / BEDROCK_LLM_MODEL_ID en tu .env."
    ),
    "ResourceNotFoundException": (
        "El modelo no existe en esta region.\n"
        "  Confirma que AWS_REGION=us-east-1 en tu .env."
    ),
    "UnrecognizedClientException": (
        "Las credenciales no son validas.\n"
        "  Revisa ~/.aws/credentials (o corre `aws configure`)."
    ),
    # Los dos clientes reportan lo mismo con palabras distintas: boto3 (Titan)
    # dice "Unable to locate credentials" y el SDK de Anthropic (Claude) dice
    # "Could not resolve AWS credentials from session".
    "NoCredentialsError": (
        "boto3 no encontro credenciales.\n"
        "  Crea ~/.aws/credentials como indica la Parte 1 de infra/aws_setup.md."
    ),
    "Could not resolve AWS credentials": (
        "El SDK de Anthropic no encontro credenciales de AWS.\n"
        "  Crea ~/.aws/credentials como indica la Parte 1 de infra/aws_setup.md."
    ),
    "ThrottlingException": (
        "Bedrock esta limitando las llamadas. Espera un momento y reintenta."
    ),
    # --- Errores de la API directa de Anthropic (LLM_PROVIDER=anthropic) ---
    "ANTHROPIC_API_KEY esta vacia": (
        "Pon tu key en ANTHROPIC_API_KEY dentro del .env.\n"
        "  Se genera en https://console.anthropic.com -> API Keys."
    ),
    "authentication_error": (
        "La ANTHROPIC_API_KEY no es valida.\n"
        "  Revisa que la copiaste completa, sin espacios ni comillas."
    ),
    "credit balance is too low": (
        "La cuenta de Anthropic no tiene saldo.\n"
        "  Agrega credito en console.anthropic.com -> Billing."
    ),
    # Distinto de AccessDenied: aqui los permisos IAM estan bien, pero la
    # cuenta no tiene habilitado ese modelo en concreto.
    "is not available for this account": (
        "Los permisos estan bien, pero la cuenta no tiene acceso a ESE modelo.\n"
        "  Abre Bedrock -> Model catalog, mira que modelos de Anthropic\n"
        "  aparecen disponibles y pon uno de esos en BEDROCK_LLM_MODEL_ID.\n"
        "  Los modelos mas nuevos suelen pedir un formulario de caso de uso."
    ),
}


def _explica(error: Exception) -> str:
    """Busca una pista accionable para el error recibido."""
    nombre = type(error).__name__
    texto = str(error)
    for clave, pista in PISTAS.items():
        if clave == nombre or clave in texto:
            return pista
    return "Sin pista especifica. El mensaje completo esta arriba."


def probar_embeddings() -> bool:
    from backend.bedrock import embed_texto

    print(f"[1/2] Titan  ({settings.bedrock_embed_model_id})")
    try:
        vector = embed_texto(
            "Artículo 4°.- Son alumnos de la Universidad quienes hayan "
            "cumplido con el proceso de inscripción."
        )
    except Exception as error:
        print(f"      FALLO: {type(error).__name__}: {error}")
        print(f"      -> {_explica(error)}")
        return False

    # La dimension tiene que cuadrar con la columna VECTOR(n) de la tabla.
    ok = len(vector) == settings.embed_dim
    print(f"      OK: vector de {len(vector)} dimensiones "
          f"(EMBED_DIM={settings.embed_dim})")
    if not ok:
        print("      AVISO: no coincide con EMBED_DIM; ajusta el .env antes "
              "de ingestar.")
    print(f"      muestra: [{vector[0]:.4f}, {vector[1]:.4f}, "
          f"{vector[2]:.4f}, ...]")
    return ok


def probar_llm() -> bool:
    from backend.bedrock import generar_respuesta

    print(f"\n[2/2] Claude ({settings.llm_model_id})")
    try:
        texto = generar_respuesta(
            system="Responde en una sola frase corta, en español.",
            mensajes=[{"role": "user", "content": "¿Qué es un reglamento escolar?"}],
        )
    except Exception as error:
        print(f"      FALLO: {type(error).__name__}: {error}")
        print(f"      -> {_explica(error)}")
        return False

    print(f"      OK: {texto[:160]}")
    return True


def main() -> int:
    print(f"Region: {settings.aws_region}\n")

    embeddings_ok = probar_embeddings()
    llm_ok = probar_llm()

    print("\n" + "-" * 62)
    if embeddings_ok and llm_ok:
        print("Bedrock listo. Sigue con la instancia de RDS (Parte 2).")
        return 0

    print("Bedrock todavia no responde. Corrige lo de arriba y reintenta;")
    print("no arranques la ingesta hasta que este paso pase.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
