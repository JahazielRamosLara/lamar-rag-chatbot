# Chatbot Inteligente con Búsqueda Vectorial — Reglamento SEP R-0

**Universidad LAMAR · Sistemas de Base de Datos · Práctica Final**
Profesor: Mtro. Adriel Palestino

Arquitectura RAG (Retrieval-Augmented Generation) desplegada sobre AWS que
permite consultar de forma semántica y explícita el **Reglamento General de
Alumnos de Licenciaturas y Posgrados (SEP R-0)**.

---

## 1. Arquitectura

```
   Navegador (frontend/index.html)
            │  POST /api/chat
            ▼
   ┌─────────────────────────────────────────────┐
   │  Backend FastAPI  (backend/)                │
   │                                             │
   │  1. retrieval.py  ¿La pregunta cita un      │
   │                   artículo o capítulo?      │
   │  2. bedrock.py    Vectoriza la consulta     │
   │  3. db.py         Búsqueda híbrida          │
   │  4. rag.py        Prompt + generación       │
   └───────┬──────────────────────────┬──────────┘
           │                          │
           ▼                          ▼
 ┌────────────────────┐   ┌──────────────────────────┐
 │  AWS RDS           │   │  AWS Bedrock             │
 │  PostgreSQL 16     │   │  · Titan Embeddings V2   │
 │  + pgvector (HNSW) │   │  · Claude (generación)   │
 └────────────────────┘   └──────────────────────────┘
```

| Componente | Tecnología | Responsabilidad |
|---|---|---|
| Base de datos vectorial | AWS RDS PostgreSQL 16 + `pgvector` | Almacena los embeddings y la metadata jerárquica de cada sección |
| Backend API | Python 3.12 + FastAPI + asyncpg | Enrutamiento de intención, búsqueda híbrida y orquestación del RAG |
| Frontend | HTML + CSS + Fetch API (sin build) | Interfaz de chat con historial, indicador de carga y fuentes citadas |
| Embeddings | AWS Bedrock — `amazon.titan-embed-text-v2:0` | Vectores de 1024 dimensiones, normalizados |
| LLM | Anthropic Claude, vía AWS Bedrock o API directa (`LLM_PROVIDER`) | Sintetiza la respuesta final a partir del contexto recuperado |

---

## 2. Estructura del repositorio

```
lamar-rag-chatbot/
├── backend/                    API FastAPI
│   ├── config.py               Configuración central (lee .env)
│   ├── bedrock.py              Cliente de embeddings (Titan) y LLM (Claude)
│   ├── db.py                   Consultas a pgvector: vectorial, léxica, híbrida
│   ├── retrieval.py            Detección de intención + orquestación de búsqueda
│   ├── rag.py                  Prompt RAG y generación de la respuesta
│   ├── schemas.py              Contratos Pydantic de la API
│   └── main.py                 Endpoints y servidor
│
├── frontend/
│   └── index.html              Interfaz de chat (autocontenida, sin dependencias)
│
├── scripts_ingestion/
│   ├── pdf_extract.py          Extracción y limpieza del PDF
│   ├── chunker.py              Segmentación jerárquica (títulos/capítulos/artículos)
│   ├── ingest.py               Pipeline completo: PDF → chunks → embeddings → BD
│   ├── probar_chunker.py       Verificación de la segmentación sin AWS ni BD
│   ├── probar_bedrock.py       Verifica que Titan y Claude respondan desde esta cuenta
│   └── probar_modelos.py       Averigua qué modelos de Claude acepta invocar la cuenta de Bedrock
│
├── infra/
│   ├── schema.sql              Tabla, extensión pgvector e índices
│   └── aws_setup.md            Guía paso a paso del despliegue en AWS
│
├── data/
│   ├── raw/                    Aquí va el PDF oficial (ignorado por git)
│   └── muestra/                Reglamento ficticio para probar el chunker
│
├── requirements.txt
└── .env.example
```

---

## 3. Puesta en marcha

### 3.1 Requisitos previos

- Python 3.11 o superior (probado en 3.13)
- Una cuenta de AWS con acceso a **Bedrock** y a **RDS**
- El PDF del Reglamento SEP R-0

### 3.2 Instalación

```bash
git clone <url-del-repositorio>
cd lamar-rag-chatbot

python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
```

### 3.3 Configuración

