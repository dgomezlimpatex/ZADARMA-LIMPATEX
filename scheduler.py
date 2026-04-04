"""
scheduler.py — Limpatex / Zadarma shift scheduler (PRODUCCIÓN)
"""

import hashlib
import hmac
import base64
import json
import logging
import os
from urllib.parse import urlencode
from datetime import datetime, timezone, timedelta
from typing import Optional

import requests
from dotenv import load_dotenv
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# ─── CARGAR .ENV ─────────────────────────────────────────

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.path.join(BASE_DIR, ".env")
load_dotenv(ENV_FILE)

# ─── CONFIGURACIÓN ───────────────────────────────────────

ZADARMA_KEY = os.getenv("ZADARMA_KEY", "").strip()
ZADARMA_SECRET = os.getenv("ZADARMA_SECRET", "").strip()

# Qué hacer cuando NO hay turno:
# - off  => quitar desvío
# - keep => dejar el último desvío tal como está
NO_SHIFT_MODE = os.getenv("NO_SHIFT_MODE", "off").strip().lower()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

RECEPTORAS = {
    "turquoise": "104",
    "marina30": "105",
}

EXTENSIONES_TRABAJADORES = {
    "dani": "100",
    "sara": "101",
    "kia": "102",
    "tamara": "106",
    "santi": "110",
    "sofia": "111",
}

# Semana base de la rotación de Turquoise:
# Semana A:
# - Santi: L-V 10-14 y S-D 10-23
# - Sofía: L-V 14-23
#
# Semana B:
# - Sofía: L-V 10-14 y S-D 10-23
# - Santi: L-V 14-23
ROTACION_TURQUOISE_BASE = datetime(2026, 3, 23).date()

STATE_FILE = os.path.join(BASE_DIR, "state.json")
LOG_FILE = os.path.join(BASE_DIR, "scheduler.log")

# ─── ZONA HORARIA ────────────────────────────────────────

def get_spain_tz():
    try:
        return ZoneInfo("Europe/Madrid")
    except ZoneInfoNotFoundError:
        # Fallback temporal para que no reviente en Windows si falta tzdata.
        # OJO: esto no ajusta automáticamente horario de verano.
        return timezone(timedelta(hours=1))

TZ = get_spain_tz()

# ─── LOGGING ─────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("limpatex")

# ─── DEFINICIÓN DE TURNOS ────────────────────────────────

class Turno:
    def __init__(self, empleado, clientes, dias, inicio, fin, nocturno=False):
        self.empleado = empleado
        self.clientes = clientes
        self.dias = dias
        self.inicio = inicio
        self.fin = fin
        self.nocturno = nocturno

TURNOS = [
    # DANI
    Turno("dani", ["turquoise", "marina30"], [0, 1, 2, 3, 4], 7.0, 10.0),
    Turno("dani", ["turquoise"], [6], 8.0, 10.0),  # domingo extra para Turquoise
    Turno("dani", ["marina30"], [0, 1, 2, 3, 4, 5, 6], 16.0, 19.0),
    Turno("dani", ["marina30"], [6], 19.0, 23.0),
    Turno("dani", ["turquoise", "marina30"], [5], 8.0, 23.0),  # sábado hasta 23
    Turno("dani", ["marina30"], [0, 1, 2, 3, 4, 5, 6], 23.0, 8.0, nocturno=True),

    # SARA
    Turno("sara", ["marina30"], [0, 1, 2, 3, 4], 19.0, 23.0),

    # KIA
    Turno("kia", ["turquoise"], [6, 0, 1, 2, 3], 23.0, 7.0, nocturno=True),

    # DANI extra para viernes y sábado noche en Turquoise
    Turno("dani", ["turquoise"], [4, 5], 23.0, 8.0, nocturno=True),
]

# ─── FIRMA ZADARMA API ───────────────────────────────────

def _firma_zadarma(method: str, params: dict | None = None) -> str:
    if params is None:
        params = {}

    params_str = urlencode(sorted(params.items()))
    md5_params = hashlib.md5(params_str.encode("utf-8")).hexdigest()
    data = method + params_str + md5_params

    hmac_hex = hmac.new(
        ZADARMA_SECRET.encode("utf-8"),
        data.encode("utf-8"),
        hashlib.sha1,
    ).hexdigest()

    sign = base64.b64encode(hmac_hex.encode("utf-8")).decode("utf-8")
    return f"{ZADARMA_KEY}:{sign}"

