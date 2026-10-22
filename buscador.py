"""
Buscador de departamentos en arriendo - Portal Inmobiliario.

Recorre las busquedas configuradas en config.json, entra a cada aviso para
sacar gastos comunes, coordenadas y caracteristicas, filtra por presupuesto
total (arriendo + GGCC), programa y zona, guarda todo en data/avisos.json,
regenera dashboard.html y avisa de lo nuevo por Windows y/o correo.

Uso:
    py buscador.py                    ejecucion normal
    py buscador.py --max-paginas 1    prueba rapida
    py buscador.py --solo-dashboard   regenera dashboard.html sin scrapear
    py buscador.py --probar-aviso     manda una notificacion de prueba
"""
import argparse
import json
import os
import random
import re
import shutil
import smtplib
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape
from pathlib import Path

import requests

BASE = Path(__file__).resolve().parent
DATA = BASE / "data"
LOGS = BASE / "logs"
AVISOS_JSON = DATA / "avisos.json"
LOCK = DATA / ".lock"
DASHBOARD = BASE / "dashboard.html"
PLANTILLA = BASE / "plantilla_dashboard.html"

URL_BUSQUEDA = "https://www.portalinmobiliario.com/arriendo/departamento/{comuna}/_PriceRange_0CLP-{precio}CLP_BEDROOMS_{dmin}-{dmax}_FULL*BATHROOMS_{bmin}-*"
URL_FOTO = "https://http2.mlstatic.com/D_NQ_NP_{id}-O.webp"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "es-CL,es;q=0.9",
}


# ---------------------------------------------------------------- utilidades

def log(msg):
    LOGS.mkdir(exist_ok=True)
    linea = f"{datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    print(linea, flush=True)
    with open(LOGS / f"buscador_{datetime.now():%Y-%m}.log", "a", encoding="utf-8") as f:
        f.write(linea + "\n")


def ahora():
    return datetime.now().isoformat(timespec="seconds")


def cargar_config():
    with open(BASE / "config.json", encoding="utf-8") as f:
        return json.load(f)


def cargar_env():
    """Lee .env (CLAVE=valor) sin pisar variables de entorno ya definidas."""
    env = BASE / ".env"
    if env.exists():
        for linea in env.read_text(encoding="utf-8").splitlines():
            if "=" in linea and not linea.strip().startswith("#"):
                k, v = linea.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


class Bloqueado(Exception):
    """El portal devolvio algo que no es la pagina esperada (captcha, verificacion, etc.)."""


class Cliente:
    def __init__(self, pausa):
        self.s = requests.Session()
        self.s.headers.update(HEADERS)
        self.pausa = pausa
        self.ultimo = 0.0

    def get(self, url, intentos=3):
        espera = random.uniform(*self.pausa) - (time.time() - self.ultimo)
        if espera > 0:
            time.sleep(espera)
        for i in range(intentos):
            try:
                r = self.s.get(url, timeout=30)
                self.ultimo = time.time()
                if r.status_code == 200:
                    return r.text
                if r.status_code in (403, 429):
                    raise Bloqueado(f"HTTP {r.status_code} en {url}")
                log(f"  HTTP {r.status_code} en {url} (intento {i + 1})")
            except requests.RequestException as e:
                log(f"  error de red {e.__class__.__name__} (intento {i + 1})")
            time.sleep(5 * (i + 1))
        return None


def estado_nordic(html):
    """Extrae el JSON de estado que Portal Inmobiliario embebe en la pagina."""
    m = re.search(r'id="__NORDIC_RENDERING_CTX__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        raise Bloqueado("la pagina no trae __NORDIC_RENDERING_CTX__ (posible captcha)")
    body = m.group(1)
    d, _ = json.JSONDecoder().raw_decode(body[body.find("{"):])
    pp = (d.get("appProps") or {}).get("pageProps") or {}
    if "initialState" not in pp:
        titulo = re.search(r"<title[^>]*>(.*?)</title>", html, re.S)
        raise Bloqueado(f"pagina inesperada '{titulo.group(1).strip()[:80] if titulo else '?'}' "
                        f"(claves: {', '.join(list(pp)[:10])})")
    return pp["initialState"]


def recorrer(o):
    """Itera todos los dicts anidados."""
    if isinstance(o, dict):
        yield o
        for v in o.values():
            yield from recorrer(v)
    elif isinstance(o, list):
        for v in o:
            yield from recorrer(v)


def numeros(texto):
    return [float(x.replace(".", "").replace(",", ".")) for x in re.findall(r"\d+(?:[.,]\d+)*", texto or "")]


def rango(textos, palabra):
    """'1 a 2 dormitorios' -> (1, 2); 'Estudio a 2 dormitorios' -> (0, 2); '98 m² útiles' -> (98, 98)."""
    for t in textos:
        if palabra in t.lower():
            n = numeros(t)
            if "estudio" in t.lower() or "monoambiente" in t.lower():
                n = [0] + n
            if n:
                return min(n), max(n)
    return None, None


def monto(texto):
    """'300.000 CLP' -> (300000, 'CLP'); '3,5 UF' -> (3.5, 'CLF')."""
    if not texto:
        return None, None
    n = numeros(texto)
    if not n:
        return None, None
    t = texto.upper()
    moneda = "CLF" if ("UF" in t or "CLF" in t) else "USD" if ("USD" in t or "US$" in t) else "CLP"
    return n[0], moneda


def a_clp(valor, moneda, uf, dolar):
    if valor is None:
        return None
    if moneda == "CLF":
        return round(valor * uf)
    if moneda == "USD":
        return round(valor * dolar)
    return round(valor)


MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
         "septiembre", "octubre", "noviembre", "diciembre"]


