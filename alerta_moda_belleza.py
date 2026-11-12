#!/usr/bin/env python3
"""
Bot 4: ofertas con descuento >= MIN_DESCUENTO en Natura, L'Bel, Mia Beauty Bar, Platanitos,
Leonisa, Bata, Skechers, Topitop y Yanbal -> Telegram.

Cada tienda se lee con el método que funcione, en este orden:
  1) API de catálogo VTEX   2) catálogo Shopify (/products.json)   3) páginas HTML de ofertas
El aviso diario de las 10 a.m. (hora de Lima) indica qué método funcionó en cada tienda.

Uso:
    python alerta_moda_belleza.py --test      # mensaje de prueba a Telegram
    python alerta_moda_belleza.py --una-vez   # una sola revisión (GitHub Actions)
    python alerta_moda_belleza.py             # bucle cada INTERVALO_MIN minutos
"""
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse

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

MIN_DESCUENTO = 65              # porcentaje mínimo para avisar
MAX_DESCUENTO = 99              # en páginas HTML, por encima de esto se descarta (casi siempre es un error de lectura)
INTERVALO_MIN = 30              # minutos entre revisiones (solo en modo bucle)
MAX_ALERTAS_POR_REVISION = 100   # el resto llega en la siguiente revisión
HORA_LATIDO = 10                # hora de Lima (0-23) desde la cual se envía el aviso diario "bot activo"
MAX_PAGINAS_HTML = 8            # páginas HTML máximas a leer por tienda
SEEN_FILE = "vistos_moda_belleza.json"

# "paginas": páginas de ofertas conocidas. "inicio": portada, de donde se descubren más páginas de ofertas.
TIENDAS = {
    "Natura": {
        "base": "https://www.natura.com.pe",
        "paginas": ["/c/promociones", "/c/exclusivos-online", "/c/kits-exclusivos-online"],
    },
    "L'Bel": {"base": "https://lbel.tiendabelcorp.com.pe"},
    "Mia Beauty Bar": {"base": "https://miabeautybar.pe", "paginas": ["/categoria/best-deals"]},
    "Platanitos": {
        "base": "https://platanitos.com",
        "inicio": "/pe",
        "paginas": [
            "/pe/ofertas",
            "/pe/productos/categoria/mujeres?sort=discount_percentage+desc",
            "/pe/productos/categoria/hombres?sort=discount_percentage+desc",
            "/pe/productos/categoria/belleza?sort=discount_percentage+desc",
        ],
    },
    "Leonisa": {"base": "https://leonisa.pe"},
    "Bata": {"base": "https://www.bata.com", "inicio": "/pe/"},
    "Skechers": {"base": "https://www.pe.skechers.com", "inicio": "/es-pe"},
    "Topitop": {"base": "https://www.topitop.pe"},
    "Yanbal": {"base": "https://www.yanbal.com", "inicio": "/pe/corporate"},
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "es-PE,es;q=0.9",
}


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


def get(url, **kw):
    return requests.get(url, headers=HEADERS, timeout=30, **kw)


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


# ---------------- MÉTODO 1: VTEX ----------------
def mejor_oferta_vtex(p):
    mejor = None
    for item in p.get("items", []):
        for seller in item.get("sellers", []):
            o = seller.get("commertialOffer", {})
            if o.get("AvailableQuantity", 0) <= 0:
                continue
            d = pct(o.get("Price"), o.get("ListPrice"))
            if mejor is None or d > mejor[0]:
                mejor = (d, o.get("Price"), o.get("ListPrice"))
    return mejor