```bash
cp .env.example .env      # en Windows: copy .env.example .env
```

Llena en `.env` el endpoint de RDS, la contraseña y la región de Bedrock.
El detalle de cómo obtener cada valor está en [`infra/aws_setup.md`](infra/aws_setup.md).

### 3.4 Ingesta del reglamento

Coloca el PDF en `data/raw/REGLAMENTO_GENERAL_DE_ALUMNOS_DE_LICENCIATURAS_Y_POSGRADOS_SEP_R-0.pdf` y verifica **primero** la
segmentación, antes de gastar llamadas a Bedrock:

```bash
python scripts_ingestion/ingest.py --pdf data/raw/REGLAMENTO_GENERAL_DE_ALUMNOS_DE_LICENCIATURAS_Y_POSGRADOS_SEP_R-0.pdf --dry-run
```

Salida esperada (números aproximados según el documento):

```
Chunks totales      : 214
Artículos detectados: 198  (del 1 al 198)
Capítulos           : 21 -> [1, 2, 3, ...]
Títulos             : 6  -> [1, 2, 3, 4, 5, 6]
Artículos partidos  : 11
```

Si el conteo de artículos no cuadra con el PDF, el problema está en la
extracción, no en el RAG. Inspecciona con:

```bash
python scripts_ingestion/probar_chunker.py --pdf data/raw/REGLAMENTO_GENERAL_DE_ALUMNOS_DE_LICENCIATURAS_Y_POSGRADOS_SEP_R-0.pdf --listar
python scripts_ingestion/probar_chunker.py --pdf data/raw/REGLAMENTO_GENERAL_DE_ALUMNOS_DE_LICENCIATURAS_Y_POSGRADOS_SEP_R-0.pdf --ver 4
```

Cuando el reporte se vea correcto, corre la ingesta real:

```bash
python scripts_ingestion/ingest.py --pdf data/raw/REGLAMENTO_GENERAL_DE_ALUMNOS_DE_LICENCIATURAS_Y_POSGRADOS_SEP_R-0.pdf --recrear
```

### 3.5 Levantar la aplicación

```bash
uvicorn backend.main:app --reload
```

| URL | Contenido |
|---|---|
| <http://localhost:8000> | Interfaz de chat |
| <http://localhost:8000/docs> | Documentación Swagger |
| <http://localhost:8000/api/salud> | Diagnóstico de BD y modelos |

---

### 3.6 Puesta en marcha para el resto del equipo

Quien clona el repositorio no recibe ni los secretos ni el PDF: ambos están en
`.gitignore` a propósito. Estos son los cuatro pasos que faltan, y el primero
**no lo puede hacer quien clona** — depende de quien administra la instancia.

#### 1. Autorizar su IP en el security group (lo hace el dueño de la instancia)

La base solo acepta conexiones desde las IPs listadas en `reglamento-sg`. Si la
IP del nuevo integrante no está, la conexión **se queda colgada hasta agotar el
timeout, sin mensaje de error útil** — es el fallo más confuso de todos.

En **RDS → reglamento-lamar → Connectivity & security → security group →
Inbound rules → Add rule**, agrega una regla `PostgreSQL / 5432` con la IP del
integrante. Cada quien puede consultar la suya en <https://checkip.amazonaws.com>.

> Esa IP cambia al moverse de red (casa → universidad). Si de un día para otro
> deja de conectar, esto es lo primero que hay que revisar.

#### 2. Crear el `.env`

```bash
cp .env.example .env      # Windows: copy .env.example .env
```

Y llenar tres valores. Los dos primeros son idénticos para todo el equipo,
porque apuntan a la misma base:

| Variable | De dónde sale |
|---|---|
| `PGHOST` | El endpoint de RDS; el mismo para todos |
| `PGPASSWORD` | La contraseña maestra que se definió al crear la instancia |
| `ANTHROPIC_API_KEY` | console.anthropic.com → API Keys (puede ser compartida) |

#### 3. Configurar credenciales de AWS

**Cada integrante necesita las suyas**, aunque la base ya esté cargada: cada
pregunta del usuario se convierte en vector con Titan en el momento de
consultar, así que el chat llama a Bedrock en cada turno.

Se necesita un usuario IAM con la política de la Parte 1 de
[`infra/aws_setup.md`](infra/aws_setup.md), y sus llaves en `~/.aws/credentials`
(vía `aws configure` o escribiendo el archivo a mano).