def uf_sii(hoy):
    """Valor UF del dia desde la tabla anual publicada por el SII."""
    html = requests.get(f"https://www.sii.cl/valores_y_fechas/uf/uf{hoy.year}.htm", timeout=20).text
    mes = html.split(f"id='mes_{MESES[hoy.month - 1]}'", 1)[1].split("id='mes_", 1)[0]
    dias = dict(re.findall(r"<strong>(\d+)</strong></th>\s*<td[^>]*>([\d.,]+)</td>", mes))
    return numeros(dias[str(hoy.day)])[0]


def indicadores(cfg, previo):
    uf = previo.get("uf") or cfg["uf_respaldo"]
    dolar = previo.get("dolar") or cfg["dolar_respaldo"]
    try:
        uf = requests.get("https://mindicador.cl/api/uf", timeout=15).json()["serie"][0]["valor"]
        dolar = requests.get("https://mindicador.cl/api/dolar", timeout=15).json()["serie"][0]["valor"]
    except Exception as e:
        try:
            uf = uf_sii(datetime.now())
            log(f"mindicador.cl no responde ({e.__class__.__name__}); UF {uf:,.2f} desde SII, dolar {dolar}")
        except Exception as e2:
            log(f"No pude leer UF ({e.__class__.__name__} / {e2.__class__.__name__}); uso {uf} / {dolar}")
    return uf, dolar


# ---------------------------------------------------------------- scraping

def parsear_tarjeta(pc, comuna):
    comp = {c.get("id"): c for c in pc.get("components", [])}
    precio = comp.get("price", {}).get("price", {}).get("current_price", {})
    textos = comp.get("attributes_list", {}).get("attributes_list", {}).get("texts", [])
    md = pc["metadata"]
    fotos = [p["id"] for p in pc.get("pictures", {}).get("pictures", [])]
    dmin, dmax = rango(textos, "dormitorio")
    bmin, bmax = rango(textos, "baño")
    smin, smax = rango(textos, "m²")
    return {
        "id": md["id"],
        "url": "https://" + md["url"].split("#")[0],
        "titulo": comp.get("title", {}).get("title", {}).get("text", ""),
        "direccion": comp.get("location", {}).get("location", {}).get("text", ""),
        "comuna": comuna,
        "precio": precio.get("value"),
        "moneda": precio.get("currency", "CLP"),
        "multifamily": "project" in comp or comp.get("headline", {}).get("headline", {}).get("text", "").startswith("Departamentos"),
        "unidades": comp.get("available_units", {}).get("available_units", {}).get("text"),
        "dorm": [dmin, dmax],
        "banos": [bmin, bmax],
        "m2": [smin, smax],
        "foto": URL_FOTO.format(id=fotos[0]) if fotos else None,
    }


def buscar_comuna(cli, cfg, slug, comuna, max_paginas):
    b = cfg["busqueda"]
    url = URL_BUSQUEDA.format(comuna=slug, precio=b["presupuesto_total_clp"],
                              dmin=b["dormitorios_min"], dmax=b["dormitorios_max"], bmin=b["banos_min"])
    tarjetas, pagina, completa = [], 0, False
    while url and pagina < max_paginas:
        pagina += 1
        html = cli.get(url)
        if html is None:
            log(f"  {comuna}: fallo pagina {pagina}, corto aqui")
            break
        st = estado_nordic(html)
        res = [r["polycard"] for r in st.get("results", []) if "polycard" in r]
        tarjetas += [parsear_tarjeta(pc, comuna) for pc in res]
        nxt = (st.get("pagination") or {}).get("next_page") or {}
        url = nxt.get("url") if nxt.get("show") else None
        if not url:
            completa = True
    log(f"  {comuna}: {len(tarjetas)} avisos en {pagina} paginas{'' if completa else ' (recorrido incompleto)'}")
    return tarjetas, completa


