#!/usr/bin/env python3
"""
Alerta de descuentos en Cassinelli y Promart -> Telegram (segundo bot, independiente).

Uso:
    python alerta_casinelli.py --test      # mensaje de prueba a Telegram
    python alerta_casinelli.py --una-vez   # una sola revisión (GitHub Actions)
    python alerta_casinelli.py             # bucle cada INTERVALO_MIN minutos
"""
import html
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests


def cargar_config():
    """Lee config.txt (opcional, junto a este archivo) para uso en la laptop."""
    ruta = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.txt")
    try:
        with open(ruta, encoding="utf-8-sig") as f:
            for linea in f:
                linea = linea.strip()
                if not linea or linea.startswith("#") or "=" not in linea:
                    continue
                clave, valor = linea.split("=", 1)
                clave, valor = clave.strip(), valor.strip().strip('"').strip("'")
                if valor and clave not in os.environ:
                    os.environ[clave] = valor
    except FileNotFoundError:
        pass


cargar_config()

# ---------------- CONFIGURACIÓN ----------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "PEGA_AQUI_TU_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "PEGA_AQUI_TU_CHAT_ID")

MIN_DESCUENTO = 50              # porcentaje mínimo para avisar
INTERVALO_MIN = 30              # minutos entre revisiones (solo en modo bucle)
MAX_ALERTAS_POR_REVISION = 15   # el resto llega en la siguiente revisión
HORA_LATIDO = 8                 # hora de Lima (0-23) desde la cual se envía el aviso diario "bot activo"
BUSQUEDAS = ["caño", "inodoro", "trampa", "smart", "sensor"]
TIENDAS = {
    "Cassinelli": "https://www.cassinelli.com",
    "Promart": "https://www.promart.pe",
}
PAGINAS = 2                     # páginas de 50 productos por búsqueda
SEEN_FILE = "vistos_casinelli.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "es-PE,es;q=0.9",
}


# ---------------- UTILIDADES ----------------
def pct(actual, original):
    if actual and original and original > actual:
        return round((1 - actual / original) * 100)
    return 0


def cargar_vistos():
    try:
        with open(SEEN_FILE, encoding="utf-8") as f:
            return set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def guardar_vistos(vistos):
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(vistos), f)


class LimiteTelegram(Exception):
    """Telegram pidió esperar demasiado: el resto queda para la próxima revisión."""


def enviar_telegram(texto):
    for intento in range(2):
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": texto,
                "parse_mode": "HTML",
                "disable_web_page_preview": False,
            },
            timeout=20,
        )
        if r.status_code == 429:
            try:
                espera = int(r.json().get("parameters", {}).get("retry_after", 5))
            except Exception:
                espera = 5
            if intento == 0 and espera <= 60:
                time.sleep(espera + 1)
                continue
            raise LimiteTelegram(f"Telegram pidió esperar {espera}s")
        if not r.ok:
            raise RuntimeError(f"Telegram respondió {r.status_code}: {r.text[:200]}")
        return


# ---------------- TIENDAS (ambas usan VTEX) ----------------
def buscar_vtex(tienda, base, termino):
    for pagina in range(PAGINAS):
        desde = pagina * 50
        r = requests.get(
            f"{base}/api/catalog_system/pub/products/search/{termino}",
            params={"_from": desde, "_to": desde + 49, "O": "OrderByBestDiscountDESC"},
            headers=HEADERS,
            timeout=30,
        )
        r.raise_for_status()
        productos = r.json()
        for p in productos:
            mejor = None  # (descuento, precio, original)
            for item in p.get("items", []):
                for seller in item.get("sellers", []):
                    oferta = seller.get("commertialOffer", {})
                    if oferta.get("AvailableQuantity", 0) <= 0:
                        continue
                    precio, original = oferta.get("Price"), oferta.get("ListPrice")
                    d = pct(precio, original)
                    if mejor is None or d > mejor[0]:
                        mejor = (d, precio, original)
            if mejor:
                yield {
                    "tienda": tienda,
                    "id": str(p.get("productId")),
                    "nombre": p.get("productName", ""),
                    "marca": p.get("brand", ""),
                    "precio": mejor[1],
                    "original": mejor[2],
                    "desc": mejor[0],
                    "url": p.get("link", ""),
                }
        if len(productos) < 50:
            break
        time.sleep(1)


