# agent/main.py — Servidor FastAPI + Webhook de WhatsApp
# Generado por AgentKit

"""
Servidor principal del agente de WhatsApp.
Funciona con cualquier proveedor gracias a la capa de providers (aquí: Whapi.Cloud).
"""

import os
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import PlainTextResponse
from dotenv import load_dotenv

from agent.brain import generar_respuesta
from agent.memory import inicializar_db, guardar_mensaje, obtener_historial
from agent.providers import obtener_proveedor
from agent.cloudinary_images import (
    detectar_categoria,
    obtener_imagenes,
    solicita_catalogo_completo,
    solicita_todos_closets,
    CATEGORIAS,
    CATEGORIAS_CLOSETS,
    MAX_IMAGENES_POR_RESPUESTA,
)

load_dotenv()

# Configuración de logging según entorno
ENVIRONMENT = os.getenv("ENVIRONMENT", "development")
log_level = logging.DEBUG if ENVIRONMENT == "development" else logging.INFO
logging.basicConfig(level=log_level)
logger = logging.getLogger("agentkit")

# Proveedor de WhatsApp (se configura en .env con WHATSAPP_PROVIDER)
proveedor = obtener_proveedor()
PORT = int(os.getenv("PORT", 8000))


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Inicializa la base de datos al arrancar el servidor."""
    await inicializar_db()
    logger.info("Base de datos inicializada")
    logger.info(f"Servidor AgentKit corriendo en puerto {PORT}")
    logger.info(f"Proveedor de WhatsApp: {proveedor.__class__.__name__}")

    # Diagnóstico temporal: muestra los NOMBRES de las variables de entorno que
    # contienen "CLOUD" (sin exponer valores). Revela si Railway está pasando las
    # variables con un nombre distinto (espacios, typos) o si no llegan al proceso.
    claves_cloud = sorted(k for k in os.environ if "CLOUD" in k.upper())
    logger.info(f"Variables de entorno con 'CLOUD': {claves_cloud}")
    for nombre in ("CLOUDINARY_CLOUD_NAME", "CLOUDINARY_API_KEY", "CLOUDINARY_API_SECRET"):
        logger.info(f"  presente {nombre}: {nombre in os.environ}")
    yield


app = FastAPI(
    title="AgentKit — WhatsApp AI Agent",
    version="1.0.0",
    lifespan=lifespan
)


@app.get("/")
async def health_check():
    """Endpoint de salud para Railway/monitoreo."""
    return {"status": "ok", "service": "agentkit"}


@app.get("/webhook")
async def webhook_verificacion(request: Request):
    """Verificación GET del webhook (no-op para Whapi; útil si cambias de proveedor)."""
    resultado = await proveedor.validar_webhook(request)
    if resultado is not None:
        return PlainTextResponse(str(resultado))
    return {"status": "ok"}


@app.post("/webhook")
async def webhook_handler(request: Request):
    """
    Recibe mensajes de WhatsApp via el proveedor configurado.
    Procesa el mensaje, genera respuesta con Claude y la envía de vuelta.
    """
    try:
        # Parsear webhook — el proveedor normaliza el formato
        mensajes = await proveedor.parsear_webhook(request)

        for msg in mensajes:
            # Ignorar mensajes propios o vacíos
            if msg.es_propio or not msg.texto:
                continue

            logger.info(f"Mensaje de {msg.telefono}: {msg.texto}")

            # Obtener historial ANTES de guardar el mensaje actual
            # (brain.py agrega el mensaje actual, evitando duplicados)

            historial = await obtener_historial(msg.telefono)
            texto_lower = msg.texto.lower()

            # Detectar si el cliente está pidiendo fotos/imágenes.
            # Cubre singular/plural y variantes con y sin acento, además de
            # frases como "muéstrame modelos", "ver catálogo", "fotos de closets".
            palabras_imagen = (
                "foto", "fotos",
                "imagen", "imagenes", "imágenes",
                "enviame imagenes", "envíame imágenes",
                "quiero ver fotos",
                "muestrame modelos", "muéstrame modelos", "modelos",
                "ver catalogo", "ver catálogo", "catalogo", "catálogo",
                "closet", "closets", "clóset", "clósets",
            )
            # Se considera solicitud de imagen si: usa palabras de "foto/catálogo",
            # menciona una categoría concreta, o pide el catálogo completo.
            solicita_imagen = (
                any(palabra in texto_lower for palabra in palabras_imagen)
                or detectar_categoria(msg.texto) is not None
                or solicita_catalogo_completo(msg.texto)
            )

            if solicita_imagen:
                logger.info(f"Solicitud de imagen detectada de {msg.telefono}: {msg.texto}")

                # Decidir qué carpetas enviar y si es una "muestra" (1 por carpeta)
                # o todas las imágenes de una sola categoría.
                #   - Catálogo completo  -> muestra de TODAS las carpetas
                #   - Todos los closets  -> muestra de las carpetas de clóset
                #   - Categoría puntual  -> hasta 5 imágenes de esa carpeta
                if solicita_catalogo_completo(msg.texto):
                    categorias_a_enviar = list(CATEGORIAS)
                    modo_muestra = True
                    logger.info(f"Categoría detectada: catálogo completo {categorias_a_enviar}")
                elif solicita_todos_closets(msg.texto):
                    categorias_a_enviar = list(CATEGORIAS_CLOSETS)
                    modo_muestra = True
                    logger.info(f"Categoría detectada: todos los closets {categorias_a_enviar}")
                else:
                    categoria = detectar_categoria(msg.texto)
                    categorias_a_enviar = [categoria] if categoria else []
                    modo_muestra = False
                    logger.info(f"Categoría detectada: {categoria}")

                if not categorias_a_enviar:
                    # Pidió fotos pero sin especificar categoría: ofrecemos las opciones.
                    logger.info(f"Solicitud sin categoría de {msg.telefono}; se ofrecen opciones")
                    opciones = "\n".join(f"• {c.replace('_', ' ')}" for c in CATEGORIAS)
                    mensaje_opciones = (
                        "¡Con gusto! ¿Qué te gustaría ver? Tenemos:\n"
                        f"{opciones}\n\n"
                        "Escríbeme la categoría que prefieras, o pídeme el catálogo completo."
                    )
                    await guardar_mensaje(msg.telefono, "user", msg.texto)
                    await guardar_mensaje(msg.telefono, "assistant", mensaje_opciones)
                    await proveedor.enviar_mensaje(msg.telefono, mensaje_opciones)
                    continue

                # Construir la lista final de imágenes (URL + caption) respetando el
                # límite máximo por respuesta para no hacer spam.
                seleccion = []  # lista de (url, caption)
                for categoria in categorias_a_enviar:
                    urls = await obtener_imagenes(categoria, max_resultados=MAX_IMAGENES_POR_RESPUESTA)
                    logger.info(f"Imágenes encontradas en '{categoria}': {len(urls)}")
                    caption = f"Modelos de {categoria.replace('_', ' ')}"
                    # En modo muestra tomamos 1 por carpeta; si no, todas las de la carpeta.
                    urls_categoria = urls[:1] if modo_muestra else urls
                    for url_imagen in urls_categoria:
                        seleccion.append((url_imagen, caption))

                # Recortar al máximo permitido por respuesta.
                seleccion = seleccion[:MAX_IMAGENES_POR_RESPUESTA]

                # Enviar las imágenes seleccionadas.
                logger.info(
                    f"Intentando enviar {len(seleccion)} imágenes a {msg.telefono} "
                    f"(máx {MAX_IMAGENES_POR_RESPUESTA}) de: {categorias_a_enviar}"
                )
                enviadas = 0
                for url_imagen, caption in seleccion:
                    if await proveedor.enviar_imagen(msg.telefono, url_imagen, caption):
                        enviadas += 1
                logger.info(f"Imágenes enviadas a {msg.telefono}: {enviadas}/{len(seleccion)}")

                # Si se envió al menos una imagen, NO generamos respuesta de texto:
                # los únicos mensajes que recibe el usuario son las imágenes con su caption.
                if enviadas > 0:
                    logger.info(f"Imágenes enviadas correctamente a {msg.telefono}")
                    await guardar_mensaje(msg.telefono, "user", msg.texto)
                    await guardar_mensaje(
                        msg.telefono,
                        "assistant",
                        f"[{enviadas} imágenes enviadas: {', '.join(categorias_a_enviar)}]",
                    )
                    logger.info(
                        f"Se omite generación de respuesta porque ya se enviaron imágenes a {msg.telefono}"
                    )
                    continue

                # Si no se pudo enviar ninguna imagen, seguimos al flujo normal de texto.
                logger.warning(
                    f"No se enviaron imágenes ({categorias_a_enviar}) a {msg.telefono}; "
                    "se continúa con respuesta de texto"
                )

            # Generar respuesta con Claude
            respuesta = await generar_respuesta(msg.texto, historial)

            # Guardar mensaje del usuario Y respuesta del agente en memoria
            await guardar_mensaje(msg.telefono, "user", msg.texto)
            await guardar_mensaje(msg.telefono, "assistant", respuesta)

            # Enviar respuesta por WhatsApp via el proveedor
            await proveedor.enviar_mensaje(msg.telefono, respuesta)

            logger.info(f"Respuesta a {msg.telefono}: {respuesta}")

        return {"status": "ok"}

    except Exception as e:
        logger.error(f"Error en webhook: {e}")
        raise HTTPException(status_code=500, detail=str(e))
