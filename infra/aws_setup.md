# Despliegue en AWS — RDS PostgreSQL + pgvector + Bedrock

Guía paso a paso de la infraestructura de la práctica. Cada sección indica
**qué captura de pantalla tomar** para el documento de evidencias en PDF.

> **Costos.** Una instancia `db.t4g.micro` entra en la capa gratuita de RDS
> durante los primeros 12 meses. Bedrock se cobra por uso: la ingesta completa
> del reglamento con Titan V2 cuesta centavos. **Al terminar la práctica,
> elimina la instancia de RDS** — es lo único con costo por hora.

---

## Parte 1 — AWS Bedrock (acceso a los modelos)

> **La página *Model access* fue retirada.** Antes había que marcar cada modelo
> a mano y esperar aprobación. Hoy los modelos serverless **se habilitan solos
> la primera vez que se invocan** en tu cuenta, en cualquier región comercial.
> Si abres *Acceso a modelos* en la consola verás únicamente el aviso
> "Model access page has been retired": es lo esperado, no un error.

Lo único que sigue haciendo falta:

1. Usa **`us-east-1` (N. Virginia)** en todo el proyecto. Si el selector de
   región no responde, es porque estás en un servicio global (IAM, Facturación);
   entra directo por URL añadiendo `?region=us-east-1`.
2. **Modelos de Anthropic:** la primera vez que los uses, AWS puede pedirte un
   formulario corto de caso de uso. Se dispara al abrir el modelo en
   **Model catalog → Claude → Open in Playground** y mandar un mensaje de
   prueba. Contesta algo como *"proyecto académico: chatbot de consulta sobre
   un reglamento universitario"*. Una vez aceptado queda habilitado para toda
   la cuenta.
3. **Titan Text Embeddings V2** no pide nada: se habilita en la primera llamada.

> 📸 **Captura 1:** el *Model catalog* mostrando Claude y Titan disponibles, o
> el playground de Claude respondiendo a un mensaje. Sustituye a la captura de
> "Access granted", que ya no existe.

### Credenciales

`boto3` lee las credenciales de `~/.aws/credentials`. Puedes crearlas con el
AWS CLI (`aws configure`) o, si prefieres no instalarlo, escribiendo los dos
archivos a mano — es lo que boto3 termina leyendo de todos modos:

`C:\Users\TU-USUARIO\.aws\credentials`

```ini
[default]
aws_access_key_id = AKIA...
aws_secret_access_key = ...
```

`C:\Users\TU-USUARIO\.aws\config`

```ini
[default]
region = us-east-1
```

Las llaves se generan en **IAM → Users → tu usuario → Security credentials →
Create access key → Command Line Interface (CLI)**. El secreto se muestra una
sola vez: cópialo en ese momento.

> ⚠️ Nunca subas estas llaves al repositorio. Viven fuera del proyecto, en tu
> carpeta de usuario, y no las toca `.gitignore` porque ni siquiera están aquí.

### Permisos IAM

