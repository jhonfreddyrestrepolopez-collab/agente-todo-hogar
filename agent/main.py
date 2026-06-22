# agent/main.py — Servidor FastAPI + Webhook de WhatsApp
# Generado por AgentKit

"""
Servidor principal del agente de WhatsApp.
Funciona con cualquier proveedor gracias a la capa de providers (aquí: Whapi.Cloud).
"""

import os
import re
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
    obtener_productos,
    obtener_estado_sync,
    info_base_datos,
    guardar_ultima_categoria,
    obtener_ultima_categoria,
)
from agent.providers import obtener_proveedor
from agent.cloudinary_images import (
    detectar_categoria,
    solicita_catalogo_completo,
    solicita_todos_closets,
    es_reclamo_fotos,
    listar_carpetas,
    listar_imagenes,
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
# Token de administrador para el comando manual de sincronización del catálogo.
ADMIN_TOKEN = (os.getenv("ADMIN_TOKEN") or "").strip()


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


def _verificar_admin(request: Request):
    """Valida el token de administrador (query ?token= o header X-Admin-Token)."""
    if not ADMIN_TOKEN:
        raise HTTPException(status_code=503, detail="ADMIN_TOKEN no configurado en el servidor")
    token = request.query_params.get("token") or request.headers.get("x-admin-token")
    if token != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="Token de administrador inválido")


@app.api_route("/admin/sync_catalogo", methods=["GET", "POST"])
async def admin_sync_catalogo(request: Request):
    """
    Comando MANUAL de administrador: sincroniza el catálogo con Cloudinary
    (nuevas, modificadas y eliminadas) y devuelve el resumen de cambios.
    Acepta GET (cómodo desde el navegador) y POST. Requiere token de administrador.
    """
    _verificar_admin(request)
    logger.info("sync_catalogo solicitado por administrador")
    resumen = await sincronizar_todo()
    return {"status": "ok", "sincronizacion": resumen}


@app.get("/diagnostico/from_me")
async def diagnostico_from_me(request: Request):
    """
    Metadatos (sin texto) de los últimos mensajes salientes (from_me) y cómo se
    clasificaron: humano / eco_bot / ignorado. Útil para ver qué 'source' usa
    Whapi. Requiere token de administrador.
    """
    _verificar_admin(request)
    fn = getattr(proveedor, "ultimos_from_me", None)
    return {"ultimos_from_me": fn() if callable(fn) else "no disponible para este proveedor"}


@app.get("/diagnostico/catalogo")
async def diagnostico_catalogo():
    """Resumen del estado real del catálogo en producción (solo lectura)."""
    carpetas = await listar_carpetas()
    productos = await obtener_productos(None)

    # Conteos por carpeta: imágenes en Cloudinary vs productos analizados en BD.
    por_categoria = {}
    for cat in carpetas:
        imgs = await listar_imagenes(cat)
        prods_cat = [p for p in productos if p["categoria"] == cat]
        por_categoria[cat] = {
            "imagenes_en_cloudinary": len(imgs),
            "productos_analizados": len(prods_cat),
            "pendientes": max(0, len(imgs) - len(prods_cat)),
        }

    def _tiene_medida(p):
        return p["alto_cm"] is not None and p["ancho_cm"] is not None

    def _fallido(p):
        return (p["nombre"] is None and p["alto_cm"] is None
                and p["ancho_cm"] is None and p["precio"] is None)

    total_imgs = sum(c["imagenes_en_cloudinary"] for c in por_categoria.values())

    return {
        "base_de_datos": info_base_datos(),
        "ultima_sincronizacion": await obtener_estado_sync(),
        "1_carpetas_detectadas": len(carpetas),
        "2_nombres_carpetas": carpetas,
        "3_imagenes_por_carpeta": {k: v["imagenes_en_cloudinary"] for k, v in por_categoria.items()},
        "4_productos_analizados": len(productos),
        "5_con_alto_y_ancho": sum(1 for p in productos if _tiene_medida(p)),
        "5b_con_alguna_medida": sum(1 for p in productos if p["alto_cm"] is not None or p["ancho_cm"] is not None),
        "6_con_precio": sum(1 for p in productos if p["precio"] is not None),
        "7_sin_analizar_pendientes": sum(c["pendientes"] for c in por_categoria.values()),
        "7b_analisis_fallido": sum(1 for p in productos if _fallido(p)),
        "total_imagenes_en_cloudinary": total_imgs,
        "detalle_por_categoria": por_categoria,
        "productos": [
            {
                "categoria": p["categoria"],
                "nombre": p["nombre"],
                "alto_cm": p["alto_cm"],
                "ancho_cm": p["ancho_cm"],
                "precio": p["precio"],
            }
            for p in productos
        ],
    }


