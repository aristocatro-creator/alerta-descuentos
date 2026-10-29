#!/usr/bin/env python3
"""
Alerta de descuentos (mínimo configurable) en Falabella, Ripley, Oechsle, Plaza Vea, Metro,
Shopstar y Portaline -> Telegram.

Uso:
    pip install requests
    python alerta_descuentos.py --test   # envía un mensaje de prueba
    python alerta_descuentos.py          # corre en bucle cada INTERVALO_MIN minutos
    python alerta_descuentos.py --una-vez  # una sola revisión (útil para cron)
"""
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import requests
from urllib.parse import quote, urljoin


def cargar_config():
    """Lee config.txt (junto a este archivo) para no tener que usar 'set' cada vez."""
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

MIN_DESCUENTO = 60      # porcentaje mínimo para avisar
INTERVALO_MIN = 30      # cada cuántos minutos revisar
MAX_ALERTAS_POR_REVISION = 50  # máximo de mensajes por revisión; el resto llega en la siguiente
HORA_LATIDO = 8  # hora de Lima (0-23) desde la cual se envía el aviso diario "bot activo"
BUSQUEDAS = [
    "caterpillar", "parlantes", "audifono ear",
    "celular", "smartphone", "tetera", "tablet", "mochila porta",
    "televisor", "audifonos", "smartwatch", "consola", "maleta",
    "sandalia", "zapatillas", "porta", "ropa", "mochila",
    "licuadora", "refrigeradora", "lavadora", "microondas",
    "freidora", "aspiradora",
]
SEEN_FILE = "vistos.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "es-PE,es;q=0.9",
}

# Oechsle y Plaza Vea usan VTEX (API pública de catálogo)
VTEX = {
    "Oechsle": "https://www.oechsle.pe",
    "Plaza Vea": "https://www.plazavea.com.pe",
    "Metro": "https://www.metro.pe",
    "Shopstar": "https://www.shopstar.pe",
}

# Portaline usa Shopify: se revisa todo su catálogo una vez por ciclo
SHOPIFY = {
    "Portaline": "https://portaline.com",
}

# Ripley: se revisan las páginas de resultados de búsqueda (primeras páginas)
RIPLEY_BASE = "https://simple.ripley.com.pe"
RIPLEY_PAGINAS = 3
_ripley_bloqueado = False


# ---------------- UTILIDADES ----------------
def parse_price(s):
    s = re.sub(r"[^\d.,]", "", str(s)).replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None


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
    """Telegram pidió esperar demasiado: se deja el resto para la próxima revisión."""


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


# ---------------- TIENDAS ----------------
def buscar_vtex(tienda, base, termino):
    url = f"{base}/api/catalog_system/pub/products/search/{termino}"
    r = requests.get(
        url,
        params={"_from": 0, "_to": 49, "O": "OrderByBestDiscountDESC"},
        headers=HEADERS,
        timeout=30,
    )
    r.raise_for_status()
    for p in r.json():
        try:
            oferta = p["items"][0]["sellers"][0]["commertialOffer"]
        except (KeyError, IndexError):
            continue
        if oferta.get("AvailableQuantity", 0) <= 0:
            continue
        precio, original = oferta.get("Price"), oferta.get("ListPrice")
        yield {
            "tienda": tienda,
            "id": str(p.get("productId")),
            "nombre": p.get("productName", ""),
            "marca": p.get("brand", ""),
            "precio": precio,
            "original": original,
            "desc": pct(precio, original),
            "url": p.get("link", ""),
        }


def buscar_falabella(termino):
    r = requests.get(
        "https://www.falabella.com.pe/falabella-pe/search",
        params={"Ntt": termino},
        headers=HEADERS,
        timeout=30,
    )
    r.raise_for_status()
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, re.S)
    if not m:
        raise RuntimeError("Falabella no devolvió datos (posible bloqueo)")
    resultados = json.loads(m.group(1))["props"]["pageProps"].get("results", [])
    for p in resultados:
        actuales, tachados = [], []
        for pr in p.get("prices", []):
            val = parse_price((pr.get("price") or [None])[0])
            if val:
                (tachados if pr.get("crossed") else actuales).append(val)
        precio = min(actuales) if actuales else None
        original = max(tachados) if tachados else None
        desc = pct(precio, original)
        if not desc:  # respaldo: etiqueta de descuento
            etiqueta = (p.get("discountBadge") or {}).get("label", "")
            n = re.search(r"\d+", etiqueta)
            desc = int(n.group()) if n else 0
        yield {
            "tienda": "Falabella",
            "id": str(p.get("productId")),
            "nombre": p.get("displayName", ""),
            "marca": p.get("brand", ""),
            "precio": precio,
            "original": original,
            "desc": desc,
            "url": p.get("url", ""),
        }


def _parsear_ripley(texto_html):
    """Lee las tarjetas de producto de una página de Ripley."""
    productos, urls_vistas = [], set()
    patron = r'<a\b[^>]*?href="([^"#][^"]*)"[^>]*>(.*?)</a>'
    for m in re.finditer(patron, texto_html, re.S | re.I):
        href, interior = m.group(1), m.group(2)
        if "S/" not in interior:
            continue
        txt = html.unescape(re.sub(r"<[^>]+>", " ", interior))
        txt = re.sub(r"\s+", " ", txt)
        txt = re.sub(r"regalo a S/\s*[\d.,]+", "", txt, flags=re.I)
        precios = []
        for p in re.findall(r"S/\s*(\d[\d.,]*)", txt):
            v = parse_price(p)
            if v and v >= 5:
                precios.append(v)
        if len(set(precios)) < 2:
            continue
        url = urljoin(RIPLEY_BASE, href)
        if url in urls_vistas:
            continue
        urls_vistas.add(url)
        precio, original = min(precios), max(precios)
        productos.append({
            "tienda": "Ripley",
            "id": url,
            "nombre": txt.split("S/")[0].strip()[:120],
            "marca": "",
            "precio": precio,
            "original": original,
            "desc": pct(precio, original),
            "url": url,
        })
    return productos


