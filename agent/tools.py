# agent/tools.py — Herramientas del agente
# Generado por AgentKit

"""
Herramientas específicas de TODO HOGAR FLORENCIA.
Estas funciones extienden las capacidades del agente más allá de responder texto.
Casos de uso elegidos: Preguntas frecuentes, Tomar pedidos, Soporte post-venta.
"""

import os
import json
import yaml
import logging
from datetime import datetime

logger = logging.getLogger("agentkit")

# Carpeta donde se guardan pedidos y tickets (texto plano, fácil de revisar)
DATOS_DIR = "knowledge"
PEDIDOS_FILE = os.path.join("config", "pedidos.json")
TICKETS_FILE = os.path.join("config", "tickets.json")


def cargar_info_negocio() -> dict:
    """Carga la información del negocio desde business.yaml."""
    try:
        with open("config/business.yaml", "r", encoding="utf-8") as f:
            return yaml.safe_load(f)
    except FileNotFoundError:
        logger.error("config/business.yaml no encontrado")
        return {}


def obtener_horario() -> dict:
    """Retorna el horario de atención del negocio."""
    info = cargar_info_negocio()
    return {
        "horario": info.get("negocio", {}).get("horario", "No disponible"),
        "esta_abierto": True,  # TODO: calcular según hora actual y horario
    }


def buscar_en_knowledge(consulta: str) -> str:
    """
    Busca información relevante en los archivos de texto de /knowledge.
    Retorna el contenido más relevante encontrado.
    (Las imágenes del catálogo no se leen aquí; se incorporaron al system prompt.)
    """
    resultados = []
    knowledge_dir = "knowledge"

    if not os.path.exists(knowledge_dir):
        return "No hay archivos de conocimiento disponibles."

    for archivo in os.listdir(knowledge_dir):
        ruta = os.path.join(knowledge_dir, archivo)
        if archivo.startswith(".") or not os.path.isfile(ruta):
            continue
        try:
            with open(ruta, "r", encoding="utf-8") as f:
                contenido = f.read()
                if consulta.lower() in contenido.lower():
                    resultados.append(f"[{archivo}]: {contenido[:500]}")
        except (UnicodeDecodeError, IOError):
            # Se ignoran binarios (imágenes, etc.)
            continue

    if resultados:
        return "\n---\n".join(resultados)
    return "No encontré información específica sobre eso en mis archivos."


# ════════════════════════════════════════════════════════════
# TOMAR PEDIDOS
# ════════════════════════════════════════════════════════════

def _leer_json(ruta: str) -> list:
    """Lee un archivo JSON de lista; retorna [] si no existe."""
    try:
        with open(ruta, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _guardar_json(ruta: str, datos: list):
    """Guarda una lista en un archivo JSON."""
    os.makedirs(os.path.dirname(ruta), exist_ok=True)
    with open(ruta, "w", encoding="utf-8") as f:
        json.dump(datos, f, ensure_ascii=False, indent=2)


def registrar_pedido(telefono: str, nombre: str, producto: str, acabado: str,
                     cantidad: int, ciudad: str, notas: str = "") -> dict:
    """
    Registra un pedido de un cliente.

    Args:
        telefono: Número del cliente
        nombre: Nombre del cliente
        producto: Mueble solicitado (ej: "Clóset 3 puertas")
        acabado: Color/acabado en melamina (ej: "roble claro")
        cantidad: Unidades
        ciudad: Ciudad/dirección para entrega o recogida
        notas: Observaciones adicionales

    Returns:
        Diccionario con el pedido registrado y su número.
    """
    pedidos = _leer_json(PEDIDOS_FILE)
    pedido = {
        "pedido_id": f"PED-{len(pedidos) + 1:04d}",
        "telefono": telefono,
        "nombre": nombre,
        "producto": producto,
        "acabado": acabado,
        "cantidad": cantidad,
        "ciudad": ciudad,
        "notas": notas,
        "estado": "pendiente",
        "fecha": datetime.utcnow().isoformat(),
    }
    pedidos.append(pedido)
    _guardar_json(PEDIDOS_FILE, pedidos)
    logger.info(f"Pedido registrado: {pedido['pedido_id']} de {telefono}")
    return pedido


def consultar_pedidos(telefono: str) -> list[dict]:
    """Retorna los pedidos de un cliente por su número."""
    return [p for p in _leer_json(PEDIDOS_FILE) if p.get("telefono") == telefono]


# ════════════════════════════════════════════════════════════
# SOPORTE POST-VENTA
# ════════════════════════════════════════════════════════════

def crear_ticket(telefono: str, nombre: str, problema: str) -> dict:
    """
    Crea un ticket de soporte post-venta para escalar al equipo.

    Args:
        telefono: Número del cliente
        nombre: Nombre del cliente
        problema: Descripción del problema o reclamo

    Returns:
        Diccionario con el ticket creado y su número.
    """
    tickets = _leer_json(TICKETS_FILE)
    ticket = {
        "ticket_id": f"TIC-{len(tickets) + 1:04d}",
        "telefono": telefono,
        "nombre": nombre,
        "problema": problema,
        "estado": "abierto",
        "fecha": datetime.utcnow().isoformat(),
    }
    tickets.append(ticket)
    _guardar_json(TICKETS_FILE, tickets)
    logger.info(f"Ticket creado: {ticket['ticket_id']} de {telefono}")
    return ticket


def consultar_ticket(ticket_id: str) -> dict:
    """Consulta el estado de un ticket por su ID."""
    for t in _leer_json(TICKETS_FILE):
        if t.get("ticket_id") == ticket_id:
            return t
    return {"error": f"No se encontró el ticket {ticket_id}"}