def parsear_detalle(html):
    st = estado_nordic(html)
    specs, lat, lon = {}, None, None
    for o in recorrer(st):
        if lat is None and "latitude" in o and "longitude" in o:
            try:
                lat, lon = float(o["latitude"]), float(o["longitude"])
            except (TypeError, ValueError):
                pass
        i, t = o.get("id"), o.get("text")
        if isinstance(i, str) and isinstance(t, str) and i[:1].isupper() and len(t) < 80:
            specs.setdefault(i, t)
    comp = st.get("components", {})
    sub = ((comp.get("header") or {}).get("subtitle") or "").lower()
    dias = None
    m = re.search(r"hace (?:más de )?(\d+) (hora|día|dia|semana|mes|año)", sub)
    if m:
        n = int(m.group(1))
        dias = {"hora": 0, "día": n, "dia": n, "semana": n * 7, "mes": n * 30, "año": n * 365}[m.group(2)]
    elif "publicado hoy" in sub:
        dias = 0
    elif "ayer" in sub:
        dias = 1
    elif "esta semana" in sub:
        dias = 3
    # Edificios multifamily: tipologias disponibles (sin precio por modelo)
    modelos = []
    for mo in (comp.get("quotable_models") or {}).get("models", []):
        txt = [x.get("label", {}).get("text", "") for x in mo.get("attributes", [])] + [mo.get("picture", {}).get("alt", "")]
        modelos.append({"dorm": rango(txt, "dormitorio")[0], "banos": rango(txt, "baño")[0], "m2": rango(txt, "m²")[0]})
    fotos = list(dict.fromkeys(re.findall(r'"(\d{5,7}-MLC\d+_\d+)"', json.dumps(comp.get("gallery_mosaic", {})))))
    desc = (comp.get("description") or {}).get("content", "") or ""
    return {
        "lat": lat, "lon": lon, "specs": specs, "modelos": modelos,
        "publicado": (datetime.now() - timedelta(days=dias)).date().isoformat() if dias is not None else None,
        "descripcion": desc[:1500],
        "fotos": [URL_FOTO.format(id=f) for f in fotos[:8]],
    }


# ---------------------------------------------------------------- evaluacion

def evaluar(a, cfg, uf, dolar):
    """Calcula montos en CLP y la lista de motivos por los que NO cumple (vacia = cumple)."""
    b = cfg["busqueda"]
    sp = a.get("specs", {})
    if re.search(r"\d+ a \d+", sp.get("Dormitorios", "")):  # edificio de renta sin etiqueta PROYECTO
        a["multifamily"] = True
    if not a["multifamily"]:
        for k, campo in (("Dormitorios", "dorm"), ("Baños", "banos")):
            n = numeros(sp.get(k))
            if n:
                a[campo] = [n[0], n[0]]
        n = numeros(sp.get("Superficie útil")) or numeros(sp.get("Superficie total"))
        if n:
            a["m2"] = [n[0], n[0]]
    a["estacionamientos"] = int(numeros(sp.get("Estacionamientos"))[0]) if numeros(sp.get("Estacionamientos")) else None
    a["bodegas"] = int(numeros(sp.get("Bodegas"))[0]) if numeros(sp.get("Bodegas")) else None
    a["mascotas"] = sp.get("Admite mascotas")
    a["amoblado"] = sp.get("Amoblado")
    a["piso"] = sp.get("Número de piso de la unidad")
    a["antiguedad"] = sp.get("Antigüedad")
    a["orientacion"] = sp.get("Orientación")

    a["precio_clp"] = a_clp(a["precio"], a["moneda"], uf, dolar)
    a["precio_estimado"] = False
    a["modelo"] = None
    motivos = []
    modelos = [m for m in a.get("modelos") or [] if m["dorm"] is not None and m["banos"] is not None]
    if a["multifamily"] and modelos:
        sirven = [m for m in modelos if b["dormitorios_min"] <= m["dorm"] <= b["dormitorios_max"] and m["banos"] >= b["banos_min"]]
        if not sirven:
            motivos.append("programa")
        else:
            # El precio publicado es "desde" (la unidad mas chica): se escala por m2 al modelo que sirve
            mod = min(sirven, key=lambda m: m["m2"] or 1e9)
            a["modelo"] = mod
            a["dorm"], a["banos"], a["m2"] = [mod["dorm"]] * 2, [mod["banos"]] * 2, [mod["m2"]] * 2
            m2_min = min((m["m2"] for m in modelos if m["m2"]), default=None)
            if mod["m2"] and m2_min and mod["m2"] > m2_min and a["precio_clp"]:
                a["precio_clp"] = round(a["precio_clp"] * mod["m2"] / m2_min / 1000) * 1000
                a["precio_estimado"] = True
    gv, gm = monto(sp.get("Gastos comunes"))
    if gv is not None and a_clp(gv, gm, uf, dolar) >= 10000:  # "$0" o "$1" = no informado
        a["ggcc_clp"], a["ggcc_estimado"] = a_clp(gv, gm, uf, dolar), False
    else:
        m2 = (a["m2"] or [None])[0] or 70
        a["ggcc_clp"], a["ggcc_estimado"] = round(m2 * cfg["ggcc_estimado_por_m2_clp"]), True
    a["total_clp"] = (a["precio_clp"] or 0) + (a["ggcc_clp"] or 0)
    m2 = (a["m2"] or [None])[0]
    a["clp_m2"] = round(a["total_clp"] / m2) if m2 else None

    if a["total_clp"] > b["presupuesto_total_clp"]:
        motivos.append("presupuesto")
    dmin, dmax = a["dorm"]
    if dmin is not None and (dmax < b["dormitorios_min"] or dmin > b["dormitorios_max"]):
        motivos.append("dormitorios")
    if a["banos"][1] is not None and a["banos"][1] < b["banos_min"]:
        motivos.append("baños")
    lim = cfg.get("limites_por_comuna", {}).get(a["comuna"], {})
    if a.get("lat") is not None:
        lat, lon = a["lat"], a["lon"]
        if ("lon_min" in lim and lon < lim["lon_min"]) or ("lon_max" in lim and lon > lim["lon_max"]) \
                or ("lat_min" in lim and lat < lim["lat_min"]) or ("lat_max" in lim and lat > lim["lat_max"]):
            motivos.append("zona")
    if a["multifamily"] and not b.get("incluir_edificios_multifamily", True):
        motivos.append("multifamily")
    a["motivos"] = motivos
    a["cumple"] = not motivos
    return a