@app.get("/webhook")
async def webhook_verificacion(request: Request):
    """Verificación GET del webhook (no-op para Whapi; útil si cambias de proveedor)."""
    resultado = await proveedor.validar_webhook(request)
    if resultado is not None:
        return PlainTextResponse(str(resultado))
    return {"status": "ok"}


# ── TRANSFERENCIA A HUMANO ───────────────────────────────────────────────────
# 1) El CLIENTE pide un asesor humano. Palabras sueltas con riesgo de falso positivo
#    (persona, asesor, vendedor...) se buscan con límites de palabra; las frases
#    claras se buscan como subcadena.
_PIDE_HUMANO_RE = re.compile(
    r"\b(asesor|asesora|asesores|persona|humano|humana|vendedor|vendedora|"
    r"encargad[oa]|administrador|administradora|gerente)\b",
    re.IGNORECASE,
)
_PIDE_HUMANO_FRASES = (
    "alguien que me atienda", "hablar con alguien", "hablar con una persona",
    "quiero hablar con alguien", "quiero hablar con una persona",
    "que me atienda una persona", "atencion humana", "atención humana",
    "agente humano", "con un asesor", "con una persona", "atienda alguien",
)


def cliente_pide_humano(texto: str) -> bool:
    """True si el cliente pide hablar con un asesor / persona / humano."""
    t = (texto or "").lower()
    if any(f in t for f in _PIDE_HUMANO_FRASES):
        return True
    return bool(_PIDE_HUMANO_RE.search(t))


# 2) El BOT escribe algo que implica que una PERSONA continuará la conversación.
#    Cuando el bot diga una de estas frases, activamos modo humano y queda en silencio.
_FRASES_HANDOFF_BOT = (
    "voy a consultar", "voy a verificar", "voy a confirmar",
    "permíteme consultar", "permiteme consultar", "permítame consultar", "permitame consultar",
    "déjame consultar", "dejame consultar", "déjame confirmar", "dejame confirmar",
    "déjame confirmarlo", "dejame confirmarlo",
    "consultar con el equipo", "consultarlo con el equipo", "confirmarlo con el equipo",
    "lo confirmo con el equipo", "con el equipo", "verificar esta información",
    "verificar esta informacion", "confirmar el dato",
    "conectarte con alguien", "con alguien de nuestro equipo", "te conecto con",
    "escalar al equipo", "lo escalo",
)


def respuesta_implica_handoff(texto: str) -> bool:
    """True si la respuesta del bot implica que un humano continuará la conversación."""
    t = (texto or "").lower()
    return any(f in t for f in _FRASES_HANDOFF_BOT)


# Mensaje al cliente cuando NO se pudo entregar NINGUNA foto por Whapi (error,
# sent:false, respuesta vacía o sin URL válida). Se acompaña de un log de REPORTE.
DISCULPA_FOTOS = (
    "Disculpa, tuve un problema enviando las fotos. "
    "Ya lo reporté para enviártelas manualmente."
)


