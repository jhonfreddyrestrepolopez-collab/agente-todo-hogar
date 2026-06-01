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


async def obtener_imagenes(categoria: str, max_resultados: int = 10) -> list[str]:
    """
    Consulta Cloudinary y devuelve las URLs (secure_url) de todas las imágenes
    que estén en la carpeta `categoria`.

    Retorna una lista vacía si no hay credenciales, si la carpeta está vacía
    o si ocurre un error con la API.
    """
    cloud_name = os.getenv("CLOUDINARY_CLOUD_NAME")
    api_key = os.getenv("CLOUDINARY_API_KEY")
    api_secret = os.getenv("CLOUDINARY_API_SECRET")

    if not all([cloud_name, api_key, api_secret]):
        logger.warning(
            "Credenciales de Cloudinary no configuradas "
            "(CLOUDINARY_CLOUD_NAME, CLOUDINARY_API_KEY, CLOUDINARY_API_SECRET)"
        )
        return []

    # Admin API: lista recursos de imagen cuyo public_id empieza por "categoria/"
    url = f"https://api.cloudinary.com/v1_1/{cloud_name}/resources/image"
    params = {
        "type": "upload",
        "prefix": f"{categoria}/",
        "max_results": max_resultados,
    }

    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(url, params=params, auth=(api_key, api_secret))
            if r.status_code != 200:
                logger.error(f"Error Cloudinary: {r.status_code} — {r.text}")
                return []
            data = r.json()
            urls = [
                recurso["secure_url"]
                for recurso in data.get("resources", [])
                if recurso.get("secure_url")
            ]
            logger.info(f"Cloudinary: {len(urls)} imágenes en la carpeta '{categoria}'")
            return urls
    except Exception as e:
        logger.error(f"Error consultando Cloudinary para '{categoria}': {e}")
        return []
