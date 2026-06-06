# agent/brain.py — Cerebro del agente: conexión con Claude API
# Generado por AgentKit

"""
Lógica de IA del agente. Lee el system prompt de prompts.yaml
y genera respuestas usando la API de Anthropic Claude.
"""

import os
import re
import yaml
import logging
from anthropic import AsyncAnthropic
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger("agentkit")

# Marcador interno que main.py guarda en memoria cuando se envían imágenes
# (ej: "[2 imágenes enviadas: closet_6_puertas]"). NUNCA debe llegar al modelo:
# Claude lo ve en su propio historial y lo imita, devolviéndolo como texto al
# cliente (en vez de enviar imágenes reales). Lo reescribimos como nota natural.
_MARCADOR_IMAGENES = re.compile(
    r"^\[\s*\d*\s*im[aá]genes?\s+enviadas?:.*\]$",
    re.IGNORECASE | re.DOTALL,
)


def _sanear_para_modelo(contenido: str) -> str:
    """Reemplaza marcadores internos por una nota natural que el modelo no imita."""
    if contenido and _MARCADOR_IMAGENES.match(contenido.strip()):
        return "Le envié al cliente las fotos del catálogo que pidió."
    return contenido

# Cliente de Anthropic
client = AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))


def cargar_config_prompts() -> dict:
    """Lee toda la configuración desde config/prompts.yaml."""
    try:
        with open("config/prompts.yaml", "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        logger.error("config/prompts.yaml no encontrado")
        return {}


def cargar_system_prompt() -> str:
    """Lee el system prompt desde config/prompts.yaml."""
    config = cargar_config_prompts()
    return config.get("system_prompt", "Eres un asistente útil. Responde en español.")


def obtener_mensaje_error() -> str:
    """Retorna el mensaje de error configurado en prompts.yaml."""
    config = cargar_config_prompts()
    return config.get("error_message", "Lo siento, estoy teniendo problemas técnicos. Por favor intenta de nuevo en unos minutos.")


def obtener_mensaje_fallback() -> str:
    """Retorna el mensaje de fallback configurado en prompts.yaml."""
    config = cargar_config_prompts()
    return config.get("fallback_message", "Disculpa, no entendí tu mensaje. ¿Podrías reformularlo?")


async def generar_respuesta(mensaje: str, historial: list[dict]) -> str:
    """
    Genera una respuesta usando Claude API.

    Args:
        mensaje: El mensaje nuevo del usuario
        historial: Lista de mensajes anteriores [{"role": "user/assistant", "content": "..."}]

    Returns:
        La respuesta generada por Claude
    """
    # Si el mensaje es muy corto o vacío, usar fallback
    if not mensaje or len(mensaje.strip()) < 2:
        return obtener_mensaje_fallback()

    system_prompt = cargar_system_prompt()

    # Construir mensajes para la API.
    # Saneamos los marcadores internos (ej "[2 imágenes enviadas: ...]") para que
    # el modelo NO los imite y los devuelva como texto al cliente.
    mensajes = []
    for msg in historial:
        contenido = msg["content"]
        if msg["role"] == "assistant":
            contenido = _sanear_para_modelo(contenido)
        mensajes.append({
            "role": msg["role"],
            "content": contenido
        })

    # Agregar el mensaje actual
    mensajes.append({
        "role": "user",
        "content": mensaje
    })

    try:
        response = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=system_prompt,
            messages=mensajes
        )

        respuesta = response.content[0].text
        logger.info(f"Respuesta generada ({response.usage.input_tokens} in / {response.usage.output_tokens} out)")
        return respuesta

    except Exception as e:
        logger.error(f"Error Claude API: {e}")
        return obtener_mensaje_error()
