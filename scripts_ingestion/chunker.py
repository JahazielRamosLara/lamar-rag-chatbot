"""
Chunking jerarquico del Reglamento General de Alumnos (SEP R-0).

La idea central de la practica: NO partir el documento cada N caracteres, sino
respetar su estructura normativa. El parser recorre el texto linea por linea
manteniendo un "estado" de donde va parado (titulo -> capitulo -> articulo) y
emite un chunk por articulo, con toda la jerarquia como metadata.

Jerarquia soportada:

    TITULO PRIMERO / TITULO I
      CAPITULO I / CAPITULO PRIMERO
        Articulo 4o.- texto...
          a) inciso
          I. numeral

Reglas de segmentacion:

  * Unidad base = un articulo completo. Es la unidad que un alumno pide
    ("dame el articulo 4"), asi que conviene que viva en una sola fila.
  * Si un articulo excede MAX_CHARS, se parte por sus incisos/numerales en
    varios fragmentos. Cada fragmento repite el encabezado del articulo para
    no perder contexto al momento de embeber.
  * Todo lo que aparece antes del primer articulo de un capitulo (preambulos,
    considerandos) se guarda como chunk tipo "preambulo".

Este modulo no depende de AWS ni de la base de datos: se puede probar solo.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field, asdict
from typing import Iterator

# ---------------------------------------------------------------------------
# Parametros de segmentacion
# ---------------------------------------------------------------------------

MAX_CHARS = 1800   # arriba de esto, el articulo se parte por incisos/numerales
MIN_CHARS = 60     # fragmentos mas chicos que esto se pegan al anterior


# ---------------------------------------------------------------------------
# Expresiones regulares de encabezados normativos
# ---------------------------------------------------------------------------
# Se compilan con re.IGNORECASE porque el PDF mezcla "ARTICULO", "Articulo" y
# "Art.". El texto ya viene normalizado en cuanto a espacios, pero se conservan
# los acentos, por eso cada clase incluye la version con y sin tilde.

RE_TITULO = re.compile(
    r"^\s*T[ÍI]TULO\s+([A-ZÁÉÍÓÚÑ0-9]+)\s*[.\-–—:]*\s*(.*)$",
    re.IGNORECASE,
)

RE_CAPITULO = re.compile(
    r"^\s*CAP[ÍI]TULO\s+([A-ZÁÉÍÓÚÑ0-9]+)\s*[.\-–—:]*\s*(.*)$",
    re.IGNORECASE,
)

# "Articulo 4o.-", "ARTICULO 12.", "Art. 7 BIS -", "Articulo 3º"
RE_ARTICULO = re.compile(
    r"^\s*(?:ART[ÍI]CULO|ART\.?)\s+"
    r"(\d{1,3})\s*"                       # numero
    r"(?:[°ºªo\.]{0,2})\s*"               # ordinal opcional: 4o / 4º / 4°
    r"(BIS|TER|QU[ÁA]TER|QUINQUIES)?\s*"  # sufijo opcional
    r"[.\-–—:]*\s*"                       # separador
    r"(.*)$",
    re.IGNORECASE,
)

# Incisos: "a)", "b.-", "c )"
RE_INCISO = re.compile(r"^\s*([a-záéíóúñ])\s*[\)\.\-]\s+(.*)$")

# Numerales romanos: "I.", "IV)", "XII.-"
RE_NUMERAL_ROMANO = re.compile(r"^\s*((?:X{0,3})(?:IX|IV|V?I{0,3}))\s*[\)\.\-]\s+(.*)$")

# Numerales arabigos: "1.", "2)", "3.-"
RE_NUMERAL_ARABIGO = re.compile(r"^\s*(\d{1,2})\s*[\)\.\-]\s+(.*)$")

# Los reglamentos cierran con una seccion de transitorios que no cuelga de
# ningun capitulo. Sin esto, esos articulos heredan el ultimo capitulo leido.
RE_TRANSITORIOS = re.compile(
    r"^\s*(?:ART[ÍI]CULOS?\s+)?TRANSITORIOS?\s*[.\-–—:]*\s*$",
    re.IGNORECASE,
)

# Fin de oracion, para partir parrafos muy largos sin cortar a media frase.
# El lookbehind exige minuscula o digito antes del punto para no cortar en
# abreviaturas ni en ordinales del estilo "Art. 4o." o "Lic. Perez".
RE_FIN_ORACION = re.compile(r"(?<=[a-záéíóúñ0-9])[.;:]\s+(?=[A-ZÁÉÍÓÚÑ])")

# Ruido tipico del PDF: numeros de pagina sueltos, pies de pagina.
RE_RUIDO = re.compile(
    r"^\s*(?:p[áa]gina\s+)?\d{1,3}\s*(?:de\s+\d{1,3})?\s*$",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Conversion de ordinales a enteros (para poder filtrar/ordenar por capitulo)
# ---------------------------------------------------------------------------

ORDINALES_PALABRA = {
    "primero": 1, "primera": 1, "segundo": 2, "segunda": 2,
    "tercero": 3, "tercera": 3, "cuarto": 4, "cuarta": 4,
    "quinto": 5, "quinta": 5, "sexto": 6, "sexta": 6,
    "septimo": 7, "séptimo": 7, "septima": 7, "séptima": 7,
    "octavo": 8, "octava": 8, "noveno": 9, "novena": 9,
    "decimo": 10, "décimo": 10, "decima": 10, "décima": 10,
    "undecimo": 11, "undécimo": 11, "duodecimo": 12, "duodécimo": 12,
    "unico": 1, "único": 1, "unica": 1, "única": 1,
}

VALOR_ROMANO = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}


def _sin_acentos(texto: str) -> str:
    """Quita tildes; util para comparar contra el diccionario de ordinales."""
    descompuesto = unicodedata.normalize("NFD", texto)
    return "".join(c for c in descompuesto if unicodedata.category(c) != "Mn")


def entero_a_romano(numero: int) -> str:
    """Convierte 19 -> 'XIX'. Solo se usa para validar la forma canonica."""
    tabla = (
        (1000, "M"), (900, "CM"), (500, "D"), (400, "CD"),
        (100, "C"), (90, "XC"), (50, "L"), (40, "XL"),
        (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I"),
    )
    salida: list[str] = []
    for valor, simbolo in tabla:
        while numero >= valor:
            salida.append(simbolo)
            numero -= valor
    return "".join(salida)


def es_romano_probable(token: str) -> bool:
    """
    True si el token esta hecho solo de simbolos romanos, aunque este mal
    escrito. Sirve para distinguir un encabezado con erratas ('XVIX') de una
    linea de prosa que empieza con 'CAPÍTULO DE...' y no es un encabezado.
    """
    token = token.upper().strip()
    return bool(token) and all(c in VALOR_ROMANO for c in token)


def romano_a_entero(romano: str) -> int | None:
    """
    Convierte 'XIV' -> 14. Devuelve None si no es un romano valido.

    La validacion es estricta: se reconstruye el numero y se compara contra el
    token original. Sin esto, la lectura sustractiva ingenua acepta formas
    invalidas y devuelve valores equivocados en silencio. El caso real que lo
    motivo: el reglamento trae 'Capítulo XVIX' (errata por XIX). Leido de
    derecha a izquierda da 10-1+5... = 14, o sea que ese capitulo se fusionaba
    con el 'Capítulo XIV — De la reinscripción' y "dame el capítulo 14"
    devolvia articulos de dos capitulos distintos.
    """
    romano = romano.upper().strip()
    if not romano or any(c not in VALOR_ROMANO for c in romano):
        return None
    total = 0
    previo = 0
    for caracter in reversed(romano):
        valor = VALOR_ROMANO[caracter]
        total = total - valor if valor < previo else total + valor
        previo = max(previo, valor)
    if total <= 0 or entero_a_romano(total) != romano:
        return None  # mal formado: lo resuelve el conteo secuencial del parser
    return total


def ordinal_a_entero(token: str) -> int | None:
    """
    Normaliza el identificador de un titulo/capitulo a un entero.
    Acepta '3', 'III' y 'TERCERO'.
    """
    token = token.strip()
    if not token:
        return None
    if token.isdigit():
        return int(token)
    palabra = _sin_acentos(token).lower()
    if palabra in ORDINALES_PALABRA:
        return ORDINALES_PALABRA[palabra]
    return romano_a_entero(token)


# ---------------------------------------------------------------------------
# Modelo del chunk
# ---------------------------------------------------------------------------


@dataclass
class Chunk:
    """Una fila de la tabla vectorial: contenido + jerarquia normativa."""

    chunk_uid: str
    tipo: str                      # 'articulo' | 'preambulo'
    contenido: str
    encabezado: str = ""           # "Artículo 4°"
    titulo: str | None = None      # "TÍTULO PRIMERO — De los alumnos"
    titulo_num: int | None = None
    capitulo: str | None = None    # "CAPÍTULO III — De las inscripciones"
    capitulo_num: int | None = None
    articulo: int | None = None
    articulo_sufijo: str | None = None   # BIS / TER
    fragmento: int = 0             # 0 si el articulo cupo en un solo chunk
    total_fragmentos: int = 1
    incisos: list[str] = field(default_factory=list)
    numerales: list[str] = field(default_factory=list)

    # Texto que realmente se manda a embeber: contenido + contexto jerarquico.
    # Sin esto, "Artículo 4" y "Artículo 40" quedan practicamente identicos en
    # el espacio vectorial y la busqueda semantica los confunde.
    @property
    def texto_embebido(self) -> str:
        partes: list[str] = []
        if self.titulo:
            partes.append(self.titulo)
        if self.capitulo:
            partes.append(self.capitulo)
        if self.encabezado:
            partes.append(self.encabezado)
        partes.append(self.contenido)
        return "\n".join(partes)

    def metadata(self) -> dict:
        """El JSONB que se guarda junto al vector."""
        return {
            "tipo": self.tipo,
            "articulo": self.articulo,
            "articulo_sufijo": self.articulo_sufijo,
            "capitulo": self.capitulo_num,
            "capitulo_titulo": self.capitulo,
            "titulo": self.titulo_num,
            "titulo_nombre": self.titulo,
            "fragmento": self.fragmento,
            "total_fragmentos": self.total_fragmentos,
            "incisos": self.incisos,
            "numerales": self.numerales,
        }

    def to_dict(self) -> dict:
        datos = asdict(self)
        datos["texto_embebido"] = self.texto_embebido
        datos["metadata"] = self.metadata()
        return datos


# ---------------------------------------------------------------------------
# Estado del recorrido
# ---------------------------------------------------------------------------


@dataclass
class _Contexto:
    """Donde vamos parados mientras recorremos el documento."""

    titulo: str | None = None
    titulo_num: int | None = None
    capitulo: str | None = None
    capitulo_num: int | None = None

    # Ultimo numero valido visto de cada nivel. No se reinicia al cambiar de
    # titulo (la numeracion de capitulos del reglamento es continua), y sirve
    # para deducir el numero cuando el encabezado viene con errata.
    ultimo_titulo_num: int = 0
    ultimo_capitulo_num: int = 0


def _numero_de_seccion(token: str, ultimo: int) -> int | None:
    """
    Resuelve el numero de un TÍTULO o CAPÍTULO a partir de su identificador.

    Devuelve None solo si el token no parece un identificador de seccion (por
    ejemplo la prosa 'CAPÍTULO DE LAS SANCIONES...'), en cuyo caso el parser
    ignora la linea. Si el token si parece romano pero esta mal escrito, se
    asume que la seccion sigue a la anterior: los reglamentos numeran de forma
    continua, asi que 'XVIX' despues del XVIII es 19.
    """
    numero = ordinal_a_entero(token)
    if numero is not None:
        return numero
    if es_romano_probable(token):
        return ultimo + 1
    return None


def _formatea_encabezado(etiqueta: str, ident: str, nombre: str) -> str:
    """Arma 'CAPÍTULO III — De las inscripciones'."""
    nombre = nombre.strip(" .-–—:")
    base = f"{etiqueta} {ident.upper()}"
    return f"{base} — {nombre}" if nombre else base


def _nombre_en_siguiente_linea(lineas: list[str], indice: int) -> tuple[str, int]:
    """
    Los reglamentos suelen poner el nombre de la seccion en la linea de abajo:

        CAPÍTULO II
        DEL INGRESO Y LA INSCRIPCIÓN

    Devuelve (nombre, lineas_consumidas). Si la siguiente linea no parece un
    nombre de seccion, devuelve ("", 0) y el recorrido continua normal.
    """
    for salto in range(1, 3):  # tolera una linea en blanco intermedia
        posicion = indice + salto
        if posicion >= len(lineas):
            break
        candidata = lineas[posicion].strip()
        if not candidata:
            continue
        # No puede ser otro encabezado ni el inicio de un articulo.
        if (
            RE_TITULO.match(candidata)
            or RE_CAPITULO.match(candidata)
            or RE_ARTICULO.match(candidata)
            or RE_TRANSITORIOS.match(candidata)
        ):
            break
        # Un nombre de seccion es corto, va en mayusculas y no termina en punto.
        letras = [c for c in candidata if c.isalpha()]
        if (
            len(candidata) <= 90
            and letras
            and sum(c.isupper() for c in letras) / len(letras) > 0.8
            and not candidata.endswith(".")
        ):
            return candidata, salto
        break
    return "", 0


def _es_encabezado_articulo(linea: str) -> re.Match | None:
    """
    Un articulo real empieza la linea. Evitamos falsos positivos de referencias
    internas del estilo "...conforme al artículo 12 de este reglamento", que
    aparecen a media oracion y no al inicio.
    """
    match = RE_ARTICULO.match(linea)
    if not match:
        return None
    # Si la linea arranca en minuscula es prosa, no encabezado.
    if linea.lstrip()[:1].islower():
        return None
    return match


# ---------------------------------------------------------------------------
# Division de un articulo largo
# ---------------------------------------------------------------------------


def _detecta_marcadores(lineas: list[str]) -> tuple[list[str], list[str]]:
    """Devuelve (incisos, numerales) presentes en un bloque de lineas."""
    incisos: list[str] = []
    numerales: list[str] = []
    for linea in lineas:
        if (m := RE_INCISO.match(linea)) and m.group(1) not in incisos:
            incisos.append(m.group(1))
            continue
        if (m := RE_NUMERAL_ROMANO.match(linea)) and m.group(1):
            if m.group(1).upper() not in numerales:
                numerales.append(m.group(1).upper())
            continue
        if (m := RE_NUMERAL_ARABIGO.match(linea)) and m.group(1) not in numerales:
            numerales.append(m.group(1))
    return incisos, numerales


def _largo(lineas: list[str]) -> int:
    return sum(len(l) + 1 for l in lineas)


def _parte_por_prosa(lineas: list[str]) -> list[list[str]]:
    """
    Plan B para articulos largos que no tienen incisos ni numerales.

    Hay articulos —sobre todo los de sanciones y titulacion— que son varios
    parrafos corridos de texto. Sin marcadores donde cortar, el bloque entero
    quedaba en un solo chunk de hasta 4 mil caracteres, y un embedding de ese
    tamaño promedia tantos temas que deja de parecerse a cualquier pregunta
    concreta. Aqui se corta primero en frontera de parrafo (linea en blanco) y,
    si un solo parrafo ya excede el limite, en frontera de oracion.
    """
    if _largo(lineas) <= MAX_CHARS:
        return [lineas]

    # 1. Agrupar en parrafos usando las lineas en blanco como separador.
    parrafos: list[list[str]] = []
    actual: list[str] = []
    for linea in lineas:
        if linea.strip():
            actual.append(linea)
        elif actual:
            parrafos.append(actual)
            actual = []
    if actual:
        parrafos.append(actual)

    # 2. Un parrafo que por si solo pasa el limite se parte por oraciones.
    unidades: list[list[str]] = []
    for parrafo in parrafos:
        if _largo(parrafo) <= MAX_CHARS:
            unidades.append(parrafo)
            continue
        oraciones = RE_FIN_ORACION.split(" ".join(parrafo))
        bloque: list[str] = []
        for oracion in oraciones:
            # Se cierra ANTES de pasarse, no despues: si no, cada bloque
            # termina excediendo el limite por la ultima oracion.
            if bloque and _largo(bloque) + len(oracion) + 1 > MAX_CHARS:
                unidades.append(bloque)
                bloque = []
            bloque.append(oracion)
        if bloque:
            unidades.append(bloque)

    # 3. Reagrupar las unidades hasta llenar MAX_CHARS.
    bloques: list[list[str]] = []
    actual = []
    for unidad in unidades:
        if actual and _largo(actual) + _largo(unidad) > MAX_CHARS:
            bloques.append(actual)
            actual = []
        actual.extend(unidad)
    if actual:
        bloques.append(actual)

    return bloques or [lineas]


def _parte_articulo_largo(lineas: list[str]) -> list[list[str]]:
    """
    Corta un articulo largo en los puntos donde empieza un inciso o numeral,
    acumulando hasta MAX_CHARS. Nunca corta a media frase.
    """
    bloques: list[list[str]] = []
    actual: list[str] = []
    largo = 0

    for linea in lineas:
        inicia_item = bool(
            RE_INCISO.match(linea)
            or (RE_NUMERAL_ROMANO.match(linea) and RE_NUMERAL_ROMANO.match(linea).group(1))
            or RE_NUMERAL_ARABIGO.match(linea)
        )
        # Cortamos solo en frontera de item y solo si ya juntamos suficiente.
        if inicia_item and largo >= MAX_CHARS and actual:
            bloques.append(actual)
            actual, largo = [], 0
        actual.append(linea)
        largo += len(linea) + 1

    if actual:
        bloques.append(actual)

    # Los bloques que siguen pasados de largo no tenian marcadores donde
    # cortar: se parten por prosa.
    expandidos: list[list[str]] = []
    for bloque in bloques:
        expandidos.extend(_parte_por_prosa(bloque))
    bloques = expandidos

    # Un ultimo bloque diminuto se pega al anterior en vez de quedar suelto.
    if len(bloques) > 1 and sum(len(l) for l in bloques[-1]) < MIN_CHARS:
        bloques[-2].extend(bloques.pop())

    return bloques


# ---------------------------------------------------------------------------
# Constructor de chunks a partir de un articulo
# ---------------------------------------------------------------------------


def _chunks_de_articulo(
    ctx: _Contexto,
    numero: int,
    sufijo: str | None,
    lineas: list[str],
) -> list[Chunk]:
    encabezado_base = f"Artículo {numero}°"
    if sufijo:
        encabezado_base += f" {sufijo.upper()}"

    bloques = _parte_articulo_largo(lineas)
    total = len(bloques)
    chunks: list[Chunk] = []

    for indice, bloque in enumerate(bloques):
        contenido = "\n".join(bloque).strip()
        if not contenido:
            continue

        incisos, numerales = _detecta_marcadores(bloque)

        encabezado = encabezado_base
        if total > 1:
            encabezado = f"{encabezado_base} (parte {indice + 1} de {total})"

        uid = f"art-{numero}"
        if sufijo:
            uid += f"-{sufijo.lower()}"
        if total > 1:
            uid += f"-f{indice + 1}"

        chunks.append(
            Chunk(
                chunk_uid=uid,
                tipo="articulo",
                contenido=contenido,
                encabezado=encabezado,
                titulo=ctx.titulo,
                titulo_num=ctx.titulo_num,
                capitulo=ctx.capitulo,
                capitulo_num=ctx.capitulo_num,
                articulo=numero,
                articulo_sufijo=sufijo.upper() if sufijo else None,
                fragmento=indice,
                total_fragmentos=total,
                incisos=incisos,
                numerales=numerales,
            )
        )

    return chunks


# ---------------------------------------------------------------------------
# Parser principal
# ---------------------------------------------------------------------------


def segmentar(texto: str) -> list[Chunk]:
    """
    Recorre el reglamento completo y devuelve la lista de chunks jerarquicos.

    Es la funcion que consume `ingest.py`, pero tambien se puede llamar sola
    para verificar la segmentacion antes de gastar llamadas a Bedrock.
    """
    ctx = _Contexto()
    chunks: list[Chunk] = []

    # Buffer del articulo que estamos leyendo
    art_numero: int | None = None
    art_sufijo: str | None = None
    art_lineas: list[str] = []

    # Buffer de texto suelto antes del primer articulo de una seccion
    preambulo: list[str] = []
    contador_preambulos = 0

    def cerrar_articulo() -> None:
        nonlocal art_numero, art_sufijo, art_lineas
        if art_numero is not None and art_lineas:
            chunks.extend(_chunks_de_articulo(ctx, art_numero, art_sufijo, art_lineas))
        art_numero, art_sufijo, art_lineas = None, None, []

    def cerrar_preambulo() -> None:
        nonlocal preambulo, contador_preambulos
        lineas_preambulo = preambulo
        preambulo = []
        if len("\n".join(lineas_preambulo).strip()) < MIN_CHARS:
            return

        contador_preambulos += 1
        # Un preambulo tambien puede ser largo (el capitulo de sanciones abre
        # con varias cuartillas antes del primer articulo), asi que se parte
        # con el mismo criterio de prosa que los articulos.
        bloques = _parte_por_prosa(lineas_preambulo)
        total = len(bloques)

        for indice_bloque, bloque in enumerate(bloques):
            contenido = "\n".join(bloque).strip()
            if not contenido:
                continue
            uid = f"preambulo-{contador_preambulos}"
            if total > 1:
                uid += f"-f{indice_bloque + 1}"
            chunks.append(
                Chunk(
                    chunk_uid=uid,
                    tipo="preambulo",
                    contenido=contenido,
                    encabezado=ctx.capitulo or ctx.titulo or "Disposiciones generales",
                    titulo=ctx.titulo,
                    titulo_num=ctx.titulo_num,
                    capitulo=ctx.capitulo,
                    capitulo_num=ctx.capitulo_num,
                    fragmento=indice_bloque,
                    total_fragmentos=total,
                )
            )

    lineas = [l.rstrip() for l in texto.splitlines()]
    indice = 0

    while indice < len(lineas):
        linea = lineas[indice]
        indice += 1

        if not linea.strip() or RE_RUIDO.match(linea):
            # Las lineas en blanco separan parrafos dentro de un articulo.
            if art_numero is not None and art_lineas and art_lineas[-1] != "":
                art_lineas.append("")
            continue

        # --- TRANSITORIOS ---
        if RE_TRANSITORIOS.match(linea):
            cerrar_articulo()
            cerrar_preambulo()
            ctx.titulo = "ARTÍCULOS TRANSITORIOS"
            ctx.titulo_num = None
            ctx.capitulo, ctx.capitulo_num = None, None
            continue

        # --- TÍTULO ---
        if (m := RE_TITULO.match(linea)) and (
            (num := _numero_de_seccion(m.group(1), ctx.ultimo_titulo_num)) is not None
        ):
            cerrar_articulo()
            cerrar_preambulo()
            nombre = m.group(2).strip()
            if not nombre:
                nombre, consumidas = _nombre_en_siguiente_linea(lineas, indice - 1)
                indice += consumidas
            ctx.titulo = _formatea_encabezado("TÍTULO", m.group(1), nombre)
            ctx.titulo_num = num
            ctx.ultimo_titulo_num = num
            # Un titulo nuevo invalida el capitulo anterior.
            ctx.capitulo, ctx.capitulo_num = None, None
            continue

        # --- CAPÍTULO ---
        if (m := RE_CAPITULO.match(linea)) and (
            (num := _numero_de_seccion(m.group(1), ctx.ultimo_capitulo_num)) is not None
        ):
            cerrar_articulo()
            cerrar_preambulo()
            nombre = m.group(2).strip()
            if not nombre:
                nombre, consumidas = _nombre_en_siguiente_linea(lineas, indice - 1)
                indice += consumidas
            ctx.capitulo = _formatea_encabezado("CAPÍTULO", m.group(1), nombre)
            ctx.capitulo_num = num
            ctx.ultimo_capitulo_num = num
            continue

        # --- ARTÍCULO ---
        if (m := _es_encabezado_articulo(linea)) is not None:
            cerrar_articulo()
            cerrar_preambulo()
            art_numero = int(m.group(1))
            art_sufijo = m.group(2)
            resto = m.group(3).strip()
            art_lineas = [resto] if resto else []
            continue

        # --- Cuerpo ---
        if art_numero is not None:
            art_lineas.append(linea)
        else:
            preambulo.append(linea)

    cerrar_articulo()
    cerrar_preambulo()
    return chunks


def resumen(chunks: list[Chunk]) -> str:
    """Reporte corto para validar la segmentacion desde la terminal."""
    articulos = sorted({c.articulo for c in chunks if c.articulo is not None})
    capitulos = sorted({c.capitulo_num for c in chunks if c.capitulo_num is not None})
    titulos = sorted({c.titulo_num for c in chunks if c.titulo_num is not None})
    partidos = sum(1 for c in chunks if c.total_fragmentos > 1)
    largos = [len(c.contenido) for c in chunks] or [0]

    faltantes: list[int] = []
    if articulos:
        faltantes = [n for n in range(articulos[0], articulos[-1] + 1) if n not in articulos]

    lineas = [
        f"Chunks totales      : {len(chunks)}",
        f"Artículos detectados: {len(articulos)}"
        + (f"  (del {articulos[0]} al {articulos[-1]})" if articulos else ""),
        f"Capítulos           : {len(capitulos)} -> {capitulos}",
        f"Títulos             : {len(titulos)} -> {titulos}",
        f"Artículos partidos  : {partidos}",
        f"Largo de chunk      : min={min(largos)}  max={max(largos)}"
        f"  prom={sum(largos) // len(largos)}",
    ]
    if faltantes:
        lineas.append(f"AVISO: números de artículo ausentes: {faltantes}")
    return "\n".join(lineas)


def iter_chunks(texto: str) -> Iterator[Chunk]:
    """Version perezosa de `segmentar`, por comodidad."""
    yield from segmentar(texto)