def via_vtex(tienda, base):
    """Lista de productos, o None si la tienda no usa VTEX."""
    out, paginas_sin_oferta = [], 0
    for pagina in range(20):
        desde = pagina * 50
        r = get(
            f"{base}/api/catalog_system/pub/products/search",
            params={"_from": desde, "_to": desde + 49, "O": "OrderByBestDiscountDESC"},
        )
        if r.status_code not in (200, 206):
            return None if pagina == 0 else out
        try:
            datos = r.json()
        except ValueError:
            return None if pagina == 0 else out
        if not isinstance(datos, list):
            return None if pagina == 0 else out
        if not datos:
            break
        mejor_pagina = 0
        for p in datos:
            mejor = mejor_oferta_vtex(p)
            if not mejor:
                continue
            mejor_pagina = max(mejor_pagina, mejor[0])
            out.append({
                "tienda": tienda, "id": str(p.get("productId")), "nombre": p.get("productName", ""),
                "marca": p.get("brand", ""), "precio": mejor[1], "original": mejor[2],
                "desc": mejor[0], "url": p.get("link", ""), "estructurado": True,
            })
        paginas_sin_oferta = paginas_sin_oferta + 1 if mejor_pagina < MIN_DESCUENTO else 0
        if paginas_sin_oferta >= 2 or len(datos) < 50:
            break
        time.sleep(1)
    return out


# ---------------- MÉTODO 2: SHOPIFY ----------------
def via_shopify(tienda, base):
    """Lista de productos, o None si la tienda no es Shopify."""
    out = []
    for pagina in range(1, 5):
        r = get(f"{base}/products.json", params={"limit": 250, "page": pagina})
        if r.status_code != 200:
            return None if pagina == 1 else out
        try:
            datos = r.json()
        except ValueError:
            return None if pagina == 1 else out
        if not isinstance(datos, dict) or "products" not in datos:
            return None if pagina == 1 else out
        if not datos["products"]:
            break
        for p in datos["products"]:
            mejor = None
            for v in p.get("variants", []):
                if not v.get("available"):
                    continue
                precio, original = parse_price(v.get("price")), parse_price(v.get("compare_at_price"))
                d = pct(precio, original)
                if mejor is None or d > mejor[0]:
                    mejor = (d, precio, original)
            if mejor:
                out.append({
                    "tienda": tienda, "id": str(p.get("id")), "nombre": p.get("title", ""),
                    "marca": p.get("vendor", ""), "precio": mejor[1], "original": mejor[2],
                    "desc": mejor[0], "url": f"{base}/products/{p.get('handle')}", "estructurado": True,
                })
        time.sleep(1)
    return out


# ---------------- MÉTODO 3: PÁGINAS HTML ----------------
RE_ANCLA = re.compile(r'<a\b[^>]*?href="([^"#][^"]*)"[^>]*>(.*?)</a>', re.S | re.I)
RE_PRECIO = re.compile(r"S/\.?\s*(\d[\d.,]*)")
RE_ETIQUETA = re.compile(r"-\s*(\d{1,2})\s*%")
RE_RUIDO = [
    re.compile(r"(?i)(?:\d+\s*)?cuotas?[^S]{0,40}S/\.?\s*[\d.,]+"),
    re.compile(r"(?i)S/\.?\s*[\d.,]+\s*(?:al|por|x)?\s*mes"),
    re.compile(r"(?i)(?:regalo|env[ií]o|delivery|compras?|gratis)\s[^S]{0,40}S/\.?\s*[\d.,]+"),
]
PALABRAS_OFERTA = ("sale", "oferta", "liquidaci", "outlet", "descuento", "promo", "rebaja", "remate")
PALABRAS_EXCLUIR = ("termino", "condicion", "legal", "politica", "blog", "login", "cuenta", "carrito",
                    "ayuda", "reclam", "privacidad", "tienda-fisica", "locales")
PARAMS_RUIDO = ("iprom", "utm", "position", "listtitle", "sid", "gclid", "fbclid")


def limpiar_url(url):
    p = urlparse(url)
    q = [(k, v) for k, v in parse_qsl(p.query) if not k.lower().startswith(PARAMS_RUIDO)]
    return p._replace(query=urlencode(q), fragment="").geturl()


def nombre_desde_url(url):
    segs = [s for s in urlparse(url).path.split("/") if s]
    return max(segs, key=len).replace("-", " ") if segs else ""


