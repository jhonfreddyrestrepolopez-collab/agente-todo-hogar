# agent/main.py — Servidor FastAPI + Webhook de WhatsApp
# Generado por AgentKit

"""
Servidor principal del agente de WhatsApp.
Funciona con cualquier proveedor gracias a la capa de providers (aquí: Whapi.Cloud).
"""

import os
import asyncio
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import PlainTextResponse
from dotenv import load_dotenv

from agent.brain import generar_respuesta
from agent.memory import (
    inicializar_db,
    guardar_mensaje,
    obtener_historial,
    activar_modo_humano,
    modo_humano_activo,
    registrar_imagen_enviada,
)
from agent.providers import obtener_proveedor
from agent.cloudinary_images import (
    detectar_categoria,
    solicita_catalogo_completo,
    solicita_todos_closets,
    CATEGORIAS,
    CATEGORIAS_CLOSETS,
)
from agent.catalogo import (
    elegir_imagenes,
    caption_producto,
    sincronizar_todo,
    pidio_medida_o_precio,
)

load_dotenv()

# --- DIAGNÓSTICO TEMPORAL: variables de entorno con "CLOUD" ---
# (Quitar una vez resuelto el tema de las credenciales de Cloudinary.)
print("TODAS LAS VARIABLES CLOUD:", flush=True)
print(sorted([k for k in os.environ.keys() if "CLOUD" in k]), flush=True)
# --- FIN DIAGNÓSTICO TEMPORAL ---

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

    # Sincroniza el catálogo en segundo plano: analiza (una sola vez) las imágenes
    # nuevas de Cloudinary y cachea nombre/alto/ancho/precio en la BD. No bloquea
    # el arranque; las que falten se analizan también al pedirlas por primera vez.
    asyncio.create_task(sincronizar_todo())
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
            # Ignorar mensajes vacíos
            if not msg.texto:
                continue

            # ── MODO HUMANO ───────────────────────────────────────────────
            # 1) Si el mensaje lo envié YO (el dueño, from_me=true), activo o
            #    extiendo el modo humano 24h y dejo que el bot quede en silencio.
            if msg.es_propio:
                hasta = await activar_modo_humano(msg.telefono)
                # Guardo lo que escribí como turno del agente, para mantener el
                # contexto cuando el bot retome la conversación.
                await guardar_mensaje(msg.telefono, "assistant", msg.texto)
                logger.info(
                    f"Modo humano activado/extendido para {msg.telefono} "
                    f"hasta {hasta.isoformat()} UTC (24h desde mi último mensaje)"
                )
                continue

            # 2) Si el chat está en modo humano, el bot NO responde: ni Claude,
            #    ni imágenes, ni texto. Solo guardo el mensaje del cliente para
            #    conservar el historial. A las 24h modo_humano_activo() da False.
            if await modo_humano_activo(msg.telefono):
                await guardar_mensaje(msg.telefono, "user", msg.texto)
                logger.info(
                    f"Modo humano ACTIVO para {msg.telefono}: bot en silencio, "
                    f"no se responde a: {msg.texto}"
                )
                continue
            # ──────────────────────────────────────────────────────────────

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
            # menciona una categoría concreta, pide el catálogo completo, o pide
            # por medida/precio (aunque no nombre la categoría).
            solicita_imagen = (
                any(palabra in texto_lower for palabra in palabras_imagen)
                or detectar_categoria(msg.texto) is not None
                or solicita_catalogo_completo(msg.texto)
                or pidio_medida_o_precio(msg.texto)
            )

            if solicita_imagen:
                logger.info(f"Solicitud de imagen detectada de {msg.telefono}: {msg.texto}")

                # Decidir en qué carpetas buscar:
                #   - Catálogo completo  -> None (TODO el catálogo), como muestra
                #   - Todos los closets  -> carpetas de clóset, como muestra
                #   - Categoría puntual  -> esa carpeta
                #   - Solo medida/precio -> None (TODO el catálogo), sin muestra
                #   - Nada de lo anterior-> ofrecer opciones
                # categorias_a_enviar == None significa "todo el catálogo".
                modo_muestra = False
                ofrecer_opciones = False
                if solicita_catalogo_completo(msg.texto):
                    categorias_a_enviar = None
                    modo_muestra = True
                    logger.info("Categoría detectada: catálogo completo (todas las carpetas)")
                elif solicita_todos_closets(msg.texto):
                    categorias_a_enviar = list(CATEGORIAS_CLOSETS)
                    modo_muestra = True
                    logger.info(f"Categoría detectada: todos los closets {categorias_a_enviar}")
                else:
                    categoria = detectar_categoria(msg.texto)
                    if categoria:
                        categorias_a_enviar = [categoria]
                        logger.info(f"Categoría detectada: {categoria}")
                    elif pidio_medida_o_precio(msg.texto):
                        categorias_a_enviar = None  # buscar en todo el catálogo
                        logger.info("Sin categoría, pero pidió medida/precio: busco en todo el catálogo")
                    else:
                        categorias_a_enviar = []
                        ofrecer_opciones = True

                if ofrecer_opciones:
                    # Pidió fotos pero sin categoría ni medida/precio: ofrecemos opciones.
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

                # Selección inteligente desde el catálogo (datos del análisis visual):
                # solo lo que el cliente no ha recibido, filtrando por medida/precio
                # cuando los menciona.
                seleccion = await elegir_imagenes(
                    texto=msg.texto,
                    categorias=categorias_a_enviar,
                    telefono=msg.telefono,
                    modo_muestra=modo_muestra,
                )
                productos = seleccion["productos"]
                logger.info(
                    f"Selección para {msg.telefono}: {len(productos)} imágenes "
                    f"(medidas={seleccion['medidas']}, presupuesto={seleccion['presupuesto']}, "
                    f"sin_nuevas={seleccion['sin_nuevas']})"
                )

                # Si ya recibió todo lo disponible y no quedan nuevas, avisamos por texto.
                if not productos and seleccion["sin_nuevas"]:
                    aviso = (
                        "Ya te compartí todos los modelos que tengo disponibles de eso 🙌. "
                        "¿Quieres ver otra categoría, o te ayudo con medidas o presupuesto?"
                    )
                    await guardar_mensaje(msg.telefono, "user", msg.texto)
                    await guardar_mensaje(msg.telefono, "assistant", aviso)
                    await proveedor.enviar_mensaje(msg.telefono, aviso)
                    continue

                # Enviar las imágenes seleccionadas (cada una con su caption de catálogo).
                enviadas = 0
                for p in productos:
                    caption = caption_producto(p)
                    if await proveedor.enviar_imagen(msg.telefono, p["image_url"], caption):
                        enviadas += 1
                        await registrar_imagen_enviada(msg.telefono, p["public_id"])
                logger.info(f"Imágenes enviadas a {msg.telefono}: {enviadas}/{len(productos)}")

                # Si se envió al menos una imagen, NO generamos respuesta de texto:
                # los únicos mensajes que recibe el usuario son las imágenes con su caption.
                if enviadas > 0:
                    nombres = ", ".join(p.get("nombre") or p["categoria"] for p in productos)
                    await guardar_mensaje(msg.telefono, "user", msg.texto)
                    await guardar_mensaje(
                        msg.telefono, "assistant", f"[{enviadas} imágenes enviadas: {nombres}]"
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