async def _enviar_fotos_catalogo(prov, telefono, texto, categorias, modo_muestra,
                                  forzar_reenvio=False) -> dict:
    """
    Selecciona y envía las fotos del catálogo, validando la respuesta REAL de
    Whapi por cada imagen (enviar_imagen solo devuelve True si Whapi confirma el
    envío con sent:true / id de mensaje). Registra logs detallados.

    Devuelve {"enviadas", "fallidas", "total", "sin_nuevas"}. NO escribe en el
    historial ni envía texto: el llamador decide qué responder según el resultado.
    """
    seleccion = await elegir_imagenes(
        texto=texto, categorias=categorias, telefono=telefono,
        modo_muestra=modo_muestra, forzar_reenvio=forzar_reenvio,
    )
    productos = seleccion["productos"]
    urls = [p.get("image_url") for p in productos]
    logger.info(
        "FOTOS | cliente=%s | categoria=%s | reenvio=%s | URLs_encontradas=%s",
        telefono, categorias, forzar_reenvio, urls,
    )

    enviadas = 0
    fallidas = 0
    for p in productos:
        url = p.get("image_url")
        caption = caption_producto(p)
        if not url:
            fallidas += 1
            logger.error("FOTO SIN URL | cliente=%s | public_id=%s", telefono, p.get("public_id"))
            continue
        ok = await prov.enviar_imagen(telefono, url, caption)
        if ok:
            enviadas += 1
            await registrar_imagen_enviada(telefono, p["public_id"])
            logger.info("FOTO ENVIADA OK | cliente=%s | URL=%s", telefono, url)
        else:
            fallidas += 1
            logger.error("FOTO FALLIDA | cliente=%s | URL=%s", telefono, url)

    logger.info(
        "FOTOS RESULTADO | cliente=%s | enviadas=%d | fallidas=%d | total=%d",
        telefono, enviadas, fallidas, len(productos),
    )
    return {
        "enviadas": enviadas,
        "fallidas": fallidas,
        "total": len(productos),
        "sin_nuevas": seleccion["sin_nuevas"],
    }


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

            # 3) Si el CLIENTE pide un asesor humano, el bot deja de responder de
            #    inmediato en este chat y activa modo humano (un humano continúa).
            if cliente_pide_humano(msg.texto):
                await guardar_mensaje(msg.telefono, "user", msg.texto)
                aviso = "¡Claro! En un momento te atiende un asesor de nuestro equipo. 🙌"
                await guardar_mensaje(msg.telefono, "assistant", aviso)
                await proveedor.enviar_mensaje(msg.telefono, aviso)
                hasta = await activar_modo_humano(msg.telefono)
                logger.info(
                    "Cliente pidió ASESOR HUMANO → modo humano para %s hasta %s UTC",
                    msg.telefono, hasta.isoformat(),
                )
                continue
            # ──────────────────────────────────────────────────────────────

            logger.info(f"Mensaje de {msg.telefono}: {msg.texto}")

            # Obtener historial ANTES de guardar el mensaje actual
            # (brain.py agrega el mensaje actual, evitando duplicados)

            historial = await obtener_historial(msg.telefono)
            texto_lower = msg.texto.lower()

            # ── RECLAMO / REENVÍO DE FOTOS ────────────────────────────────
            # Si el cliente dice "no me llegaron", "cuáles fotos", "envíamelas
            # otra vez", etc., NO mostramos el menú: reenviamos las fotos de la
            # ÚLTIMA categoría que pidió (forzando el reenvío de las mismas).
            if es_reclamo_fotos(msg.texto):
                ultima = await obtener_ultima_categoria(msg.telefono)
                logger.info(
                    "RECLAMO/REENVÍO | cliente=%s | last_requested_category=%s",
                    msg.telefono, ultima,
                )
                if ultima and ultima.get("categoria"):
                    cats = (None if ultima["categoria"] == "__NONE__"
                            else ultima["categoria"].split(","))
                    res = await _enviar_fotos_catalogo(
                        proveedor, msg.telefono, msg.texto, cats,
                        ultima["modo_muestra"], forzar_reenvio=True,
                    )
                    await guardar_mensaje(msg.telefono, "user", msg.texto)
                    if res["enviadas"] > 0:
                        nombres = ultima["categoria"].replace(",", ", ").replace(
                            "__NONE__", "catálogo completo")
                        await guardar_mensaje(
                            msg.telefono, "assistant",
                            f"[{res['enviadas']} imágenes enviadas: {nombres}]",
                        )
                    else:
                        logger.error(
                            "REPORTE reenvío fallido | cliente=%s | categoria=%s | "
                            "fallidas=%d/%d",
                            msg.telefono, ultima["categoria"], res["fallidas"], res["total"],
                        )
                        await guardar_mensaje(msg.telefono, "assistant", DISCULPA_FOTOS)
                        await proveedor.enviar_mensaje(msg.telefono, DISCULPA_FOTOS)
                    continue
                # No hay categoría previa registrada: pedimos aclaración BREVE
                # (no el menú completo), para no cambiar de tema.
                aviso = (
                    "¿De cuál producto querías las fotos? Dime la categoría "
                    "(por ejemplo: clóset de 4 puertas) y te las envío enseguida. 📸"
                )
                await guardar_mensaje(msg.telefono, "user", msg.texto)
                await guardar_mensaje(msg.telefono, "assistant", aviso)
                await proveedor.enviar_mensaje(msg.telefono, aviso)
                continue
            # ──────────────────────────────────────────────────────────────

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

                # Recordar la última categoría solicitada (para reclamos/reenvíos).
                token_cat = ("__NONE__" if categorias_a_enviar is None
                             else ",".join(categorias_a_enviar))
                await guardar_ultima_categoria(msg.telefono, token_cat, modo_muestra)
                logger.info(
                    "last_requested_category | cliente=%s | categoria=%s | muestra=%s",
                    msg.telefono, token_cat, modo_muestra,
                )

                # Seleccionar y enviar las fotos, validando la respuesta REAL de Whapi.
                res = await _enviar_fotos_catalogo(
                    proveedor, msg.telefono, msg.texto, categorias_a_enviar, modo_muestra
                )

                # 1) Si se entregó al menos una imagen, NO generamos texto: los únicos
                #    mensajes que recibe el cliente son las fotos con su caption.
                if res["enviadas"] > 0:
                    nombres = token_cat.replace(",", ", ").replace("__NONE__", "catálogo completo")
                    await guardar_mensaje(msg.telefono, "user", msg.texto)
                    await guardar_mensaje(
                        msg.telefono, "assistant",
                        f"[{res['enviadas']} imágenes enviadas: {nombres}]",
                    )
                    continue

                # 2) Había fotos para enviar pero Whapi NO entregó NINGUNA (error,
                #    sent:false o sin URL válida): NO decimos que se enviaron; pedimos
                #    disculpa y dejamos el REPORTE en logs para envío manual.
                if res["total"] > 0:
                    logger.error(
                        "REPORTE envío fallido | cliente=%s | categoria=%s | fallidas=%d/%d",
                        msg.telefono, token_cat, res["fallidas"], res["total"],
                    )
                    await guardar_mensaje(msg.telefono, "user", msg.texto)
                    await guardar_mensaje(msg.telefono, "assistant", DISCULPA_FOTOS)
                    await proveedor.enviar_mensaje(msg.telefono, DISCULPA_FOTOS)
                    continue

                # 3) No quedaban fotos NUEVAS: ya recibió todo lo disponible.
                if res["sin_nuevas"]:
                    aviso = (
                        "Ya te compartí todos los modelos que tengo disponibles de eso 🙌. "
                        "¿Quieres ver otra categoría, o te ayudo con medidas o presupuesto?"
                    )
                    await guardar_mensaje(msg.telefono, "user", msg.texto)
                    await guardar_mensaje(msg.telefono, "assistant", aviso)
                    await proveedor.enviar_mensaje(msg.telefono, aviso)
                    continue

                # 4) El catálogo no tiene esa categoría (sin sincronizar): seguimos al
                #    flujo de texto normal (Claude responde abajo).
                logger.warning(
                    f"Sin productos en catálogo para {categorias_a_enviar} "
                    f"(cliente {msg.telefono}); se continúa con respuesta de texto"
                )

            # Generar respuesta con Claude
            respuesta = await generar_respuesta(msg.texto, historial)

            # Guardar mensaje del usuario Y respuesta del agente en memoria
            await guardar_mensaje(msg.telefono, "user", msg.texto)
            await guardar_mensaje(msg.telefono, "assistant", respuesta)

            # Enviar respuesta por WhatsApp via el proveedor
            await proveedor.enviar_mensaje(msg.telefono, respuesta)

            logger.info(f"Respuesta a {msg.telefono}: {respuesta}")

            # Si la respuesta del bot implica que un HUMANO continuará (ej. "voy a
            # consultar con el equipo"), activamos modo humano: el bot se calla y deja
            # la conversación a una persona.
            if respuesta_implica_handoff(respuesta):
                hasta = await activar_modo_humano(msg.telefono)
                logger.info(
                    "Respuesta del bot implica HANDOFF → modo humano para %s hasta %s UTC",
                    msg.telefono, hasta.isoformat(),
                )

        return {"status": "ok"}

    except Exception as e:
        logger.error(f"Error en webhook: {e}")
        raise HTTPException(status_code=500, detail=str(e))