def buscar_ripley(termino):
    global _ripley_bloqueado
    if _ripley_bloqueado:
        return
    for pagina in range(1, RIPLEY_PAGINAS + 1):
        r = requests.get(
            f"{RIPLEY_BASE}/search/{quote(termino)}",
            params={"page": pagina},
            headers=HEADERS,
            timeout=30,
        )
        if r.status_code == 403:
            _ripley_bloqueado = True
            raise RuntimeError("Ripley bloquea el acceso desde este servidor (403); se omite en esta revision")
        r.raise_for_status()
        productos = _parsear_ripley(r.text)
        if not productos:
            if pagina == 1:
                raise RuntimeError("no se encontraron productos (posible bloqueo o cambio de la página)")
            break
        yield from productos
        time.sleep(1)


def debug_ripley(termino):
    url = f"{RIPLEY_BASE}/search/{quote(termino)}"
    r = requests.get(url, headers=HEADERS, timeout=30)
    print("URL:", r.url, "| Estado:", r.status_code, "| Tamaño:", len(r.text))
    with open("ripley_debug.html", "w", encoding="utf-8") as f:
        f.write(r.text)
    prods = _parsear_ripley(r.text)
    print("Productos detectados:", len(prods))
    for p in prods[:8]:
        print(f'  -{p["desc"]}%  S/{p["precio"]:,.2f} (antes S/{p["original"]:,.2f})  {p["nombre"][:60]}')


def buscar_shopify(tienda, base):
    for pagina in range(1, 5):
        r = requests.get(
            f"{base}/products.json",
            params={"limit": 250, "page": pagina},
            headers=HEADERS,
            timeout=30,
        )
        r.raise_for_status()
        productos = r.json().get("products", [])
        if not productos:
            break
        for p in productos:
            mejor = None
            for v in p.get("variants", []):
                if not v.get("available"):
                    continue
                precio = parse_price(v.get("price"))
                original = parse_price(v.get("compare_at_price"))
                d = pct(precio, original)
                if mejor is None or d > mejor[0]:
                    mejor = (d, precio, original)
            if mejor:
                yield {
                    "tienda": tienda,
                    "id": str(p.get("id")),
                    "nombre": p.get("title", ""),
                    "marca": p.get("vendor", ""),
                    "precio": mejor[1],
                    "original": mejor[2],
                    "desc": mejor[0],
                    "url": f"{base}/products/{p.get('handle')}",
                }
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
        f"✅ Bot activo ({ahora:%d/%m/%Y %H:%M}, hora de Lima)",
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
    global _ripley_bloqueado
    _ripley_bloqueado = False
    vistos = cargar_vistos()
    nuevos = 0
    fuentes = []
    for termino in BUSQUEDAS:
        fuentes.append((f"Falabella / {termino}", lambda t=termino: buscar_falabella(t)))
        for tienda, base in VTEX.items():
            fuentes.append(
                (f"{tienda} / {termino}", lambda tn=tienda, b=base, t=termino: buscar_vtex(tn, b, t))
            )
    for termino in BUSQUEDAS:
        fuentes.append((f"Ripley / {termino}", lambda t=termino: buscar_ripley(t)))
    for tienda, base in SHOPIFY.items():
        fuentes.append((f"{tienda} / catálogo", lambda tn=tienda, b=base: buscar_shopify(tn, b)))

    revisados = 0
    con_error = set()
    for nombre, fn in fuentes:
        if nuevos >= MAX_ALERTAS_POR_REVISION:
            print(f"Límite de {MAX_ALERTAS_POR_REVISION} alertas por revisión alcanzado; el resto llegará en la próxima.")
            break
        try:
            for prod in fn():
                revisados += 1
                if nuevos >= MAX_ALERTAS_POR_REVISION:
                    break
                nuevos += procesar(prod, vistos)
        except LimiteTelegram as e:
            print(f"[{nombre}] {e}; el resto se enviará en la próxima revisión.")
            break
        except Exception as e:
            print(f"[{nombre}] error: {e}")
            if "Telegram" not in str(e):
                con_error.add(nombre.split(" / ")[0])
        time.sleep(2)  # cortesía entre peticiones
    guardar_vistos(vistos)
    print(f"Revisión terminada: {nuevos} alertas nuevas.")
    try:
        enviar_latido(vistos, revisados, nuevos, con_error)
    except Exception as e:
        print(f"[aviso diario] error: {e}")


def main():
    if "--debug-ripley" not in sys.argv and (
        not TELEGRAM_TOKEN
        or not TELEGRAM_CHAT_ID
        or "PEGA_AQUI" in TELEGRAM_TOKEN
        or "PEGA_AQUI" in TELEGRAM_CHAT_ID
    ):
        print("Falta configurar TELEGRAM_TOKEN y/o TELEGRAM_CHAT_ID.")
        print("En la laptop: config.txt. En GitHub: Settings > Secrets and variables > Actions.")
        sys.exit(1)
    if "--debug-ripley" in sys.argv:
        i = sys.argv.index("--debug-ripley")
        debug_ripley(sys.argv[i + 1] if len(sys.argv) > i + 1 else "celular")
        return
    if "--test" in sys.argv:
        enviar_telegram("✅ Alerta de descuentos conectada correctamente.")
        print("Mensaje de prueba enviado.")
        return
    if "--una-vez" in sys.argv:
        revisar()
        return
    while True:
        revisar()
        time.sleep(INTERVALO_MIN * 60)


if __name__ == "__main__":
    main()
