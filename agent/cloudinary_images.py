# agent/cloudinary_images.py — Catálogo de imágenes desde Cloudinary
# Generado por AgentKit

"""
Administra las imágenes del catálogo consultando carpetas de Cloudinary.

Así puedes agregar o quitar fotos directamente en Cloudinary (subiéndolas a
la carpeta de cada categoría) SIN volver a tocar el código.

Configuración (variables de entorno en .env / Railway):
    CLOUDINARY_CLOUD_NAME   nombre de tu cloud (ej: dn8arwqww)
    CLOUDINARY_API_KEY      API Key de tu cuenta Cloudinary
    CLOUDINARY_API_SECRET   API Secret de tu cuenta Cloudinary

Las imágenes de cada categoría deben estar en una carpeta de Cloudinary con
el mismo nombre que la categoría, por ejemplo:
    escritorios/...
    closet_6_puertas/...

Documentación: https://cloudinary.com/documentation/admin_api#get_resources
"""

import os
import logging
import httpx

logger = logging.getLogger("agentkit")

# Categorías disponibles = nombres EXACTOS de las carpetas en Cloudinary.
# El orden importa para la detección (lo más específico primero).
# OJO: "mesa de tv" lleva espacios porque así se llama la carpeta en Cloudinary.
CATEGORIAS = [
    "closet_2_puertas",
    "closet_3_puertas",
    "closet_4_puertas",
    "closet_5_puertas",
    "closet_6_puertas",
    "escritorios",
    "mesa de tv",
]

# Solo las carpetas de clósets (para "todos los closets").
CATEGORIAS_CLOSETS = [c for c in CATEGORIAS if c.startswith("closet")]

# Máximo de imágenes a enviar en una sola respuesta (evita spam).
MAX_IMAGENES_POR_RESPUESTA = 5

# Palabras/frases que disparan cada categoría cuando el cliente escribe.
PALABRAS_POR_CATEGORIA = {
    "closet_2_puertas": ["2 puertas", "dos puertas", "closet de 2", "clóset de 2"],
    "closet_3_puertas": ["3 puertas", "tres puertas", "closet de 3", "clóset de 3"],
    "closet_4_puertas": ["4 puertas", "cuatro puertas", "closet de 4", "clóset de 4"],
    "closet_5_puertas": ["5 puertas", "cinco puertas", "closet de 5", "clóset de 5"],
    "closet_6_puertas": ["6 puertas", "seis puertas", "closet de 6", "clóset de 6"],
    "escritorios": ["escritorio", "escritorios"],
    "mesa de tv": ["mesa de tv", "mesa tv", "mueble de tv", "mesa para tv", "rack de tv"],
}

# Frases que piden TODO el catálogo (imágenes de todas las carpetas).
PALABRAS_CATALOGO_COMPLETO = [
    "catalogo completo", "catálogo completo",
    "todo el catalogo", "todo el catálogo",
    "todos los diseños", "todos los disenos", "todos los disenos",
    "todos los modelos", "todos los productos",
    "todo lo que tienen", "todo lo que tienes",
]


# Frases que piden todos los clósets (muestra de cada carpeta de clóset).
PALABRAS_TODOS_CLOSETS = [
    "todos los closets", "todos los clósets", "todos los closet", "todos los clóset",
    "todos los closets", "todas las closets",
    "todos los armarios", "todos los modelos de closet",
]


def solicita_catalogo_completo(texto: str) -> bool:
    """True si el cliente pide el catálogo completo / todos los diseños / modelos."""
    texto_lower = texto.lower()
    return any(frase in texto_lower for frase in PALABRAS_CATALOGO_COMPLETO)


def solicita_todos_closets(texto: str) -> bool:
    """True si el cliente pide ver todos los clósets (muestra de cada carpeta)."""
    texto_lower = texto.lower()
    return any(frase in texto_lower for frase in PALABRAS_TODOS_CLOSETS)


def detectar_categoria(texto: str) -> str | None:
    """
    Devuelve el nombre de la categoría que coincide con el texto del cliente,
    o None si no detecta ninguna. Revisa las categorías en orden (las más
    específicas, como los clósets por número de puertas, van primero).
    """
    texto_lower = texto.lower()
    for categoria in CATEGORIAS:
        for palabra in PALABRAS_POR_CATEGORIA.get(categoria, []):
            if palabra in texto_lower:
                return categoria
    return None


def _urls_de_resultado(data: dict) -> list[str]:
    """Extrae las secure_url de la respuesta de la Admin API de Cloudinary."""
    return [
        recurso["secure_url"]
        for recurso in data.get("resources", [])
        if recurso.get("secure_url")
    ]


async def obtener_imagenes(categoria: str, max_resultados: int = 10) -> list[str]:
    """
    Consulta Cloudinary y devuelve las URLs (secure_url) de las imágenes que estén
    en la carpeta `categoria`.

    Funciona con los dos modelos de carpetas de Cloudinary:
      1. Carpetas dinámicas (asset_folder) — modo por defecto desde 2024.
      2. Carpetas clásicas (prefijo en el public_id, ej "categoria/archivo").

    Retorna una lista vacía si no hay credenciales, si la carpeta está vacía
    o si ocurre un error con la API.
    """
    # Leemos las credenciales y limpiamos espacios/saltos de línea accidentales.
    cloud_name = (os.getenv("CLOUDINARY_CLOUD_NAME") or "").strip()
    api_key = (os.getenv("CLOUDINARY_API_KEY") or "").strip()
    api_secret = (os.getenv("CLOUDINARY_API_SECRET") or "").strip()

    # Logs de diagnóstico (temporales): muestran si cada variable llegó al proceso,
    # SIN exponer su valor real por seguridad.
    logger.info(f"Cloudinary cloud_name definido: {bool(cloud_name)}")
    logger.info(f"Cloudinary api_key definida: {bool(api_key)}")
    logger.info(f"Cloudinary api_secret definida: {bool(api_secret)}")

    if not all([cloud_name, api_key, api_secret]):
        logger.warning(
            "Credenciales de Cloudinary no configuradas "
            "(CLOUDINARY_CLOUD_NAME, CLOUDINARY_API_KEY, CLOUDINARY_API_SECRET)"
        )
        return []

    base = f"https://api.cloudinary.com/v1_1/{cloud_name}"
    auth = (api_key, api_secret)

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            # Estrategia 1: carpetas dinámicas (asset_folder).
            r = await client.get(
                f"{base}/resources/by_asset_folder",
                params={"asset_folder": categoria, "max_results": max_resultados},
                auth=auth,
            )
            if r.status_code == 200:
                urls = _urls_de_resultado(r.json())
                if urls:
                    logger.info(
                        f"Cloudinary (asset_folder): {len(urls)} imágenes en '{categoria}'"
                    )
                    return urls
            else:
                logger.warning(
                    f"Cloudinary by_asset_folder '{categoria}': "
                    f"{r.status_code} — {r.text[:200]}"
                )

            # Estrategia 2: carpetas clásicas (prefijo del public_id).
            r = await client.get(
                f"{base}/resources/image",
                params={"type": "upload", "prefix": f"{categoria}/", "max_results": max_resultados},
                auth=auth,
            )
            if r.status_code == 200:
                urls = _urls_de_resultado(r.json())
                logger.info(f"Cloudinary (prefix): {len(urls)} imágenes en '{categoria}'")
                return urls

            logger.error(f"Error Cloudinary '{categoria}': {r.status_code} — {r.text[:200]}")
            return []
    except Exception as e:
        logger.error(f"Error consultando Cloudinary para '{categoria}': {e}")
        return []
