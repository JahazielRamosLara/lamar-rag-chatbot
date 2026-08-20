-- =====================================================================
--  Esquema de la base de datos vectorial
--  AWS RDS PostgreSQL + extensión pgvector
--
--  Ejecutar conectado a la base `reglamento`:
--      psql "host=... dbname=reglamento user=postgres sslmode=require" -f infra/schema.sql
--
--  El script de ingesta (scripts_ingestion/ingest.py) también lo ejecuta
--  automáticamente, así que basta con correr la ingesta.
-- =====================================================================

-- ---------------------------------------------------------------------
-- 1. Extensiones
-- ---------------------------------------------------------------------
-- pgvector añade el tipo VECTOR y los operadores de distancia (<=>, <->, <#>).
CREATE EXTENSION IF NOT EXISTS vector;

-- unaccent permite que "articulo" encuentre "artículo" en la búsqueda léxica.
CREATE EXTENSION IF NOT EXISTS unaccent;


-- ---------------------------------------------------------------------
-- 2. Tabla de chunks vectorizados
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS reglamento_chunks (
    id              BIGSERIAL PRIMARY KEY,

    -- Identidad del fragmento
    doc_id          TEXT        NOT NULL DEFAULT 'SEP-R-0',
    chunk_uid       TEXT        NOT NULL,   -- 'art-4', 'art-14-f2', 'art-16-bis'

    -- Jerarquía normativa (el corazón del chunking inteligente)
    titulo          TEXT,                   -- 'TÍTULO PRIMERO — Disposiciones generales'
    titulo_num      INTEGER,
    capitulo        TEXT,                   -- 'CAPÍTULO III — De las evaluaciones'
    capitulo_num    INTEGER,
    articulo        INTEGER,                -- 4
    articulo_sufijo TEXT,                   -- 'BIS', 'TER'
    fragmento       INTEGER     NOT NULL DEFAULT 0,
    total_fragmentos INTEGER    NOT NULL DEFAULT 1,
    encabezado      TEXT        NOT NULL DEFAULT '',   -- 'Artículo 4°'
    tipo            TEXT        NOT NULL DEFAULT 'articulo',

    -- Contenido
    contenido       TEXT        NOT NULL,   -- texto literal del artículo
    texto_embebido  TEXT        NOT NULL,   -- contenido + jerarquía (lo que se vectorizó)

    -- Metadata estructurada para el filtrado híbrido
    metadata        JSONB       NOT NULL DEFAULT '{}'::jsonb,

    -- Vector de Amazon Titan Text Embeddings V2 (1024 dimensiones)
    embedding       VECTOR(1024) NOT NULL,

    creado_en       TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Reingestar el mismo documento actualiza en lugar de duplicar
    CONSTRAINT reglamento_chunks_uid_unico UNIQUE (doc_id, chunk_uid)
);


-- ---------------------------------------------------------------------
-- 3. Índice vectorial
-- ---------------------------------------------------------------------
-- Elegimos HNSW sobre IVFFlat por dos razones:
--   * IVFFlat necesita datos ya cargados para calcular sus listas y hay que
--     reconstruirlo si el corpus cambia; HNSW se puede crear con la tabla vacía.
--   * Con ~200 artículos el recall de HNSW es prácticamente 100% y la latencia
--     está en milisegundos.
--
-- Métrica: distancia coseno (vector_cosine_ops / operador <=>).
--   * Titan V2 con normalize=true entrega vectores de norma 1. En ese caso
--     coseno y L2 ordenan igual, pero el coseno da un valor acotado en [0, 2]
--     que se convierte directo a similitud: similitud = 1 - distancia.
--   * Inner product (<#>) sería equivalente, pero devuelve el negativo del
--     producto punto y complica interpretar el umbral en el backend.
CREATE INDEX IF NOT EXISTS idx_reglamento_embedding_hnsw
    ON reglamento_chunks
    USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);


-- ---------------------------------------------------------------------
-- 4. Índices de metadata (para las consultas explícitas: "dame el artículo 4")
-- ---------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_reglamento_articulo
    ON reglamento_chunks (articulo);

CREATE INDEX IF NOT EXISTS idx_reglamento_capitulo
    ON reglamento_chunks (capitulo_num);

CREATE INDEX IF NOT EXISTS idx_reglamento_metadata
    ON reglamento_chunks
    USING gin (metadata jsonb_path_ops);


-- ---------------------------------------------------------------------
-- 5. Índice de texto completo en español (complementa al vectorial)
-- ---------------------------------------------------------------------
-- Cubre el caso donde el alumno usa un término literal del reglamento
-- ("extraordinario", "baja definitiva") que la búsqueda semántica podría
-- diluir entre artículos parecidos.
CREATE INDEX IF NOT EXISTS idx_reglamento_fts
    ON reglamento_chunks
    USING gin (to_tsvector('spanish', contenido));


-- ---------------------------------------------------------------------
-- 6. Vista de apoyo para las capturas de evidencia
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW v_reglamento_resumen AS
SELECT
    id,
    chunk_uid,
    encabezado,
    capitulo,
    articulo,
    length(contenido)                AS caracteres,
    vector_dims(embedding)           AS dimensiones,
    left(embedding::text, 60) || '…' AS vector_muestra,
    metadata
FROM reglamento_chunks
ORDER BY articulo NULLS FIRST, fragmento;
