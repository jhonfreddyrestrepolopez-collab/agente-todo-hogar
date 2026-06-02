# agent/catalogo.py — Catálogo de productos por análisis visual (cacheado en BD)
# Generado por AgentKit

"""
Construye y consulta un catálogo estructurado de productos a partir de las
imágenes de Cloudinary.

Flujo:
  1. Lista las imágenes de cada carpeta/categoría en Cloudinary.
  2. La PRIMERA vez que ve una imagen (por su public_id), la analiza con Claude
     (visión) para extraer: nombre, alto, ancho y precio, y guarda el resultado
     en PostgreSQL (tabla `catalogo`). No se vuelve a analizar.
  3. Cuando un cliente pide fotos, se eligen imágenes según:
       - las que aún NO ha recibido,
       - la medida más cercana si pidió una medida,
       - el precio si pidió un presupuesto.

Así las medidas y precios dejan de depender del nombre del archivo y se vuelven
datos consultables.
"""

import re
import json
import logging

from agent.brain import client
from agent.cloudinary_images import (
    listar_imagenes,
    listar_carpetas,
    CATEGORIAS,
    CATEGORIAS_CLOSETS,
    MAX_IMAGENES_POR_RESPUESTA,
)
from agent.memory import (
    guardar_producto,
    obtener_productos,
    public_ids_enviados,
)

logger = logging.getLogger("agentkit")

MODELO_VISION = "claude-sonnet-4-6"

PROMPT_VISION = (
    "Eres un analista de catálogo de muebles. Te muestro la foto de UN producto "
    "(clóset, escritorio o mueble de TV). Devuelve SOLO un objeto JSON válido, sin "
    "texto adicional ni markdown, con estas claves exactas:\n"
    '  "nombre": string corto y descriptivo del producto (ej. "Clóset 4 puertas roble"),\n'
    '  "alto_cm": número en centímetros o null si no se puede determinar,\n'
    '  "ancho_cm": número en centímetros o null si no se puede determinar,\n'
    '  "precio": número entero (sin símbolos ni puntos) o null si no aparece.\n'
    "Si en la imagen hay medidas o precio escritos, úsalos. Si no, estima alto y "
    "ancho de forma razonable a partir de las proporciones del mueble y deja "
    "precio en null. NO inventes precios."
)


# ── Análisis visual de una imagen ─────────────────────────────────────────────

def _parsear_json(texto: str) -> dict:
    """Extrae el objeto JSON de la respuesta del modelo (tolerante a ```json)."""
    if not texto:
        return {}
    limpio = texto.strip()
    # Quitar vallas de código markdown si las hubiera.
    limpio = re.sub(r"^```(?:json)?", "", limpio).strip()
    limpio = re.sub(r"```$", "", limpio).strip()
    try:
        return json.loads(limpio)
    except json.JSONDecodeError:
        # Buscar el primer bloque {...}
        m = re.search(r"\{.*\}", limpio, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                return {}
        return {}


def _a_numero(valor) -> float | None:
    """Convierte a número o None."""
    if valor is None:
        return None
    if isinstance(valor, (int, float)):
        return float(valor)
    try:
        return float(re.sub(r"[^\d.]", "", str(valor)))
    except (ValueError, TypeError):
        return None


async def analizar_imagen(image_url: str) -> dict:
    """Analiza una imagen con Claude (visión) y devuelve nombre/alto/ancho/precio."""
    try:
        resp = await client.messages.create(
            model=MODELO_VISION,
            max_tokens=300,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "url", "url": image_url}},
                    {"type": "text", "text": PROMPT_VISION},
                ],
            }],
        )
        datos = _parsear_json(resp.content[0].text)
        precio = _a_numero(datos.get("precio"))
        return {
            "nombre": (datos.get("nombre") or None),
            "alto_cm": _a_numero(datos.get("alto_cm")),
            "ancho_cm": _a_numero(datos.get("ancho_cm")),
            "precio": int(precio) if precio is not None else None,
        }
    except Exception as e:
        logger.error(f"Error analizando imagen {image_url}: {e}")
        return {"nombre": None, "alto_cm": None, "ancho_cm": None, "precio": None}


# ── Sincronización del catálogo (analiza solo lo nuevo, una vez) ──────────────

async def sincronizar_categoria(categoria: str) -> int:
    """
    Asegura que todas las imágenes de la categoría estén analizadas en el catálogo.
    Analiza SOLO las imágenes nuevas o que CAMBIARON (la URL de Cloudinary incluye
    la versión, así que si la imagen se reemplaza, su URL cambia y se re-analiza).
    Devuelve cuántas analizó.
    """
    items = await listar_imagenes(categoria)
    # Mapa de lo ya catalogado: public_id -> URL guardada.
    existentes = {p["public_id"]: p["image_url"] for p in await obtener_productos(categoria)}
    nuevas = [it for it in items if existentes.get(it["public_id"]) != it["url"]]
    if nuevas:
        logger.info(f"Catálogo '{categoria}': analizando {len(nuevas)} imagen(es) nueva(s)/cambiada(s)")
    for it in nuevas:
        datos = await analizar_imagen(it["url"])
        await guardar_producto(
            public_id=it["public_id"],
            categoria=categoria,
            image_url=it["url"],
            nombre=datos["nombre"],
            alto_cm=datos["alto_cm"],
            ancho_cm=datos["ancho_cm"],
            precio=datos["precio"],
        )
        logger.info(f"  + [{categoria}] {it['public_id']}: {datos}")
    return len(nuevas)