# Estaciones de Metro de la zona (OpenStreetMap)
METRO = [
    ("Manuel Montt", -33.42855, -70.61965), ("Pedro de Valdivia", -33.42548, -70.61380),
    ("Los Leones", -33.42202, -70.60856), ("Inés de Suárez", -33.43872, -70.60734),
    ("Tobalaba", -33.41829, -70.60165), ("El Golf", -33.41668, -70.59607),
    ("Cristóbal Colón", -33.42632, -70.59098), ("Alcántara", -33.41545, -70.58999),
    ("Francisco Bilbao", -33.43179, -70.58470), ("Escuela Militar", -33.41348, -70.58268),
    ("Príncipe de Gales", -33.43920, -70.57315), ("Simón Bolívar", -33.44618, -70.57193),
    ("Manquehue", -33.40946, -70.56973), ("Hernando de Magallanes", -33.40794, -70.55585),
    ("Los Dominicos", -33.40789, -70.54499), ("Fernando Castillo Velasco", -33.45210, -70.55811),
    ("Plaza Egaña", -33.45349, -70.57082), ("Chile España", -33.45491, -70.59814),
]


def dist_m(lat1, lon1, lat2, lon2):
    return ((lon2 - lon1) * 111320 * 0.8348) ** 2 + ((lat2 - lat1) * 110540) ** 2  # cos(-33.4°); al cuadrado


def mediana(xs):
    xs = sorted(xs)
    n = len(xs)
    return None if not n else xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2


def lineal(x, cero, cien):
    """0 en 'cero', 100 en 'cien', recortado a [0, 100]."""
    return max(0.0, min(100.0, (x - cero) / (cien - cero) * 100))