El usuario necesita, como mínimo, esta política. Que la página de *Model access*
haya desaparecido no significa que cualquiera pueda invocar los modelos: el
control de acceso ahora se hace enteramente por IAM y SCPs.

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": ["bedrock:InvokeModel"],
    "Resource": [
      "arn:aws:bedrock:*::foundation-model/amazon.titan-embed-text-v2*",
      "arn:aws:bedrock:*::foundation-model/anthropic.claude*"
    ]
  }]
}
```

> 📸 **Captura 2:** la política IAM adjunta al usuario.

### Verificar que Bedrock responde

En lugar de confiar en lo que muestre la consola, invoca los dos modelos desde
el proyecto. Es la única prueba que importa, porque es exactamente lo que hará
la ingesta:

```bash
python scripts_ingestion/probar_bedrock.py
```

Si Titan devuelve un vector de 1024 dimensiones y Claude contesta, Bedrock está
listo y puedes pasar a RDS.

---

## Parte 2 — AWS RDS PostgreSQL

### 2.1 Crear la instancia

1. Consola de AWS → **RDS** → **Create database**.
2. Configura así:

| Campo | Valor |
|---|---|
| Método de creación | **Standard create** |
| Motor | **PostgreSQL** |
| Versión | **16.x** (cualquiera ≥ 15.4 trae pgvector) |
| Plantilla | **Free tier** |
| DB instance identifier | `reglamento-lamar` |
| Master username | `postgres` |
| Master password | *(guárdala; va en el `.env`)* |
| Instance class | `db.t4g.micro` |
| Storage | 20 GB gp3 |
| **Public access** | **Yes** ⚠️ |
| VPC security group | *Create new* → `reglamento-sg` |
| Initial database name | `reglamento` |

⚠️ **`Public access: Yes` es indispensable** para conectarte desde tu laptop.
Es una configuración de práctica académica, no de producción.

⚠️ Si no llenas **Initial database name** en *Additional configuration*, RDS no
crea la base y tendrás que crearla a mano después.

3. Crea la base y espera a que el estado sea **Available** (5–10 minutos).

> 📸 **Captura 3:** la instancia RDS en estado *Available*, mostrando el
> endpoint y la versión del motor.

### 2.2 Abrir el security group

Sin este paso la conexión se queda colgada hasta agotar el timeout.

1. En la página de la instancia → pestaña **Connectivity & security** →
   haz clic en el security group (`reglamento-sg`).
2. Pestaña **Inbound rules** → **Edit inbound rules** → **Add rule**:

| Type | Port | Source |
|---|---|---|
| PostgreSQL | 5432 | **My IP** |

3. Guarda los cambios.

> 💡 Si trabajan en equipo, cada integrante debe añadir su propia IP, y la IP
> cambia al moverse de red (casa → universidad). Si la conexión deja de
> funcionar de un día para otro, revisa esto primero.

> 📸 **Captura 4:** las reglas de entrada del security group con el puerto 5432
> abierto.

### 2.3 Anotar el endpoint

En la pestaña **Connectivity & security** copia el **Endpoint**:

```
reglamento-lamar.abcd1234efgh.us-east-1.rds.amazonaws.com
```

Ese valor va en `PGHOST` dentro de tu `.env`.

---

## Parte 3 — Activar pgvector

pgvector viene incluido en RDS PostgreSQL ≥ 15.4, pero hay que **activar la
extensión dentro de la base de datos**. No requiere grupo de parámetros.

### Conectarse

```bash
psql "host=reglamento-lamar.xxxx.us-east-1.rds.amazonaws.com \
      port=5432 dbname=reglamento user=postgres sslmode=require"
```

Si no tienes `psql`, puedes usar pgAdmin, DBeaver o el propio script de ingesta
(que aplica el esquema automáticamente).

### Activar y comprobar

```sql
-- Comprobar que pgvector está disponible en esta versión del motor
SELECT name, default_version
FROM pg_available_extensions
WHERE name IN ('vector', 'unaccent');

-- Activarla en esta base de datos
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS unaccent;

-- Verificar la versión instalada
SELECT extname, extversion FROM pg_extension WHERE extname = 'vector';
```

Debe devolver `vector | 0.7.x` o superior. La versión importa: HNSW requiere
pgvector ≥ 0.5.0.

> 📸 **Captura 5:** la salida de `SELECT extname, extversion ...` mostrando
> pgvector activo.

### Si `vector` no aparece en `pg_available_extensions`

La versión del motor es anterior a 15.4. Ve a **Modify** en la instancia RDS y
actualízala a PostgreSQL 16.

---

## Parte 4 — Crear el esquema

El script de ingesta aplica `infra/schema.sql` automáticamente, pero también
puedes ejecutarlo a mano:

```bash
psql "host=... dbname=reglamento user=postgres sslmode=require" -f infra/schema.sql
```

### Qué crea

| Objeto | Tipo | Propósito |
|---|---|---|
| `reglamento_chunks` | Tabla | Contenido + metadata jerárquica + `VECTOR(1024)` |
| `idx_reglamento_embedding_hnsw` | HNSW | Búsqueda por similitud coseno (`<=>`) |
| `idx_reglamento_articulo` | B-tree | Consultas explícitas: "dame el artículo 4" |
| `idx_reglamento_capitulo` | B-tree | Filtrado por capítulo |
| `idx_reglamento_metadata` | GIN | Consultas sobre el `JSONB` |
| `idx_reglamento_fts` | GIN | Búsqueda léxica en español |
| `v_reglamento_resumen` | Vista | Consulta lista para las capturas de evidencia |

### Comprobar la estructura

```sql
\d reglamento_chunks
\di reglamento_chunks*
```

> 📸 **Captura 6:** la salida de `\d reglamento_chunks`, donde se ve la columna
> `embedding | vector(1024)`.

---

## Parte 5 — Ingesta y verificación

```bash
python scripts_ingestion/ingest.py --pdf data/raw/reglamento_sep_r0.pdf --recrear
```

### Consultas de verificación

```sql
-- 1. Cuántos chunks y artículos se cargaron
SELECT count(*)                  AS chunks,
       count(DISTINCT articulo)  AS articulos,
       count(DISTINCT capitulo_num) AS capitulos