async def sincronizar_todo() -> int:
    """
    Sincroniza TODAS las carpetas de Cloudinary (descubiertas dinámicamente).
    Uso típico: tarea de fondo al arrancar. Si no se pueden listar las carpetas,
    cae a la lista conocida CATEGORIAS como respaldo.
    """
    carpetas = await listar_carpetas()
    if not carpetas:
        logger.warning("No se descubrieron carpetas en Cloudinary; uso CATEGORIAS conocidas")
        carpetas = list(CATEGORIAS)

    total = 0
    for categoria in carpetas:
        try:
            total += await sincronizar_categoria(categoria)
        except Exception as e:
            logger.error(f"Error sincronizando '{categoria}': {e}")
    logger.info(
        f"Sincronización de catálogo completa: {total} imágenes nuevas/cambiadas "
        f"en {len(carpetas)} carpetas"
    )
    return total


# ── Interpretación del mensaje del cliente ────────────────────────────────────

def _a_cm(valor: float) -> float:
    """Heurística: valores < 10 se asumen en metros (1.8 -> 180 cm)."""
    return valor * 100 if valor < 10 else valor


def extraer_medidas(texto: str) -> dict:
    """
    Detecta medidas pedidas por el cliente. Devuelve {'alto_cm', 'ancho_cm'} con
    None donde no aplique. Reconoce ambos órdenes ("alto 1.80" y "1.80 de alto"),
    unidades (m/cm) y el patrón "180x120".
    """
    t = texto.lower()

    def cm(s: str) -> float:
        return _a_cm(float(s.replace(",", ".")))

    alto = ancho = None
    unidad = r"\s*(?:m|mts|metros|cm|cms)?\s*(?:de\s+)?"

    # Caso 1: número ANTES de la etiqueta ("1.80 de alto", "120 cm de ancho").
    m = re.search(r"(\d+[.,]?\d*)" + unidad + r"(?:alto|altura)", t)
    if m:
        alto = cm(m.group(1))
    m = re.search(r"(\d+[.,]?\d*)" + unidad + r"(?:ancho|anchura)", t)
    if m:
        ancho = cm(m.group(1))

    # Caso 2: etiqueta ANTES del número ("alto 1.80", "ancho de 120").
    if alto is None:
        m = re.search(r"(?:alto|altura)\s*(?:de\s+)?(\d+[.,]?\d*)", t)
        if m:
            alto = cm(m.group(1))
    if ancho is None:
        m = re.search(r"(?:ancho|anchura)\s*(?:de\s+)?(\d+[.,]?\d*)", t)
        if m:
            ancho = cm(m.group(1))

    # Caso 3: completar con el patrón "A x B".
    if alto is not None and ancho is None:
        m = re.search(r"x\s*(\d+[.,]?\d*)", t)
        if m:
            ancho = cm(m.group(1))
    elif ancho is not None and alto is None:
        m = re.search(r"(\d+[.,]?\d*)\s*x", t)
        if m:
            alto = cm(m.group(1))
    elif alto is None and ancho is None:
        m = re.search(r"(\d+[.,]?\d*)\s*(?:x|por|×)\s*(\d+[.,]?\d*)", t)
        if m:
            alto = cm(m.group(1))
            ancho = cm(m.group(2))

    return {"alto_cm": alto, "ancho_cm": ancho}


_PALABRAS_PRESUPUESTO = (
    "presupuesto", "hasta", "menos de", "maximo", "máximo", "max ",
    "tengo", "cuento con", "rango", "economic", "económic", "barat",
    "no mas de", "no más de", "por menos de", "que no pase de",
)


def extraer_presupuesto(texto: str) -> int | None:
    """
    Detecta un presupuesto máximo. Reconoce "400 mil", "400k", "450.000",
    "1 millón", etc. Solo lo considera si hay palabra de presupuesto o si el
    monto parece un precio (>= 10000 o lleva sufijo mil/k/millón).
    """
    t = texto.lower()
    tiene_keyword = any(p in t for p in _PALABRAS_PRESUPUESTO)

    candidatos = []
    for m in re.finditer(r"(\d+(?:[.,]\d+)*)\s*(millones|millón|millon|mill|mil|k)?", t):
        raw, suf = m.group(1), m.group(2)
        digitos = re.sub(r"[.,]", "", raw)
        if not digitos:
            continue
        val = int(digitos)
        if suf in ("mil", "k"):
            val *= 1_000
        elif suf in ("millones", "millón", "millon", "mill"):
            val *= 1_000_000
        # Aceptamos el monto si: hay sufijo, hay keyword, o parece precio grande.
        if suf or tiene_keyword or val >= 10_000:
            candidatos.append(val)

    return max(candidatos) if candidatos else None