def parsear_tarjetas(texto_html, tienda, url_pagina):
    """Lee tarjetas de producto (enlaces que contienen precios en S/)."""
    productos, vistas = [], set()
    for m in RE_ANCLA.finditer(texto_html):
        href, interior = m.group(1), m.group(2)
        if "S/" not in interior:
            continue
        txt = html.unescape(re.sub(r"<[^>]+>", " ", interior))
        txt = re.sub(r"\s+", " ", txt)
        for ruido in RE_RUIDO:
            txt = ruido.sub(" ", txt)
        precios = sorted({v for v in (parse_price(p) for p in RE_PRECIO.findall(txt)) if v and v >= 5},
                         reverse=True)
        etiquetas = [int(e) for e in RE_ETIQUETA.findall(txt)]
        etiqueta = max(etiquetas) if etiquetas else 0
        if len(precios) >= 2:
            original, precio = precios[0], precios[1]   # conservador: ignora precios más bajos (cuotas, tarjeta)
        elif len(precios) == 1 and etiqueta:
            precio = precios[0]
            original = round(precio / (1 - etiqueta / 100), 2)
        else:
            continue
        desc = etiqueta or pct(precio, original)
        url = limpiar_url(urljoin(url_pagina, href))
        if url in vistas:
            continue
        vistas.add(url)
        productos.append({
            "tienda": tienda, "id": url, "nombre": txt.split("S/")[0].strip()[:120] or nombre_desde_url(url),
            "marca": "", "precio": precio, "original": original, "desc": desc, "url": url,
        })
    return productos


def descubrir_paginas(base, url_inicio, texto_html):
    host = urlparse(base).netloc
    encontradas = []
    for m in RE_ANCLA.finditer(texto_html):
        href, interior = m.group(1), m.group(2)
        texto = html.unescape(re.sub(r"<[^>]+>", " ", interior)).lower()
        destino = href.lower()
        if any(x in destino for x in PALABRAS_EXCLUIR):
            continue
        if not any(x in destino or x in texto for x in PALABRAS_OFERTA):
            continue
        url = limpiar_url(urljoin(url_inicio, href))
        if urlparse(url).netloc == host and url not in encontradas:
            encontradas.append(url)
    return encontradas[:6]


def via_html(tienda, cfg):
    base = cfg["base"]
    paginas = [urljoin(base, p) for p in cfg.get("paginas", [])]
    errores = []
    inicio = urljoin(base, cfg.get("inicio", "/"))
    try:
        r = get(inicio)
        r.raise_for_status()
        paginas.append(r.url)
        paginas += descubrir_paginas(base, r.url, r.text)
    except Exception as e:
        errores.append(f"{inicio}: {e}")
    out, ids, leidas = [], set(), 0
    for url in list(dict.fromkeys(paginas))[:MAX_PAGINAS_HTML]:
        try:
            r = get(url)
            r.raise_for_status()
            leidas += 1
            for prod in parsear_tarjetas(r.text, tienda, r.url):
                if prod["id"] not in ids:
                    ids.add(prod["id"])
                    out.append(prod)
        except Exception as e:
            errores.append(f"{url}: {e}")
        time.sleep(1)
    if leidas == 0:
        raise RuntimeError("; ".join(errores)[:150] or "no se pudo leer ninguna página")
    return out


def buscar_tienda(tienda, cfg):
    """Devuelve (método, productos). Prueba VTEX, luego Shopify, luego HTML."""
    base = cfg["base"]
    for nombre, funcion in (("VTEX", via_vtex), ("Shopify", via_shopify)):
        try:
            productos = funcion(tienda, base)
        except Exception:
            productos = None
        if productos is not None:
            return nombre, productos
    return "HTML", via_html(tienda, cfg)


# ---------------- LÓGICA PRINCIPAL ----------------
def procesar(prod, vistos):
    clave = f'{prod["tienda"]}:{prod["id"]}:{prod["precio"]}'
    if clave in vistos:
        return 0
    msg = (
        f'🔥 <b>-{prod["desc"]}%</b> en {html.escape(prod["tienda"], quote=False)}\n'
        f'{html.escape(" ".join(x for x in (prod["marca"], prod["nombre"]) if x), quote=False)}\n'
        f'💰 S/ {prod["precio"]:,.2f}'
        + (f' (antes S/ {prod["original"]:,.2f})' if prod["original"] else "")
        + f'\n{html.escape(prod["url"], quote=False)}'
    )
    enviar_telegram(msg)
    vistos.add(clave)
    guardar_vistos(vistos)
    time.sleep(3)
    return 1