FROM reglamento_chunks;

-- 2. La tabla con su columna vectorial y su metadata
SELECT * FROM v_reglamento_resumen LIMIT 10;

-- 3. Un artículo concreto con su metadata JSONB
SELECT encabezado, capitulo, metadata
FROM reglamento_chunks
WHERE articulo = 4;

-- 4. Confirmar la dimensión del vector
SELECT vector_dims(embedding) AS dimensiones
FROM reglamento_chunks LIMIT 1;
```

> 📸 **Captura 7:** la tabla mostrando la columna vectorial y los metadatos
> almacenados (consulta 2).
> 📸 **Captura 8:** la metadata JSONB de un artículo (consulta 3).

### Probar la búsqueda vectorial directamente en SQL

Útil para demostrar en el video que la similitud coseno funciona a nivel de
base de datos, sin pasar por el backend:

```sql
-- Los 5 artículos más parecidos al artículo 4
SELECT encabezado,
       round((1 - (embedding <=> (
           SELECT embedding FROM reglamento_chunks WHERE articulo = 4 LIMIT 1
       )))::numeric, 4) AS similitud
FROM reglamento_chunks
ORDER BY embedding <=> (
    SELECT embedding FROM reglamento_chunks WHERE articulo = 4 LIMIT 1
)
LIMIT 5;
```

### Confirmar que se usa el índice HNSW

```sql
EXPLAIN ANALYZE
SELECT chunk_uid
FROM reglamento_chunks
ORDER BY embedding <=> (SELECT embedding FROM reglamento_chunks LIMIT 1)
LIMIT 5;
```

Debe aparecer `Index Scan using idx_reglamento_embedding_hnsw`. Si aparece
`Seq Scan`, es porque hay muy pocas filas y el planificador decide que el
recorrido secuencial es más barato — con el reglamento completo usará el índice.

> 📸 **Captura 9:** el `EXPLAIN ANALYZE` mostrando el uso del índice HNSW.

---

## Parte 6 — Archivo `.env` final

```ini
AWS_REGION=us-east-1

BEDROCK_EMBED_MODEL_ID=amazon.titan-embed-text-v2:0
EMBED_DIM=1024
BEDROCK_LLM_MODEL_ID=anthropic.claude-sonnet-5
LLM_EFFORT=low
LLM_MAX_TOKENS=1500

PGHOST=reglamento-lamar.abcd1234efgh.us-east-1.rds.amazonaws.com
PGPORT=5432
PGDATABASE=reglamento
PGUSER=postgres
PGPASSWORD=tu-password-real
PGSSLMODE=require

DB_TABLE=reglamento_chunks
TOP_K=6
MIN_SIMILARITY=0.25
```

Comprueba todo de una vez:

```bash
uvicorn backend.main:app --reload
curl http://localhost:8000/api/salud
```

Respuesta esperada:

```json
{
  "estado": "ok",
  "base_datos": "conectada",
  "chunks": 214,
  "modelo_embeddings": "amazon.titan-embed-text-v2:0",
  "modelo_llm": "anthropic.claude-sonnet-5",
  "dimension": 1024
}
```

---

## Parte 7 — Limpieza al terminar

RDS cobra por hora aunque no lo uses. Cuando ya tengas las capturas y el video:

1. RDS → selecciona `reglamento-lamar` → **Actions** → **Delete**
2. Desmarca *Create final snapshot* (no lo necesitas para la práctica)
3. Escribe `delete me` para confirmar

Bedrock no requiere limpieza: se cobra únicamente por invocación.

---

## Checklist de capturas para el PDF de evidencias

| # | Captura | Rúbrica |
|---|---|---|
| 1 | Bedrock — Model catalog o playground de Claude respondiendo | Infraestructura AWS |
| 2 | Política IAM del usuario | Infraestructura AWS |
| 3 | Instancia RDS *Available* con su endpoint | Infraestructura AWS |
| 4 | Security group con el puerto 5432 abierto | Infraestructura AWS |
| 5 | `SELECT extversion` mostrando pgvector activo | Infraestructura AWS |
| 6 | `\d reglamento_chunks` con la columna `vector(1024)` | Base de datos |
| 7 | Tabla con la columna vectorial y los metadatos | Base de datos |
| 8 | Metadata JSONB de un artículo | Ingesta y chunking |
| 9 | `EXPLAIN ANALYZE` usando el índice HNSW | Infraestructura AWS |
| 10–14 | Cinco consultas distintas en la interfaz de chat | Frontend y Backend |