#### 4. El PDF, solo si se va a re-ingestar

`data/raw/*.pdf` está excluido del repositorio. Para **usar** el chatbot no hace
falta: los 217 chunks ya viven en la base. Solo hay que colocarlo si se quiere
volver a correr `ingest.py`.

#### Comprobar que quedó

```bash
python scripts_ingestion/probar_bedrock.py   # ¿responden Titan y Claude?
uvicorn backend.main:app --reload            # y luego /api/salud
```

`/api/salud` debe reportar `"estado": "ok"` con 217 chunks. Si marca error de
base de datos, es el paso 1. Si `probar_bedrock.py` falla, es el paso 3.

---

## 4. Estrategia de segmentación (chunking)

Este es el corazón de la práctica. Partir el PDF cada N caracteres rompe los
artículos a la mitad y hace imposible responder *"dame el artículo 4"*, así que
la segmentación es **consciente del formato normativo**.

### 4.1 Reconocimiento de la jerarquía

`chunker.py` recorre el documento línea por línea manteniendo un estado de
dónde va parado, y reconoce los encabezados con expresiones regulares:

| Nivel | Patrón reconocido | Ejemplos que acepta |
|---|---|---|
| Título | `T[ÍI]TULO\s+(\w+)` | `TÍTULO PRIMERO`, `TÍTULO VI` |
| Capítulo | `CAP[ÍI]TULO\s+(\w+)` | `CAPÍTULO III`, `Capítulo Segundo` |
| Artículo | `(?:ART[ÍI]CULO\|ART\.?)\s+(\d+)…` | `Artículo 4°.-`, `ARTICULO 12.`, `Art. 7 BIS` |
| Inciso | `^([a-z])\s*[\)\.\-]` | `a)`, `b.-` |
| Numeral | romanos y arábigos | `I.`, `IV)`, `1.` |
| Transitorios | `(ART[ÍI]CULOS?\s+)?TRANSITORIOS?` | `ARTÍCULOS TRANSITORIOS` |

Los identificadores se normalizan a enteros (`III` → 3, `PRIMERO` → 1) para
poder filtrar y ordenar por capítulo desde SQL.

### 4.2 Reglas de corte

1. **La unidad base es el artículo completo.** Es lo que un alumno pide por su
   nombre, así que vive en una sola fila de la tabla.
2. **Los artículos largos (> 1800 caracteres) se parten por sus incisos o
   numerales**, nunca a media frase, y cada fragmento repite el encabezado del
   artículo para no perder contexto al vectorizarse.
3. **El texto anterior al primer artículo** de una sección se guarda como chunk
   de tipo `preambulo`.

### 4.3 Enriquecimiento de metadata

Cada vector se guarda con su jerarquía completa, tanto en columnas indexadas
como en un `JSONB`:

```json
{
  "tipo": "articulo",
  "articulo": 4,
  "articulo_sufijo": null,
  "capitulo": 2,
  "capitulo_titulo": "CAPÍTULO II — Del ingreso y la inscripción",
  "titulo": 1,
  "fragmento": 0,
  "total_fragmentos": 1,
  "incisos": [],
  "numerales": ["I", "II", "III", "IV", "V"]
}
```

### 4.4 Texto contextualizado para el embedding

No se vectoriza el artículo en crudo, sino el artículo **precedido de su
jerarquía**:

```
TÍTULO PRIMERO — Disposiciones generales
CAPÍTULO II — Del ingreso y la inscripción
Artículo 4°
Son requisitos para obtener la calidad de alumno los siguientes: …
```

Sin este contexto, los vectores de artículos vecinos quedan casi idénticos y la
búsqueda semántica los confunde.

---

## 5. Estrategia de búsqueda

Una búsqueda puramente vectorial falla con *"dame el artículo 4"*: los
embeddings de `"artículo 4"` y `"artículo 40"` son prácticamente iguales. Por
eso la recuperación combina tres caminos.

| Camino | Cuándo se usa | Mecanismo en PostgreSQL |
|---|---|---|
| **Metadata** | La pregunta cita un artículo o capítulo | `WHERE articulo = $1` sobre índice B-tree |
| **Vectorial** | Siempre | `ORDER BY embedding <=> $1` sobre índice HNSW |
| **Léxica** | Siempre | `to_tsvector('spanish', …)` sobre índice GIN |