def enviar_latido(vistos, resumen, nuevos, ahora=None):
    """Una vez al día (desde HORA_LATIDO, hora de Lima) avisa que el bot sigue activo."""
    ahora = ahora or datetime.now(timezone(timedelta(hours=-5)))
    clave = f"__latido__:{ahora:%Y-%m-%d}"
    if ahora.hour < HORA_LATIDO or clave in vistos:
        return
    lineas = [
        f"✅ Bot moda y belleza {MIN_DESCUENTO}%+ activo ({ahora:%d/%m/%Y %H:%M}, hora de Lima)",
        f"Alertas nuevas en la última ronda: {nuevos}",
        "Estado por tienda:",
    ]
    for tienda, (modo, n, error) in resumen.items():
        if error:
            lineas.append(f"⚠️ {html.escape(tienda, quote=False)}: sin acceso")
        elif n == 0:
            lineas.append(f"⚠️ {html.escape(tienda, quote=False)}: {modo}, 0 productos leídos")
        else:
            lineas.append(f"✔️ {html.escape(tienda, quote=False)}: {modo}, {n} productos")
    enviar_telegram("\n".join(lineas))
    for vieja in [v for v in vistos if v.startswith("__latido__:")]:
        vistos.discard(vieja)
    vistos.add(clave)
    guardar_vistos(vistos)


def revisar():
    vistos = cargar_vistos()
    guardar_vistos(vistos)  # asegura que el archivo exista
    resumen, todos, errores = {}, [], 0
    for tienda, cfg in TIENDAS.items():
        try:
            modo, productos = buscar_tienda(tienda, cfg)
            resumen[tienda] = (modo, len(productos), None)
            todos += productos
            print(f"[{tienda}] {modo}: {len(productos)} productos leídos")
        except Exception as e:
            errores += 1
            resumen[tienda] = ("error", 0, str(e)[:100])
            print(f"[{tienda}] error: {e}")
        time.sleep(2)  # cortesía entre tiendas
    candidatos = sorted(
        (p for p in todos if p["precio"] and p["desc"] >= MIN_DESCUENTO
         and (p["desc"] <= MAX_DESCUENTO or p.get("estructurado"))),
        key=lambda p: -p["desc"],
    )
    nuevos = 0
    try:
        for prod in candidatos:
            if nuevos >= MAX_ALERTAS_POR_REVISION:
                print(f"Límite de {MAX_ALERTAS_POR_REVISION} alertas alcanzado; el resto llegará en la próxima.")
                break
            nuevos += procesar(prod, vistos)
    except LimiteTelegram as e:
        print(f"{e}; el resto se enviará en la próxima revisión.")
        return len(resumen) - errores, errores
    print(f"Revisión terminada: {len(todos)} productos leídos, {nuevos} alertas nuevas.")
    try:
        enviar_latido(vistos, resumen, nuevos)
    except Exception as e:
        print(f"[aviso diario] error: {e}")
    return len(resumen) - errores, errores


def main():
    if "PEGA_AQUI" in TELEGRAM_TOKEN or "PEGA_AQUI" in TELEGRAM_CHAT_ID \
            or not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("Falta configurar TELEGRAM_TOKEN y/o TELEGRAM_CHAT_ID.")
        print("En la laptop: config.txt. En GitHub: Settings > Secrets and variables > Actions.")
        sys.exit(1)
    if "--test" in sys.argv:
        enviar_telegram(f"✅ Bot moda y belleza {MIN_DESCUENTO}%+ conectado correctamente.")
        print("Mensaje de prueba enviado.")
        return
    if "--una-vez" in sys.argv:
        ok, errores = revisar()
        if ok == 0 and errores > 0:   # todas las tiendas fallaron: que GitHub marque la ejecución en rojo
            sys.exit(1)
        return
    while True:
        revisar()
        time.sleep(INTERVALO_MIN * 60)


if __name__ == "__main__":
    main()