def pidio_medida_o_precio(texto: str) -> bool:
    """True si el cliente menciona una medida o un presupuesto (aunque no diga categoría)."""
    medidas = extraer_medidas(texto)
    return (
        medidas["alto_cm"] is not None
        or medidas["ancho_cm"] is not None
        or extraer_presupuesto(texto) is not None
    )


# ── Selección de imágenes a enviar ────────────────────────────────────────────

def _distancia(producto: dict, alto: float | None, ancho: float | None) -> float | None:
    """Distancia entre las medidas del producto y las pedidas (None si no aplica)."""
    dims = []
    if alto is not None:
        if producto.get("alto_cm") is None:
            return None
        dims.append(producto["alto_cm"] - alto)
    if ancho is not None:
        if producto.get("ancho_cm") is None:
            return None
        dims.append(producto["ancho_cm"] - ancho)
    if not dims:
        return None
    return sum(d * d for d in dims) ** 0.5


async def elegir_imagenes(texto: str, categorias: list[str] | None, telefono: str,
                          modo_muestra: bool,
                          max_imgs: int = MAX_IMAGENES_POR_RESPUESTA) -> dict:
    """
    Decide qué productos enviar.

    `categorias`:
      - lista de categorías -> busca en esas carpetas (las sincroniza primero);
      - None -> busca en TODO el catálogo (útil cuando el cliente pide por medida
        o precio sin mencionar categoría).

    Devuelve:
      {
        "productos": [..],        # productos elegidos (con datos)
        "presupuesto": int|None,
        "medidas": {alto_cm, ancho_cm},
        "sin_nuevas": bool,       # True si el cliente ya recibió todo lo disponible
      }
    """
    # 1) Reunir productos del catálogo.
    if categorias:
        productos = []
        for categoria in categorias:
            await sincronizar_categoria(categoria)  # analiza solo lo nuevo/cambiado
            productos.extend(await obtener_productos(categoria))
        categorias_muestra = list(categorias)
    else:
        # Todo el catálogo (lo ya analizado en BD; el fondo lo mantiene al día).
        productos = await obtener_productos(None)
        categorias_muestra = sorted({p["categoria"] for p in productos})

    presupuesto = extraer_presupuesto(texto)
    medidas = extraer_medidas(texto)
    pidio_medida = medidas["alto_cm"] is not None or medidas["ancho_cm"] is not None

    # 2) Filtro por presupuesto (si lo pidió).
    base = productos
    if presupuesto is not None:
        base = [p for p in base if p["precio"] is not None and p["precio"] <= presupuesto]

    enviados = await public_ids_enviados(telefono)
    no_enviados = [p for p in base if p["public_id"] not in enviados]

    # 3) Selección según la intención.
    if pidio_medida:
        # Imagen cuya medida sea igual o la más cercana (prefiere no enviadas).
        con_dist = [
            (p, _distancia(p, medidas["alto_cm"], medidas["ancho_cm"]))
            for p in base
        ]
        con_dist = [(p, d) for p, d in con_dist if d is not None]
        con_dist.sort(key=lambda pd: pd[1])
        ordenados = [p for p, _ in con_dist]
        nuevos_ordenados = [p for p in ordenados if p["public_id"] not in enviados]
        elegidos = (nuevos_ordenados or ordenados)[:1]
        sin_nuevas = bool(ordenados) and not nuevos_ordenados
    elif modo_muestra:
        # Una muestra por categoría (1 por carpeta), priorizando no enviadas.
        elegidos = []
        usados = set()
        for fuente in (no_enviados, base):  # primero las no enviadas
            for categoria in categorias_muestra:
                if len(elegidos) >= max_imgs:
                    break
                for p in fuente:
                    if p["categoria"] == categoria and p["public_id"] not in usados:
                        elegidos.append(p)
                        usados.add(p["public_id"])
                        break
            if len(elegidos) >= max_imgs:
                break
        sin_nuevas = not no_enviados
    else:
        # "más fotos": las siguientes que aún no ha recibido.
        elegidos = no_enviados[:max_imgs]
        sin_nuevas = not no_enviados

    return {
        "productos": elegidos,
        "presupuesto": presupuesto,
        "medidas": medidas,
        "sin_nuevas": sin_nuevas,
    }


def caption_producto(p: dict) -> str:
    """Arma el texto que acompaña a la imagen con los datos del catálogo."""
    partes = [p.get("nombre") or p["categoria"].replace("_", " ")]
    alto, ancho = p.get("alto_cm"), p.get("ancho_cm")
    if alto and ancho:
        partes.append(f"{_fmt_cm(alto)} x {_fmt_cm(ancho)} cm")
    elif alto:
        partes.append(f"alto {_fmt_cm(alto)} cm")
    elif ancho:
        partes.append(f"ancho {_fmt_cm(ancho)} cm")
    if p.get("precio"):
        partes.append(f"${p['precio']:,.0f}".replace(",", "."))
    return " — ".join(partes)


def _fmt_cm(v: float) -> str:
    """Formatea cm sin decimales innecesarios (180.0 -> '180')."""
    return f"{v:g}"