def analizar(avisos, cfg, uf):
    """UF/m2, comparacion con comparables cercanos, metro mas cercano y puntaje 0-100."""
    pesos = {"precio_vs_mercado": 30, "costo_total": 15, "superficie": 20, "ubicacion": 20, "extras": 15}
    pesos.update({k: v for k, v in cfg.get("pesos_puntaje", {}).items() if not k.startswith("_")})
    pres = cfg["busqueda"]["presupuesto_total_clp"]
    base = [a for a in avisos.values() if a.get("activo") and not a.get("duplicado_de") and not a["multifamily"]
            and a["m2"][0] and a.get("precio_clp")]
    for a in base:
        m2 = a["m2"][0]
        a["uf_mes"] = round(a["precio_clp"] / uf, 2)
        a["uf_m2"] = round(a["precio_clp"] / uf / m2, 3)
        a["uf_m2_total"] = round(a["total_clp"] / uf / m2, 3)
        a["metro"], a["dist_metro"] = None, None
        if a.get("lat") is not None:
            d2, nombre = min((dist_m(a["lat"], a["lon"], la, lo), n) for n, la, lo in METRO)
            a["metro"], a["dist_metro"] = nombre, round(d2 ** 0.5)
        if a["dist_metro"] is not None and a["dist_metro"] <= 1200:
            a["sector"] = a["metro"]
        else:
            a["sector"] = "Las Condes oriente" if a["comuna"] == "Las Condes" else f"{a['comuna']} (sin metro cerca)"

        # m2 o precio fuera de rango razonable para un depto de 2-3D en la zona: probable error del aviso
        a["dudoso"] = not (30 <= m2 <= 250 and 0.15 <= a["uf_m2"] <= 1.2)

    con_geo = [a for a in base if a.get("lat") is not None and not a["dudoso"]]
    for a in base:
        a["mercado_uf_m2"], a["comps"], a["vs_mercado"] = None, 0, None
        if a.get("lat") is None:
            continue
        for radio, mismo_dorm in ((1000, True), (1500, True), (1000, False), (2000, False)):
            comps = [c["uf_m2"] for c in con_geo if c is not a
                     and (not mismo_dorm or c["dorm"][0] == a["dorm"][0])
                     and dist_m(a["lat"], a["lon"], c["lat"], c["lon"]) <= radio ** 2]
            if len(comps) >= 5:
                med = mediana(comps)
                a["mercado_uf_m2"], a["comps"] = round(med, 3), len(comps)
                a["vs_mercado"] = round(a["uf_m2"] / med - 1, 3)
                a["comps_criterio"] = f"{len(comps)} deptos {'de ' + str(int(a['dorm'][0])) + 'D ' if mismo_dorm else ''}a ≤{radio} m"
                break

        d = a["dorm"][0] or 2
        lo, hi = (55, 85) if d <= 2 else (70, 110)
        antig = numeros(a.get("antiguedad"))
        det = {
            "precio_vs_mercado": 50 if a["dudoso"] or a["vs_mercado"] is None else lineal(a["vs_mercado"], 0.25, -0.25),
            "costo_total": lineal(a["total_clp"], pres, pres * 0.65),
            "superficie": 50 if a["dudoso"] else lineal(a["m2"][0], lo, hi),
            "ubicacion": lineal(a["dist_metro"], 1500, 300) if a["dist_metro"] is not None else 50,
            "extras": (50 if (a.get("estacionamientos") or 0) > 0 else 0) + (20 if (a.get("bodegas") or 0) > 0 else 0)
                      + (30 if (antig and antig[0] <= 15) or "estrenar" in (a.get("antiguedad") or "").lower() else 0),
        }
        a["puntaje_det"] = {k: round(v) for k, v in det.items()}
        a["puntaje"] = round(sum(det[k] * pesos[k] for k in det) / sum(pesos.values()))

        r = ["⚠ revisar m² o precio (dato inusual)"] if a["dudoso"] else []
        if a["dudoso"]:
            pass
        elif a["vs_mercado"] is not None and a["vs_mercado"] <= -0.08:
            r.append(f"{-a['vs_mercado']:.0%} bajo mercado")
        elif a["vs_mercado"] is not None and a["vs_mercado"] >= 0.08:
            r.append(f"{a['vs_mercado']:.0%} sobre mercado")
        if a["dist_metro"] is not None and a["dist_metro"] <= 600:
            r.append(f"{a['dist_metro']} m de Metro {a['metro']}")
        if a["dudoso"]:
            pass
        elif a["m2"][0] >= hi:
            r.append(f"amplio ({a['m2'][0]:.0f} m²)")
        elif a["m2"][0] < lo:
            r.append(f"chico ({a['m2'][0]:.0f} m²)")
        if not a.get("estacionamientos"):
            r.append("sin estacionamiento")
        if a.get("ggcc_estimado"):
            r.append("GGCC no informado")
        a["razones"] = r


def marcar_duplicados(avisos):
    """Mismo depto publicado varias veces (mismas coordenadas, programa y m2): deja visible el mas barato."""
    grupos = {}
    for a in avisos.values():
        a.pop("duplicado_de", None)
        a["duplicados"] = 0
        if a.get("activo") and not a["multifamily"] and a.get("lat") is not None:
            k = (round(a["lat"], 4), round(a["lon"], 4), a["dorm"][0], a["banos"][0], a["m2"][0])
            grupos.setdefault(k, []).append(a)
    for g in grupos.values():
        if len(g) > 1:
            g.sort(key=lambda a: (a["total_clp"], a["primera_vez"]))
            g[0]["duplicados"] = len(g) - 1
            for a in g[1:]:
                a["duplicado_de"] = g[0]["id"]


# ---------------------------------------------------------------- salida

def escribir_dashboard(avisos, meta):
    """Genera dashboard.html desde la plantilla con los datos incrustados (funciona abriendolo desde cualquier lado)."""
    payload = {"meta": meta, "avisos": sorted(avisos.values(), key=lambda a: a["total_clp"])}
    datos = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    html = PLANTILLA.read_text(encoding="utf-8").replace("/*__DATOS__*/null", datos, 1)
    tmp = DASHBOARD.with_suffix(".tmp")
    tmp.write_text(html, encoding="utf-8")
    tmp.replace(DASHBOARD)


