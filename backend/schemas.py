"""Contratos de entrada y salida de la API."""

from __future__ import annotations

from pydantic import BaseModel, Field


class Mensaje(BaseModel):
    """Un turno del historial de conversación."""

    role: str = Field(..., pattern="^(user|assistant)$")
    content: str


class ChatRequest(BaseModel):
    pregunta: str = Field(..., min_length=1, max_length=1000)
    historial: list[Mensaje] = Field(default_factory=list, max_length=10)
    top_k: int | None = Field(default=None, ge=1, le=20)


class Fuente(BaseModel):
    """Un chunk del reglamento citado como respaldo de la respuesta."""

    chunk_uid: str
    encabezado: str
    articulo: int | None = None
    capitulo: str | None = None
    titulo: str | None = None
    similitud: float
    extracto: str


class ChatResponse(BaseModel):
    respuesta: str
    fuentes: list[Fuente]
    estrategia: str          # 'metadata' | 'hibrida' | 'metadata+hibrida'
    articulos_citados: list[int]
    ms: int


class SaludResponse(BaseModel):
    estado: str
    base_datos: str
    chunks: int
    modelo_embeddings: str
    modelo_llm: str
    dimension: int
