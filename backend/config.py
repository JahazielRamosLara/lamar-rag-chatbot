"""
Configuracion central del proyecto.

Todo se lee del archivo .env (ver .env.example). Un solo objeto `settings` lo
comparten el backend y los scripts de ingesta, para que ambos usen exactamente
el mismo modelo de embeddings: si el reglamento se incrusta con Titan V2 y la
consulta se incrusta con otro modelo, la distancia coseno deja de tener
sentido y el RAG devuelve basura.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

RAIZ = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=RAIZ / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---------- AWS ----------
    aws_region: str = "us-east-1"

    # ---------- Bedrock ----------
    bedrock_embed_model_id: str = "amazon.titan-embed-text-v2:0"
    embed_dim: int = 1024
    bedrock_llm_model_id: str = "anthropic.claude-sonnet-5"
    llm_effort: str = "low"
    llm_max_tokens: int = 1500

    # ---------- PostgreSQL (AWS RDS) ----------
    pghost: str = "localhost"
    pgport: int = 5432
    pgdatabase: str = "reglamento"
    pguser: str = "postgres"
    pgpassword: str = ""
    pgsslmode: str = "require"
    db_table: str = "reglamento_chunks"

    # ---------- RAG ----------
    top_k: int = 6
    min_similarity: float = 0.25

    # ---------- Servidor ----------
    api_host: str = "0.0.0.0"
    api_port: int = 8000

    @property
    def dsn(self) -> str:
        """Cadena de conexion para psycopg (scripts de ingesta)."""
        return (
            f"postgresql://{self.pguser}:{self.pgpassword}"
            f"@{self.pghost}:{self.pgport}/{self.pgdatabase}"
            f"?sslmode={self.pgsslmode}"
        )

    @property
    def asyncpg_kwargs(self) -> dict:
        """
        asyncpg no entiende el parametro `sslmode` de libpq: usa `ssl`.
        RDS exige TLS, asi que traducimos aqui en vez de en cada llamada.
        """
        return {
            "host": self.pghost,
            "port": self.pgport,
            "database": self.pgdatabase,
            "user": self.pguser,
            "password": self.pgpassword,
            "ssl": self.pgsslmode not in ("disable", "allow"),
        }


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
