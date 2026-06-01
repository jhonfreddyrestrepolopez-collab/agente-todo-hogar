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
from agent.cloudinary_images import detectar_categoria, obtener_imagenes, CATEGORIAS

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
            solicita_imagen = any(palabra in texto_lower for palabra in palabras_imagen)

            if solicita_imagen:
                logger.info(f"Solicitud de imagen detectada de {msg.telefono}: {msg.texto}")

                # Detectar a qué categoría/carpeta de Cloudinary se refiere.
                categoria = detectar_categoria(msg.texto)

                if not categoria:
                    # Pidió fotos pero sin especificar categoría: ofrecemos las opciones.
                    logger.info(f"Solicitud sin categoría de {msg.telefono}; se ofrecen opciones")
                    opciones = "\n".join(f"• {c.replace('_', ' ')}" for c in CATEGORIAS)
                    mensaje_opciones = (
                        "¡Con gusto! ¿Qué te gustaría ver? Tenemos:\n"
                        f"{opciones}\n\n"
                        "Escríbeme la categoría que prefieras."
                    )
                    await guardar_mensaje(msg.telefono, "user", msg.texto)
                    await guardar_mensaje(msg.telefono, "assistant", mensaje_opciones)
                    await proveedor.enviar_mensaje(msg.telefono, mensaje_opciones)
                    continue

                # Consultar las imágenes de esa carpeta en Cloudinary.
                logger.info(f"Intentando enviar imágenes de '{categoria}' a {msg.telefono}")
                urls = await obtener_imagenes(categoria)
                caption = f"Modelos de {categoria.replace('_', ' ')}"

                enviadas = 0
                for url_imagen in urls:
                    if await proveedor.enviar_imagen(msg.telefono, url_imagen, caption):
                        enviadas += 1
                logger.info(
                    f"Resultado de enviar_imagen a {msg.telefono}: "
                    f"{enviadas}/{len(urls)} imágenes de '{categoria}'"
                )

                # Si se envió al menos una imagen, NO generamos respuesta de texto:
                # los únicos mensajes que recibe el usuario son las imágenes con su caption.
                if enviadas > 0:
                    logger.info(f"Imágenes enviadas correctamente a {msg.telefono}")
                    await guardar_mensaje(msg.telefono, "user", msg.texto)
                    await guardar_mensaje(
                        msg.telefono, "assistant", f"[{enviadas} imágenes enviadas: {categoria}]"
                    )
                    logger.info(
                        f"Se omite generación de respuesta porque ya se enviaron imágenes a {msg.telefono}"
                    )
                    continue

                # Si no se pudo enviar ninguna imagen, seguimos al flujo normal de texto.
                logger.warning(
                    f"No se enviaron imágenes de '{categoria}' a {msg.telefono}; "
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