def guardar(avisos):
    tmp = AVISOS_JSON.with_suffix(".tmp")
    tmp.write_text(json.dumps(avisos, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(AVISOS_JSON)


def publicar_web(cfg):
    """Sube dashboard.html a la rama gh-pages del repo (GitHub Pages) como un unico commit que se reescribe."""
    pub = cfg.get("publicar_github") or {}
    if not pub.get("activo") or not DASHBOARD.exists():
        return
    tmp = Path(tempfile.mkdtemp(prefix="buscador_web_"))
    try:
        shutil.copy(DASHBOARD, tmp / "index.html")
        (tmp / ".nojekyll").write_text("")
        git = ["git", "-C", str(tmp), "-c", "user.name=buscador", "-c", "user.email=buscador@users.noreply.github.com"]
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        for args in (["init", "-q", "-b", "gh-pages"], ["add", "-A"], ["commit", "-qm", f"dashboard {ahora()}"],
                     ["push", "-qf", f"https://github.com/{pub['repo']}.git", "gh-pages"]):
            r = subprocess.run(git + args, capture_output=True, text=True, timeout=180, creationflags=flags)
            if r.returncode:
                log(f"No pude publicar en GitHub ({args[0]}): {r.stderr.strip()[:200]}")
                return
        log(f"Dashboard publicado en {pub.get('url', pub['repo'])}")
    except Exception as e:
        log(f"No pude publicar en GitHub: {e.__class__.__name__} {e}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def clp(n):
    return "$" + f"{n:,.0f}".replace(",", ".") if n is not None else "-"


def toast(titulo, texto):
    """Notificacion nativa de Windows; al hacer clic abre el dashboard. En la nube (Linux) no hace nada."""
    if os.name != "nt":
        return

    def x(s):
        return escape(s, quote=True).replace("'", "&apos;")
    xml = (f"<toast activationType='protocol' launch='{x(DASHBOARD.as_uri())}'><visual><binding template='ToastGeneric'>"
           f"<text>{x(titulo)}</text><text>{x(texto)}</text></binding></visual></toast>")
    ps = (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null;"
        "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] > $null;"
        "$d = New-Object Windows.Data.Xml.Dom.XmlDocument; $d.LoadXml(@'\n" + xml + "\n'@);"
        "$app = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe';"
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show([Windows.UI.Notifications.ToastNotification]::new($d))"
    )
    try:
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                       capture_output=True, timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception as e:
        log(f"No pude mostrar notificacion de Windows: {e}")


def html_correo(nuevos, bajas, meta):
    def r(par):
        lo, hi = par
        if lo is None:
            return "?"
        return f"{lo:.0f}" if lo == hi else f"{lo:.0f}-{hi:.0f}"

    def tarjeta(a, etiqueta):
        dorm, banos = r(a["dorm"]), r(a["banos"])
        m2 = f'{r(a["m2"])} m²' if a["m2"][0] else ""
        ggcc = clp(a["ggcc_clp"]) + (" (estimado)" if a["ggcc_estimado"] else "")
        extra = " · ".join(x for x in [
            f'{a["estacionamientos"]} estac.' if a.get("estacionamientos") else "",
            f'{a["bodegas"]} bod.' if a.get("bodegas") else "",
            "mascotas OK" if a.get("mascotas") == "Sí" else "",
            "multifamily" if a["multifamily"] else ""] if x)
        foto = f'<img src="{a["foto"]}" width="160" style="border-radius:6px;display:block">' if a.get("foto") else ""
        return f"""
        <tr><td style="padding:10px;border-bottom:1px solid #e5e5e5;vertical-align:top">{foto}</td>
        <td style="padding:10px;border-bottom:1px solid #e5e5e5;vertical-align:top;font-family:Arial,sans-serif;font-size:14px">
          <span style="background:#0b6e4f;color:#fff;font-size:11px;padding:2px 6px;border-radius:4px">{etiqueta}</span><br>
          <b>{escape(a["titulo"])}</b><br><span style="color:#666">{escape(a["direccion"])}</span><br>
          <b style="font-size:16px">{clp(a["total_clp"])}</b> total
          <span style="color:#666">(arriendo {"~" if a.get("precio_estimado") else ""}{clp(a["precio_clp"])} + GGCC {ggcc})</span><br>
          {dorm} D · {banos} B · {m2} {('· ' + extra) if extra else ''}<br>
          <a href="{a["url"]}">Ver aviso →</a></td></tr>"""
    filas = "".join(tarjeta(a, "NUEVO") for a in nuevos) + "".join(tarjeta(a, "BAJÓ PRECIO") for a in bajas)
    return f"""<div style="font-family:Arial,sans-serif;max-width:680px">
      <h2 style="margin-bottom:4px">Deptos en arriendo: {len(nuevos)} nuevos, {len(bajas)} bajaron de precio</h2>
      <p style="color:#666;margin-top:0">Las Condes / Providencia oriente · 2-3D · 2B · total ≤ {clp(meta["presupuesto"])} · UF {clp(meta["uf"])}</p>
      <table cellspacing="0" cellpadding="0">{filas}</table></div>"""


def correo(cfg, asunto, html):
    """Usuario, clave y destinatarios salen de variables de entorno (.env local o secretos de GitHub)."""
    c = cfg["notificaciones"]["correo"]
    usuario = os.environ.get("SMTP_USUARIO") or c.get("usuario")
    clave = os.environ.get("SMTP_PASSWORD")
    destinos = [x.strip() for x in (os.environ.get("CORREO_DESTINO") or "").split(",") if x.strip()] \
        or c.get("destinatarios") or ([usuario] if usuario else [])
    if not (c.get("activo") or os.environ.get("SMTP_PASSWORD")):
        return
    if not usuario or not clave:
        log("Correo activo pero falta SMTP_USUARIO o SMTP_PASSWORD; no se envia")
        return
    url = os.environ.get("DASHBOARD_URL") or (cfg.get("publicar_github") or {}).get("url")
    if url:
        html += f'<p style="font-family:Arial,sans-serif"><a href="{url}">Abrir dashboard con todas las opciones →</a></p>'
    msg = MIMEMultipart("alternative")
    msg["Subject"], msg["From"], msg["To"] = asunto, usuario, ", ".join(destinos)
    msg.attach(MIMEText(html, "html", "utf-8"))
    try:
        with smtplib.SMTP(c["smtp_host"], c["smtp_port"], timeout=30) as s:
            s.starttls()
            s.login(usuario, clave)
            s.sendmail(usuario, destinos, msg.as_string())
        log(f"Correo enviado a {len(destinos)} destinatario(s)")
    except Exception as e:
        log(f"Error enviando correo: {e.__class__.__name__}")


def notificar(cfg, nuevos, bajas, meta, carga_inicial):
    if not nuevos and not bajas:
        return
    if carga_inicial:
        titulo = f"Carga inicial: {len(nuevos)} deptos cumplen tus filtros"
        nuevos = sorted(nuevos, key=lambda a: a["total_clp"])[:20]
    else:
        titulo = f"{len(nuevos)} deptos nuevos" + (f", {len(bajas)} bajaron de precio" if bajas else "")
    mejor = min(nuevos + bajas, key=lambda a: a["total_clp"])
    if cfg["notificaciones"].get("windows", True):
        toast(titulo, f"Desde {clp(mejor['total_clp'])} total · {mejor['titulo'][:60]}")
    correo(cfg, titulo, html_correo(nuevos[:30], bajas[:10], meta))


# ---------------------------------------------------------------- principal

def tomar_lock():
    DATA.mkdir(exist_ok=True)
    if LOCK.exists() and time.time() - LOCK.stat().st_mtime < 90 * 60:
        return False
    LOCK.write_text(str(os.getpid()))
    return True


def construir_meta(cfg, avisos, uf, dolar, previo, corrida):
    meta = {
        "generado": ahora(),
        "uf": uf, "dolar": dolar,
        "presupuesto": cfg["busqueda"]["presupuesto_total_clp"],
        "limites": cfg.get("limites_por_comuna", {}),
        "activos": sum(1 for a in avisos.values() if a.get("activo")),
        "cumplen": sum(1 for a in avisos.values() if a.get("activo") and a.get("cumple") and not a.get("duplicado_de")),
        "carga_inicial_completa": previo.get("carga_inicial_completa", False),
        "corridas": (previo.get("corridas", []) + ([corrida] if corrida else []))[-50:],
    }
    return meta


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-paginas", type=int)
    ap.add_argument("--solo-dashboard", action="store_true")
    ap.add_argument("--probar-aviso", action="store_true")
    args = ap.parse_args()

    cargar_env()
    cfg = cargar_config()
    DATA.mkdir(exist_ok=True)
    avisos = json.loads(AVISOS_JSON.read_text(encoding="utf-8")) if AVISOS_JSON.exists() else {}
    meta_previa = avisos.pop("_meta", {})

    if args.probar_aviso:
        muestra = [a for a in avisos.values() if a.get("cumple") and a.get("activo")][:3]
        toast("Prueba buscador de deptos", "Si ves esto, las notificaciones funcionan. Clic para abrir el dashboard.")
        if muestra:
            correo(cfg, "Prueba buscador de deptos", html_correo(muestra, [], meta_previa or {"presupuesto": cfg["busqueda"]["presupuesto_total_clp"], "uf": 0}))
        return

    if args.solo_dashboard:
        uf, dolar = meta_previa.get("uf", cfg["uf_respaldo"]), meta_previa.get("dolar", cfg["dolar_respaldo"])
        for a in avisos.values():
            evaluar(a, cfg, uf, dolar)
        marcar_duplicados(avisos)
        analizar(avisos, cfg, uf)
        meta = construir_meta(cfg, avisos, uf, dolar, meta_previa, None)
        escribir_dashboard(avisos, meta)
        guardar({**avisos, "_meta": meta})
        log("dashboard.html regenerado")
        publicar_web(cfg)
        return

    if not tomar_lock():
        log("Otra ejecucion sigue corriendo; salgo")
        return
    t0 = time.time()
    try:
        # Sigue siendo carga inicial hasta completar un recorrido entero (por si la primera se corta)
        carga_inicial = not meta_previa.get("carga_inicial_completa")
        uf, dolar = indicadores(cfg, meta_previa)
        cli = Cliente(cfg["pausa_entre_requests_seg"])
        max_pag = args.max_paginas or cfg["busqueda"]["max_paginas_por_comuna"]
        log(f"Inicio busqueda (UF {uf:,.0f}){' - CARGA INICIAL' if carga_inicial else ''}")

        vistos, completas, bloqueado = set(), True, None
        tarjetas = []
        try:
            for slug, comuna in cfg["busqueda"]["comunas"].items():
                t, ok = buscar_comuna(cli, cfg, slug, comuna, max_pag)
                tarjetas += t
                completas &= ok
        except Bloqueado as e:
            bloqueado, completas = str(e), False
            log(f"BLOQUEO: {e}")

        nuevos, bajas, detalles = [], [], 0
        limite_detalle = datetime.now() - timedelta(days=cfg["refrescar_detalle_cada_dias"])
        for t in tarjetas:
            if t["id"] in vistos:
                continue
            vistos.add(t["id"])
            prev = avisos.get(t["id"])
            a = {**(prev or {}), **t}
            a.setdefault("primera_vez", ahora())
            a["ultima_vez"] = ahora()
            a["activo"] = True
            if prev is None:
                a["carga_inicial"] = carga_inicial
                a["historial_precio"] = [[ahora()[:10], t["precio"], t["moneda"]]]
            elif (prev.get("precio"), prev.get("moneda")) != (t["precio"], t["moneda"]):
                a["historial_precio"] = prev.get("historial_precio", []) + [[ahora()[:10], t["precio"], t["moneda"]]]
                if prev.get("moneda") == t["moneda"] and (t["precio"] or 0) < (prev.get("precio") or 0):
                    a["bajo_precio"] = ahora()
            cambio_precio = prev is not None and prev.get("precio") != t["precio"]
            sin_detalle = not a.get("detalle_en") or datetime.fromisoformat(a["detalle_en"]) < limite_detalle
            excluido_mf = t["multifamily"] and not cfg["busqueda"].get("incluir_edificios_multifamily", True)
            if (sin_detalle or cambio_precio) and not bloqueado and not excluido_mf:
                try:
                    html = cli.get(t["url"])
                    if html:
                        a.update(parsear_detalle(html))
                        a["detalle_en"] = ahora()
                        detalles += 1
                except Bloqueado as e:
                    bloqueado = str(e)
                    log(f"BLOQUEO en detalle: {e}")
                except Exception as e:
                    log(f"  no pude leer detalle {t['id']}: {e.__class__.__name__} {e}")
            cumplia = bool(prev and prev.get("cumple"))
            evaluar(a, cfg, uf, dolar)
            if a["cumple"] and (prev is None or not cumplia):
                nuevos.append(a)
            elif a["cumple"] and a.get("bajo_precio", "")[:10] == ahora()[:10] and cambio_precio:
                bajas.append(a)
            avisos[t["id"]] = a
            if detalles and detalles % 25 == 0:
                guardar({**avisos, "_meta": meta_previa})
                marcar_duplicados(avisos)
                analizar(avisos, cfg, uf)
                escribir_dashboard(avisos, construir_meta(cfg, avisos, uf, dolar, meta_previa, None))
                log(f"  ... {detalles} fichas leidas")

        dados_de_baja = 0
        if completas and tarjetas:
            for k, a in avisos.items():
                if k not in vistos and a.get("activo"):
                    a["activo"], a["baja_en"] = False, ahora()
                    dados_de_baja += 1
        else:
            for a in avisos.values():
                evaluar(a, cfg, uf, dolar)
        marcar_duplicados(avisos)
        analizar(avisos, cfg, uf)
        nuevos = [a for a in nuevos if not a.get("duplicado_de")]
        bajas = [a for a in bajas if not a.get("duplicado_de")]

        corrida ={"fecha": ahora(), "segundos": round(time.time() - t0), "tarjetas": len(vistos),
                   "fichas": detalles, "nuevos": len(nuevos), "bajas_precio": len(bajas),
                   "dados_de_baja": dados_de_baja, "bloqueo": bloqueado}
        meta = construir_meta(cfg, avisos, uf, dolar, meta_previa, corrida)
        meta["carga_inicial_completa"] = meta_previa.get("carga_inicial_completa") or (completas and not bloqueado)
        guardar({**avisos, "_meta": meta})
        escribir_dashboard(avisos, meta)
        log(f"Fin: {len(vistos)} avisos, {detalles} fichas, {meta['cumplen']} cumplen, "
            f"{len(nuevos)} nuevos, {len(bajas)} bajas de precio, {dados_de_baja} dados de baja "
            f"({corrida['segundos']} s)")
        notificar(cfg, nuevos, bajas, meta, carga_inicial)
        publicar_web(cfg)
        if bloqueado and cfg["notificaciones"].get("windows", True):
            toast("Buscador de deptos: el portal bloqueó la consulta", "Se reintentará en la próxima ejecución.")
    finally:
        LOCK.unlink(missing_ok=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"ERROR no controlado: {e.__class__.__name__}: {e}")
        raise