# ---------------- LÓGICA PRINCIPAL ----------------
def procesar(prod, vistos):
    if prod["desc"] < MIN_DESCUENTO or not prod["precio"]:
        return 0
    clave = f'{prod["tienda"]}:{prod["id"]}:{prod["precio"]}'
    if clave in vistos:
        return 0
    msg = (
        f'🔥 <b>-{prod["desc"]}%</b> en {html.escape(prod["tienda"])}\n'
        f'{html.escape(prod["marca"])} {html.escape(prod["nombre"])}\n'
        f'💰 S/ {prod["precio"]:,.2f}'
        + (f' (antes S/ {prod["original"]:,.2f})' if prod["original"] else "")
        + f'\n{html.escape(prod["url"], quote=False)}'
    )
    enviar_telegram(msg)
    vistos.add(clave)
    guardar_vistos(vistos)
    time.sleep(3)
    return 1


def enviar_latido(vistos, revisados, nuevos, con_error, ahora=None):
    """Una vez al día (desde HORA_LATIDO, hora de Lima) avisa que el bot sigue activo."""
    ahora = ahora or datetime.now(timezone(timedelta(hours=-5)))
    clave = f"__latido__:{ahora:%Y-%m-%d}"
    if ahora.hour < HORA_LATIDO or clave in vistos:
        return
    lineas = [
        f"✅ Bot Cassinelli/Promart activo ({ahora:%d/%m/%Y %H:%M}, hora de Lima)",
        f"Productos revisados en la última ronda: {revisados}",
        f"Alertas nuevas en esa ronda: {nuevos}",
        f"Descuento mínimo: {MIN_DESCUENTO}%",
    ]
    if con_error:
        lineas.append("⚠️ Sin acceso a: " + html.escape(", ".join(sorted(con_error))))
    enviar_telegram("\n".join(lineas))
    for vieja in [v for v in vistos if v.startswith("__latido__:")]:
        vistos.discard(vieja)
    vistos.add(clave)
    guardar_vistos(vistos)


def revisar():
    vistos = cargar_vistos()
    guardar_vistos(vistos)  # asegura que el archivo exista
    nuevos = ok = fallos = revisados = 0
    con_error = set()
    for tienda, base in TIENDAS.items():
        for termino in BUSQUEDAS:
            if nuevos >= MAX_ALERTAS_POR_REVISION:
                break
            try:
                for prod in buscar_vtex(tienda, base, termino):
                    revisados += 1
                    if nuevos >= MAX_ALERTAS_POR_REVISION:
                        break
                    nuevos += procesar(prod, vistos)
                ok += 1
            except LimiteTelegram as e:
                print(f"[{tienda} / {termino}] {e}; el resto se enviará en la próxima revisión.")
                return ok, fallos + 1
            except Exception as e:
                fallos += 1
                print(f"[{tienda} / {termino}] error: {e}")
                if "Telegram" not in str(e):
                    con_error.add(tienda)
            time.sleep(2)
    if nuevos >= MAX_ALERTAS_POR_REVISION:
        print(f"Límite de {MAX_ALERTAS_POR_REVISION} alertas alcanzado; el resto llegará en la próxima.")
    print(f"Revisión terminada: {nuevos} alertas nuevas.")
    try:
        enviar_latido(vistos, revisados, nuevos, con_error)
    except Exception as e:
        print(f"[aviso diario] error: {e}")
    return ok, fallos


def main():
    if "PEGA_AQUI" in TELEGRAM_TOKEN or "PEGA_AQUI" in TELEGRAM_CHAT_ID \
            or not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("Falta configurar TELEGRAM_TOKEN y/o TELEGRAM_CHAT_ID.")
        print("En la laptop: config.txt. En GitHub: Settings > Secrets and variables > Actions.")
        sys.exit(1)
    if "--test" in sys.argv:
        enviar_telegram("✅ Alerta Cassinelli/Promart conectada correctamente.")
        print("Mensaje de prueba enviado.")
        return
    if "--una-vez" in sys.argv:
        ok, fallos = revisar()
        if ok == 0 and fallos > 0:   # todo falló: que GitHub marque la ejecución en rojo
            sys.exit(1)
        return
    while True:
        revisar()
        time.sleep(INTERVALO_MIN * 60)


if __name__ == "__main__":
    main()