Los rankings vectorial y léxico se fusionan con **Reciprocal Rank Fusion**
(`1 / (60 + posición)`), que evita tener que normalizar dos escalas de puntaje
que no son comparables entre sí (similitud coseno vs. `ts_rank`).

### Elección de la métrica de similitud

| Operador | Métrica | Decisión |
|---|---|---|
| `<=>` | **Distancia coseno** | ✅ **Elegida.** Titan V2 con `normalize: true` entrega vectores de norma 1; el coseno queda acotado en `[0, 2]` y se convierte directo a similitud con `1 - distancia`, lo que hace legible el umbral de corte. |
| `<->` | Distancia L2 | Con vectores normalizados ordena igual que el coseno, pero su escala no se interpreta como porcentaje de parecido. |
| `<#>` | Inner product | Equivalente al coseno con vectores normalizados, pero pgvector devuelve el producto punto negado, lo que complica el umbral. |

### Elección del índice

**HNSW** sobre IVFFlat, porque IVFFlat necesita datos ya cargados para calcular
sus listas y hay que reconstruirlo cuando el corpus cambia, mientras que HNSW se
crea con la tabla vacía. Con el tamaño de este corpus el recall es prácticamente
del 100% y la latencia se mide en milisegundos.

---

## 6. Endpoints de la API

| Método | Ruta | Descripción |
|---|---|---|
| `POST` | `/api/chat` | Ciclo RAG completo. Recibe `{pregunta, historial}` y devuelve la respuesta con sus fuentes |
| `GET` | `/api/salud` | Estado de la BD, número de chunks y modelos configurados |
| `GET` | `/api/articulo/{n}` | Devuelve un artículo tal cual, **sin pasar por el LLM** |
| `GET` | `/api/intencion?pregunta=…` | Muestra qué artículos y capítulos detecta el router |

Ejemplo:

```bash
curl -X POST http://localhost:8000/api/chat \
  -H "Content-Type: application/json" \
  -d '{"pregunta": "¿Qué dice el artículo 4 del reglamento?"}'
```

---

## 7. Consultas de prueba sugeridas

Para el documento de evidencias se piden al menos 5 consultas distintas,
combinando peticiones directas e indirectas:

| # | Consulta | Qué demuestra |
|---|---|---|
| 1 | `¿Qué dice el artículo 4 del reglamento?` | Petición explícita → filtrado por metadata |
| 2 | `Dame el artículo 4` | Misma intención, redacción distinta |
| 3 | `¿Cuánta asistencia necesito para presentar examen ordinario?` | Búsqueda puramente semántica |
| 4 | `¿Qué pasa si repruebo tres veces la misma materia?` | Semántica sin coincidencia léxica exacta |
| 5 | `¿Qué dice el capítulo III?` | Filtrado jerárquico por capítulo |
| 6 | `¿Cuál es el reglamento de estacionamiento?` | Caso negativo: el bot debe reconocer que no está cubierto |

La consulta 6 importa tanto como las demás: comprueba que el sistema **no
alucina** cuando el reglamento no cubre el tema.

---

## 8. Solución de problemas

| Síntoma | Causa probable | Solución |
|---|---|---|
| `/api/salud` marca `sin datos ingestados` | La tabla está vacía | Corre `ingest.py` |
| `AccessDeniedException` en Bedrock | Modelo sin habilitar | Bedrock → *Model access* → habilita Titan y Claude |
| `ValidationException` con el `modelId` | El modelo no existe en esa región | Revisa `BEDROCK_LLM_MODEL_ID` y `AWS_REGION` |
| Timeout al conectar a RDS | Security group cerrado | Abre el puerto 5432 a tu IP (ver `infra/aws_setup.md`) |
| `type "vector" does not exist` | Falta la extensión | `CREATE EXTENSION vector;` en la base correcta |
| Dimensión no coincide | Cambiaste de modelo de embeddings | Ajusta `EMBED_DIM` y reingesta con `--recrear` |
| El PDF no devuelve texto | PDF escaneado (solo imágenes) | Necesita OCR previo |

---

## 9. Integrantes

| Nombre | Matrícula |
|---|---|
| Jahaziel Osmar Ramos Lara | 000050444 |
| Keb Emiliano Moreno Alcaráz | 000049870 |