def zadarma_get(method: str, params: dict | None = None) -> dict:
    if params is None:
        params = {}

    auth = _firma_zadarma(method, params)
    url = f"https://api.zadarma.com{method}"

    resp = requests.get(
        url,
        params=params,
        headers={
            "Authorization": auth,
            "Accept": "application/json",
        },
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()

def zadarma_post(method: str, params: dict) -> dict:
    auth = _firma_zadarma(method, params)
    url = f"https://api.zadarma.com{method}"

    resp = requests.post(
        url,
        data=params,
        headers={
            "Authorization": auth,
            "Accept": "application/json",
        },
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()

# ─── TEST DE API ─────────────────────────────────────────

def test_auth() -> bool:
    try:
        result = zadarma_get("/v1/info/balance/", {})
        if result.get("status") == "success":
            log.info(f"API OK | balance={result.get('balance')} {result.get('currency')}")
            return True
        log.error(f"API respondió sin éxito: {result}")
        return False
    except Exception as e:
        log.error(f"Error probando API: {e}")
        return False

# ─── DESVÍOS ─────────────────────────────────────────────

def obtener_desvio_actual(ext_receptora: str) -> dict:
    try:
        result = zadarma_get("/v1/pbx/redirection/", {"pbx_number": ext_receptora})
        return result
    except Exception as e:
        log.error(f"    ext {ext_receptora} error consultando desvío actual: {e}")
        return {}

def set_desvio_extension(ext_receptora: str, ext_destino: str) -> bool:
    try:
        antes = obtener_desvio_actual(ext_receptora)
        log.info(f"    Estado antes {ext_receptora}: {antes}")

        params = {
            "pbx_number": ext_receptora,
            "status": "on",
            "type": "sip",
            "destination": ext_destino,
            "condition": "always",
        }

        result = zadarma_post("/v1/pbx/redirection/", params)
        log.info(f"    Respuesta cambio {ext_receptora}: {result}")

        despues = obtener_desvio_actual(ext_receptora)
        log.info(f"    Estado después {ext_receptora}: {despues}")

        destino_real = str(despues.get("destination", "")).strip()

        if (
            despues.get("current_status") == "on"
            and destino_real == str(ext_destino)
        ):
            log.info(f"    ext {ext_receptora} → ext {ext_destino} [CONFIRMADO]")
            return True

        log.error(
            f"    ext {ext_receptora} → ext {ext_destino} [NO CONFIRMADO] "
            f"respuesta={result} estado_final={despues}"
        )
        return False

    except Exception as e:
        log.error(f"    ext {ext_receptora} error API: {e}")
        return False

def quitar_desvio_extension(ext_receptora: str) -> bool:
    try:
        antes = obtener_desvio_actual(ext_receptora)
        log.info(f"    Estado antes quitar {ext_receptora}: {antes}")

        params = {
            "pbx_number": ext_receptora,
            "status": "off",
        }

        result = zadarma_post("/v1/pbx/redirection/", params)
        log.info(f"    Respuesta quitar {ext_receptora}: {result}")

        despues = obtener_desvio_actual(ext_receptora)
        log.info(f"    Estado después quitar {ext_receptora}: {despues}")

        # Si al final está en off, para nosotros está bien,
        # aunque Zadarma devuelva "error" porque ya estaba apagado.
        if despues.get("current_status") == "off":
            log.info(f"    ext {ext_receptora} desvío quitado [CONFIRMADO]")
            return True

        log.error(
            f"    ext {ext_receptora} quitar desvío [NO CONFIRMADO] "
            f"respuesta={result} estado_final={despues}"
        )
        return False

    except Exception as e:
        log.error(f"    ext {ext_receptora} error quitando desvío: {e}")
        return False

# ─── LÓGICA DE TURNOS ────────────────────────────────────

def hora_float(dt: datetime) -> float:
    return dt.hour + dt.minute / 60.0

def turno_activo(turno: Turno, ahora: datetime) -> bool:
    h = hora_float(ahora)
    wd = ahora.weekday()

    if not turno.nocturno:
        return wd in turno.dias and turno.inicio <= h < turno.fin

    ventana_a = (wd in turno.dias) and (h >= turno.inicio)
    ayer = (wd - 1) % 7
    ventana_b = (ayer in turno.dias) and (h < turno.fin)
    return ventana_a or ventana_b

def semana_rotacion_turquoise(fecha):
    """
    Devuelve 0 para semana A, 1 para semana B
    """
    delta_dias = (fecha - ROTACION_TURQUOISE_BASE).days
    semanas = delta_dias // 7
    return semanas % 2

def empleado_turquoise_rotativo(ahora: datetime) -> Optional[str]:
    """
    Gestiona el horario diurno de Turquoise:
    - Semana A:
        Santi 10-14 y sábado/domingo 10-23
        Sofía 14-23 lunes-viernes
    - Semana B:
        Sofía 10-14 y sábado/domingo 10-23
        Santi 14-23 lunes-viernes
    """
    wd = ahora.weekday()   # 0=lunes ... 6=domingo
    h = hora_float(ahora)
    semana = semana_rotacion_turquoise(ahora.date())

    # Fuera del horario diurno de Turquoise
    if h < 10.0 or h >= 23.0:
        return None

    # Semana A
    if semana == 0:
        # Lunes a viernes
        if wd in [0, 1, 2, 3, 4]:
            if 10.0 <= h < 14.0:
                return "santi"
            if 14.0 <= h < 23.0:
                return "sofia"

        # Sábado y domingo
        if wd in [5, 6]:
            if 10.0 <= h < 23.0:
                return "santi"

    # Semana B
    else:
        # Lunes a viernes
        if wd in [0, 1, 2, 3, 4]:
            if 10.0 <= h < 14.0:
                return "sofia"
            if 14.0 <= h < 23.0:
                return "santi"

        # Sábado y domingo
        if wd in [5, 6]:
            if 10.0 <= h < 23.0:
                return "sofia"

    return None

def empleado_de_turno(cliente: str, ahora: datetime) -> Optional[str]:
    # Turquoise primero mira su rotación diurna propia
    if cliente == "turquoise":
        emp_rotativo = empleado_turquoise_rotativo(ahora)
        if emp_rotativo:
            return emp_rotativo

    # Resto de lógica general
    for turno in TURNOS:
        if cliente in turno.clientes and turno_activo(turno, ahora):
            return turno.empleado
    return None

# ─── ESTADO ──────────────────────────────────────────────

def cargar_estado() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def guardar_estado(estado: dict):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(estado, f, indent=2, ensure_ascii=False)
    except Exception as e:
        log.warning(f"No se pudo guardar el estado: {e}")

# ─── TELEGRAM ────────────────────────────────────────────

def alerta_telegram(msg: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return

    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": f"Limpatex scheduler:\n{msg}"},
            timeout=10,
        )
    except Exception:
        pass

# ─── MAIN ────────────────────────────────────────────────

def main():
    if not ZADARMA_KEY or not ZADARMA_SECRET:
        log.error("Faltan ZADARMA_KEY o ZADARMA_SECRET en el archivo .env")
        return

    if NO_SHIFT_MODE not in {"off", "keep"}:
        log.error("NO_SHIFT_MODE debe ser 'off' o 'keep'")
        return

    if not test_auth():
        return

    ahora = datetime.now(TZ)
    log.info(f"=== Comprobación de turno {ahora.strftime('%A %d/%m/%Y %H:%M')} ===")

    estado = cargar_estado()
    cambios = False
    errores = []

    for cliente, ext_receptora in RECEPTORAS.items():
        empleado = empleado_de_turno(cliente, ahora)

        if empleado is None:
            msg = f"[{cliente.upper()}] SIN TURNO ASIGNADO a las {ahora.strftime('%H:%M')}"
            log.warning(f"  {msg}")

            if NO_SHIFT_MODE == "off":
                log.info(f"  Quitando desvío de ext {ext_receptora}...")
                ok = quitar_desvio_extension(ext_receptora)
                if ok:
                    estado[ext_receptora] = None
                    cambios = True
                else:
                    errores.append(f"[{cliente.upper()}] Error al quitar desvío")
            continue

        ext_destino = EXTENSIONES_TRABAJADORES[empleado]
        log.info(f"  [{cliente.upper()}] turno activo: {empleado} (ext {ext_destino})")

        if estado.get(ext_receptora) != ext_destino:
            log.info(f"  Cambiando desvío de ext {ext_receptora}...")
            ok = set_desvio_extension(ext_receptora, ext_destino)
            if ok:
                estado[ext_receptora] = ext_destino
                cambios = True
            else:
                errores.append(f"[{cliente.upper()}] Error al cambiar desvío a {empleado}")
        else:
            log.info(f"  Sin cambios (ya apunta a ext {ext_destino})")

    if cambios:
        guardar_estado(estado)

    if errores:
        alerta_telegram("\n".join(errores))

if __name__ == "__main__":
    main()
