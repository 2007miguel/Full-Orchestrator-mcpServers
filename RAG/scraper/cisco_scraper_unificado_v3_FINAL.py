# NUEVAS CORRECIONES - OS/VERSION FALTANTES 

# cisco_scraper_unificado.py
#
# Pipeline UNIFICADO IOS XE + IOS XR: URL base de Cisco Configuration Guides
# -> JSONL chunks.
#
# Flujo:
#   URL base -> secciones OS/versión -> guías -> HTML de cada guía
#           -> tablas (Command/Purpose) + tablas Procedure (stepTable) -> chunks JSONL
#
# Cada chunk es de uno de dos tipos (campo block_type):
#   - "step_table":   procedimiento CLI desde una tabla Command or Action | Purpose
#                     (commands numerados + purpose numerado + examples).
#   - "procedure":    procedimiento "Procedure / Step N" (table.stepTable). La CLI
#                     de cada step va en examples (numerada por step, con prompts);
#                     la narrativa explicativa de cada step va en purpose (numerada).
#                     commands queda vacío en este tipo.
#
# El pase 'procedure' corre igual para XE y XR (es agnóstico del OS), así que
# captura los bloques Procedure presentes en ambas familias de guías.
#
# Metadata (device_type, product): lógica portada de la versión de XE del equipo:
#   applies-products -> breadcrumb de la URL base -> breadcrumb de la guía ->
#   paréntesis del título -> inferencia por keywords.
#
# Campos de cada chunk:
#   section_id, device_type, product, os, version,
#   configuration_guide, chapter, section, block_type, commands, purpose, examples

from bs4 import BeautifulSoup
from urllib.parse import urljoin
import httpx
import os
import json
import re
import sys
import time
import hashlib
import unicodedata
from contextlib import redirect_stdout


# ============================================================
# Mapas de inferencia (XE + XR)
# ============================================================

# Palabras en el breadcrumb de categoría -> device_type
BREADCRUMB_DEVICE_TYPE_MAP = {
    "switches":  "switch",
    "routers":   "router",
    "wireless":  "wireless",
    "firewalls": "firewall",
    "security":  "security",
}

# Palabras en título/texto de producto -> device_type (fallback)
TITLE_DEVICE_TYPE_KEYWORDS = {
    "switch":   "switch",
    "switches": "switch",
    "catalyst": "switch",
    "nexus":    "switch",
    "router":   "router",
    "routers":  "router",
    "isr":      "router",
    "asr":      "router",
    "ncs":      "router",
    "firewall": "firewall",
    "asa":      "firewall",
    "ftd":      "firewall",
}


# ============================================================
# Limpieza de texto
# ============================================================

def limpiar_espacios(texto):
    return re.sub(r"\s+", " ", texto).strip()


def limpiar_command_or_action(celda):
    for br in celda.find_all("br"):
        br.replace_with("\n")
    for tag in celda.find_all(["p", "div", "pre", "section"]):
        tag.insert_before("\n")
        tag.insert_after("\n")

    texto = celda.get_text(" ", strip=True)
    texto = re.sub(r"[ \t]+", " ", texto)
    texto = re.sub(r" *\n *", "\n", texto)
    texto = re.sub(r"\s*Example:\s*", "\nExample:\n", texto, flags=re.IGNORECASE)
    texto = re.sub(r"(Router(?:\([^)]*\))?#|Device(?:\([^)]*\))?#)\s*\n\s*", r"\1 ", texto)
    texto = re.sub(r"(?<!^)\s+(?=(?:Router|Device)(?:\([^)]*\))?#\s+\S)", "\n", texto)
    texto = re.sub(
        r"((?:Router|Device)\([^)]*\)#\s+banner\s+[^\n]*?\s+X)\s+(Enter TEXT message\.)",
        r"\1\n\2", texto, flags=re.IGNORECASE
    )
    texto = re.sub(r"(End with the character\s+'X'\.)\s+(--)", r"\1\n\2", texto, flags=re.IGNORECASE)
    texto = re.sub(r"(--[^-\n].*?--)\s+(X)(?=\s*(?:Router|Device)|\s*$)", r"\1\n\2", texto)

    lineas = [limpiar_espacios(l) for l in texto.splitlines() if limpiar_espacios(l)]
    return "\n".join(lineas)


def limpiar_purpose(celda):
    return limpiar_espacios(celda.get_text(" ", strip=True))


# ============================================================
# Utilidades de chunk
# ============================================================

def clean_text(value):
    if not value:
        return ""
    text = str(value).replace("\r\n", "\n").replace("\r", "\n")
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    return "\n".join(lines).strip()


def parse_command_and_example(raw):
    text = clean_text(raw)
    if not text:
        return "", ""
    parts = re.split(r"(?i)\bExample:\s*", text, maxsplit=1)
    command = " ".join(parts[0].strip().split())
    example = clean_text(parts[1].strip()) if len(parts) > 1 else ""
    return command, example


def slugify(value, max_len=60):
    value = unicodedata.normalize("NFKD", str(value)).encode("ascii", "ignore").decode("ascii")
    value = re.sub(r"[^a-z0-9]+", "_", value.lower())
    return re.sub(r"_+", "_", value).strip("_")[:max_len]


def make_section_id(product, guide_name, chapter_name, section_name):
    raw = "||".join([product, guide_name, chapter_name, section_name])
    digest = hashlib.md5(raw.encode("utf-8")).hexdigest()[:8]
    return f"SECTION_{slugify(section_name, 45)}_{digest}"


def build_numbered_commands(commands):
    return "\n".join(f"{i}. {cmd}" for i, cmd in enumerate(commands, 1) if cmd)


def build_plain_examples(examples):
    return "\n".join(e for e in examples if e)


def build_numbered_purposes(purposes):
    return "\n".join(f"{i}. {p}" for i, p in enumerate(purposes, 1) if p)


# ============================================================
# HTTP
# ============================================================

def obtener_html(url, client, referer=None, max_reintentos=3):
    headers_extra = {"Referer": referer} if referer else {}
    for intento in range(1, max_reintentos + 1):
        try:
            r = client.get(url, headers=headers_extra)
            if r.status_code == 403:
                print(f"    403 intento {intento}: {url}")
                time.sleep(3 * intento)
                continue
            if r.status_code == 429:
                print(f"    429 intento {intento}: {url}")
                time.sleep(5 * intento)
                continue
            r.raise_for_status()
            return r.text
        except httpx.HTTPStatusError as e:
            print(f"    HTTP error intento {intento}: {e}")
            time.sleep(3 * intento)
        except httpx.RequestError as e:
            print(f"    Conexión error intento {intento}: {e}")
            time.sleep(3 * intento)
    raise Exception(f"Fallo tras {max_reintentos} intentos: {url}")


def make_client(referer_url):
    return httpx.Client(
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9,es;q=0.8",
            "Accept-Encoding": "gzip, deflate, br",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Upgrade-Insecure-Requests": "1",
            "Referer": referer_url,
        },
        timeout=60.0,
        follow_redirects=True,
        http2=True,
    )


# ============================================================
# Extracción de metadatos de dispositivo y producto
# ============================================================

def _inferir_device_type_desde_texto(texto):
    """Infiere un device_type buscando keywords en el texto (lowercase)."""
    texto_lower = texto.lower()
    for keyword, dtype in TITLE_DEVICE_TYPE_KEYWORDS.items():
        if keyword in texto_lower:
            return dtype
    return ""


def _inferir_device_types_desde_textos(textos):
    """Infiere varios device_types distintos de una lista de textos, unidos por ', '."""
    encontrados = []
    for texto in textos:
        dtype = _inferir_device_type_desde_texto(texto)
        if dtype and dtype not in encontrados:
            encontrados.append(dtype)
    return ", ".join(encontrados)


def _es_item_software_ios(texto):
    """¿El ítem de la lista 'applies to' es software (IOS/NX-OS) y no un producto?"""
    return bool(re.search(r"\bios\b|nx-os|software", texto.lower()))


def extraer_metadata_desde_applies_products(soup):
    """
    device_type y product desde la sección 'This Document Applies to These Products'
    (h3 + ul.eot-tdatp-list). Descarta ítems de software (IOS/NX-OS) y deduplica.
    """
    titulo_applies = soup.find(
        lambda tag: tag.name == "h3"
        and "this document applies to these products"
        in limpiar_espacios(tag.get_text(" ", strip=True)).lower()
    )
    if not titulo_applies:
        return "", ""

    lista = titulo_applies.find_next_sibling(
        "ul", class_=lambda value: value and "eot-tdatp-list" in value
    )
    if not lista:
        return "", ""

    productos = []
    vistos = set()
    for item in lista.find_all("li"):
        texto = limpiar_espacios(item.get_text(" ", strip=True))
        if not texto or _es_item_software_ios(texto):
            continue
        clave = texto.lower()
        if clave in vistos:
            continue
        vistos.add(clave)
        productos.append(texto)

    product = ", ".join(productos)
    device_type = _inferir_device_types_desde_textos(productos)
    return device_type, product


def extraer_os_version_desde_applies_products(soup):
    titulo_applies = soup.find(
        lambda tag: tag.name == "h3"
        and "this document applies to these products"
        in limpiar_espacios(tag.get_text(" ", strip=True)).lower()
    )
    if not titulo_applies:
        return "", ""

    lista = titulo_applies.find_next_sibling(
        "ul", class_=lambda value: value and "eot-tdatp-list" in value
    )
    if not lista:
        return "", ""

    for item in lista.find_all("li"):
        texto = limpiar_espacios(item.get_text(" ", strip=True))
        if not texto or not _es_item_software_ios(texto):
            continue
        os_detectado, version_detectada = extraer_os_version_desde_titulo(texto)
        if os_detectado or version_detectada:
            return os_detectado, version_detectada
    return "", ""


def extraer_os_version_desde_url_guia(guide_url):
    m = re.search(r"/(?:xe|xr)-(\d+(?:-\d+)*(?:-x)?)/", guide_url, re.IGNORECASE)
    if not m:
        return "", ""

    os_token = re.search(r"/(xe|xr)-", guide_url, re.IGNORECASE).group(1).upper()
    version = m.group(1).replace("-", ".")
    if version.endswith(".x"):
        version = version[:-2] + ".x"
    return f"Cisco IOS {os_token}", version


def extraer_os_version_desde_breadcrumb(soup):
    breadcrumb = soup.find("ul", attrs={"itemtype": re.compile(r"BreadcrumbList", re.I)})
    if not breadcrumb:
        return "", ""

    items = breadcrumb.find_all("li", attrs={"itemprop": "itemListElement"})
    if not items:
        items = breadcrumb.find_all("li")

    for item in items:
        texto = limpiar_espacios(item.get_text(" ", strip=True))
        os_detectado, version_detectada = extraer_os_version_desde_titulo(texto)
        if os_detectado or version_detectada:
            return os_detectado, version_detectada
    return "", ""


def extraer_metadata_desde_breadcrumb(soup):
    """
    device_type y product desde el breadcrumb:
      [#] [Support] [Product Support] [CATEGORY] [Product] [Configuration Guides]
    CATEGORY -> device_type; el primer ítem tras la categoría (que no sea
    'Configuration Guide...') -> product.
    """
    breadcrumb = soup.find("ul", attrs={"itemtype": re.compile(r"BreadcrumbList", re.I)})
    if not breadcrumb:
        return "", ""

    items = breadcrumb.find_all("li", attrs={"itemprop": "itemListElement"})
    if not items:
        items = breadcrumb.find_all("li")
    textos = [limpiar_espacios(li.get_text(" ", strip=True)) for li in items]

    category_index = None
    device_type = ""
    for i, texto in enumerate(textos):
        dtype = BREADCRUMB_DEVICE_TYPE_MAP.get(texto.lower(), "")
        if dtype:
            device_type = dtype
            category_index = i
            break

    if category_index is None:
        return "", ""

    product = ""
    for texto in textos[category_index + 1:]:
        if not texto or "configuration guide" in texto.lower():
            continue
        product = texto
        break

    return device_type, product


def extraer_product_desde_titulo(titulo):
    """Product desde el paréntesis final del título -> 'Catalyst 9600 Switches' / 'Cisco 8000 Series Routers'."""
    m = re.search(r'\(([^)]+)\)\s*$', titulo)
    return m.group(1).strip() if m else ""


def extraer_os_version_desde_titulo(titulo):
    """
    os y version desde el título de la guía (IOS XE o IOS XR):
      'Cisco IOS XE 17.15.x', 'Cisco IOS XE Dublin 17.16.x'
      '..., IOS XR Release 26.x.x', 'Cisco IOS XR Releases' (sin versión)
    """
    version_pat = r'\d+(?:\.(?:\d+|x))*'
    m = re.search(
        rf'(?:Cisco\s+)?IOS\s+(XE|XR)\s+(?:[A-Za-z]+\s+)*?({version_pat})',
        titulo, re.IGNORECASE
    )
    if m:
        return f"Cisco IOS {m.group(1).upper()}", m.group(2)
    m = re.search(r'(?i)(?:Cisco\s+)?IOS\s+(XE|XR)\b', titulo)
    if m:
        return f"Cisco IOS {m.group(1).upper()}", ""
    return "", ""


def extraer_metadata_guia(
    soup, titulo_guia, os_version_del_heading, metadata_url_base=("", ""), guide_url=""
):
    """
    Determina device_type, product, os y version de una guía.

    Prioridad device_type/product:
      applies-products -> metadata de la URL base -> breadcrumb de la guía ->
      paréntesis del título (product) -> inferencia por keywords (device_type).
    os, version: del heading de la URL base; si faltan, desde el título,
    applies-products, breadcrumb de la guía y la URL de la guía.
    """
    os_conocido, version_conocida = os_version_del_heading

    # -- 1) Sección "This Document Applies to These Products" --
    device_type, product = extraer_metadata_desde_applies_products(soup)

    # -- 2) Metadata de la URL base (breadcrumb base) --
    if not device_type or not product:
        device_type_base, product_base = metadata_url_base
        if not device_type:
            device_type = device_type_base
        if not product:
            product = product_base

    # -- 3) Breadcrumb de la guía --
    if not device_type or not product:
        device_type_guia, product_guia = extraer_metadata_desde_breadcrumb(soup)
        if not device_type:
            device_type = device_type_guia
        if not product:
            product = product_guia

    # -- 4) Product desde el paréntesis del título --
    if not product:
        product = extraer_product_desde_titulo(titulo_guia)

    # -- 5) device_type inferido del título + product --
    if not device_type:
        device_type = _inferir_device_type_desde_texto(titulo_guia + " " + product)

    # -- os y version --
    os_final = os_conocido
    version_final = version_conocida
    if not os_final or not version_final:
        os_del_titulo, ver_del_titulo = extraer_os_version_desde_titulo(titulo_guia)
        if not os_final:
            os_final = os_del_titulo
        if not version_final:
            version_final = ver_del_titulo

    if not os_final or not version_final:
        os_del_applies, ver_del_applies = extraer_os_version_desde_applies_products(soup)
        if not os_final:
            os_final = os_del_applies
        if not version_final:
            version_final = ver_del_applies

    if not os_final or not version_final:
        os_del_breadcrumb, ver_del_breadcrumb = extraer_os_version_desde_breadcrumb(soup)
        if not os_final:
            os_final = os_del_breadcrumb
        if not version_final:
            version_final = ver_del_breadcrumb

    if not os_final or not version_final:
        os_de_url, ver_de_url = extraer_os_version_desde_url_guia(guide_url)
        if not os_final:
            os_final = os_de_url
        if not version_final:
            version_final = ver_de_url

    if not os_final:
        os_final = "not defined"
    if not version_final:
        version_final = "not defined"

    return device_type, product, os_final, version_final


# ============================================================
# Extracción de guías desde la URL base
# ============================================================

def extraer_os_version_desde_heading(texto_heading):
    """
    os y version desde el texto del div.heading de la URL base (IOS XE o IOS XR):
      'Cisco IOS XE 17.16.1', 'Cisco IOS XE Dublin 17.12.1'
      'Cisco 8000 Series Routers, IOS XR Release 26.x.x'
      'Cisco NCS 5500 Series Routers, Release 7.6.x'  (sin token de OS)
    """
    version_pat = r'\d+(?:\.(?:\d+|x))*'
    m = re.search(
        rf'(?i)(?:cisco\s+)?ios\s+(xe|xr)\s+(?:[A-Za-z]+\s+)*?({version_pat})',
        texto_heading
    )
    if m:
        return f"Cisco IOS {m.group(1).upper()}", m.group(2)
    # Sólo 'Release N.x.x' sin token de OS; el os se completa luego desde el título.
    m = re.search(rf'(?i)\brelease\s+({version_pat})', texto_heading)
    if m:
        return "", m.group(1)
    return "", ""


def extraer_guias_desde_url_base(soup, url_base):
    """
    Identifica todas las guías de la página base. Maneja los dos layouts de Cisco:
      1. div.heading + <a> hermanos (versión en el texto del heading).
      2. ul.listing / ul.listing4 (os/version se infieren luego desde el título).
    Deduplica por guide_url. Retorna [{guide_title, guide_url, os, version}].
    """
    guias = []
    vistos = set()

    def agregar(titulo, href, os_h, ver_h, solo_docs=False):
        titulo = limpiar_espacios(titulo)
        if not titulo:
            return
        es_doc = "/td/docs/" in href
        if solo_docs and not es_doc:
            return
        if not es_doc and not href.startswith("http"):
            return
        full = urljoin(url_base, href)
        if full in vistos:
            return
        vistos.add(full)
        guias.append({
            "guide_title": titulo,
            "guide_url": full,
            "os": os_h,
            "version": ver_h,
        })

    # -- Estrategia 1: secciones div.heading con versión en el texto --
    for heading in soup.find_all("div", class_="heading"):
        texto_heading = limpiar_espacios(heading.get_text(" ", strip=True))
        os_heading, version_heading = extraer_os_version_desde_heading(texto_heading)

        sibling = heading.next_sibling
        while sibling:
            if hasattr(sibling, "name") and sibling.name is not None:
                if sibling.name == "div" and "heading" in (sibling.get("class") or []):
                    break
                for a in sibling.find_all("a", href=True):
                    agregar(a.get_text(" ", strip=True), a["href"],
                            os_heading, version_heading)
            sibling = sibling.next_sibling

    # -- Estrategia 2: layout ul.listing / ul.listing4 --
    for ul in soup.select("ul.listing, ul.listing4"):
        for a in ul.find_all("a", href=True):
            agregar(a.get_text(" ", strip=True), a["href"], "", "", solo_docs=True)

    return guias


# ============================================================
# Extracción de chapters desde TOC de una guía
# ============================================================

def obtener_configuration_guide(soup):
    titulo = soup.find("h1", id="fw-pagetitle")
    return limpiar_espacios(titulo.get_text(" ", strip=True)) if titulo else ""


def obtener_texto_directo_li(li):
    a = li.find("a", href=True, recursive=False)
    if a:
        return limpiar_espacios(a.get_text(" ", strip=True))
    btn = li.find("button", recursive=False)
    if btn:
        return limpiar_espacios(btn.get_text(" ", strip=True))
    return ""


def recorrer_toc_li(li, url_base, ruta_padre=None):
    if ruta_padre is None:
        ruta_padre = []
    chapters = []
    texto = obtener_texto_directo_li(li)
    ruta = ruta_padre + ([texto] if texto else [])

    a = li.find("a", href=True, recursive=False)
    if a:
        chapters.append({
            "chapter_name_toc": " - ".join(ruta),
            "url": urljoin(url_base, a["href"]),
        })

    for ul in li.find_all("ul", recursive=False):
        for li_hijo in ul.find_all("li", recursive=False):
            chapters.extend(recorrer_toc_li(li_hijo, url_base, ruta))

    return chapters


def obtener_urls_chapters(soup, url_base):
    book_toc = soup.find("ul", id="bookToc")
    if not book_toc:
        return []
    chapters = []
    for li in book_toc.find_all("li", recursive=False):
        chapters.extend(recorrer_toc_li(li, url_base))
    return chapters


# ============================================================
# Validación de tablas y extracción de sección
# ============================================================

def extraer_encabezados(tabla):
    primera_fila = tabla.find("tr")
    if not primera_fila:
        return []
    return [limpiar_espacios(th.get_text(" ", strip=True)) for th in primera_fila.find_all(["th", "td"])]


def tabla_valida(headers):
    return (
        len(headers) >= 3
        and headers[1].strip().lower() == "command or action"
        and headers[2].strip().lower() == "purpose"
    )


def es_titulo_capitulo(tag):
    if not tag or tag.name != "h2":
        return False
    texto = limpiar_espacios(tag.get_text(" ", strip=True)).lower()
    clases = tag.get("class", [])
    return "chapter" in texto or any("chapter" in c.lower() for c in clases)


def es_titulo_ignorable(tag):
    if not tag:
        return True
    texto = limpiar_espacios(tag.get_text(" ", strip=True)).lower()
    return not texto or texto in {"summary steps", "detailed steps", "before you begin", "procedure"}


def obtener_section_de_elemento(elemento):
    """Nombre de la sección que precede a un elemento (tabla o stepTable):
    el primer heading h2/h3/h4 anterior que no sea título de capítulo ni ruido."""
    heading = elemento.find_previous(["h2", "h3", "h4"])
    while heading:
        if es_titulo_capitulo(heading) or es_titulo_ignorable(heading):
            heading = heading.find_previous(["h2", "h3", "h4"])
            continue
        texto = limpiar_espacios(heading.get_text(" ", strip=True))
        if heading.name == "h4":
            h3 = heading.find_previous("h3")
            while h3 and es_titulo_ignorable(h3):
                h3 = h3.find_previous("h3")
            if h3:
                return f"{limpiar_espacios(h3.get_text(' ', strip=True))} - {texto}"
        return texto
    return ""


# ============================================================
# Extracción de celdas
# ============================================================

def clase_contiene(tag, nombre):
    return any(nombre in c for c in tag.get("class", []))


def extraer_command_y_purpose(fila):
    """
    Retorna (command_or_action, purpose) desde una fila de tabla.
    Prioriza clases step--command / step--purpose; fallback por posición.
    """
    celdas = fila.find_all(["td", "th"])
    if not celdas:
        return None, None

    celda_command = fila.find(
        lambda t: t.name in ["td", "th"] and clase_contiene(t, "step--command")
    )
    celda_purpose = fila.find(
        lambda t: t.name in ["td", "th"] and clase_contiene(t, "step--purpose")
    )

    if not celda_command and len(celdas) >= 2:
        celda_command = celdas[1]
    if not celda_purpose and len(celdas) >= 3:
        celda_purpose = celdas[2]

    if not celda_command:
        return None, None

    raw_command = limpiar_command_or_action(celda_command)
    raw_purpose = limpiar_purpose(celda_purpose) if celda_purpose else None
    return raw_command, raw_purpose


# ============================================================
# Conversión de tablas a chunks (en memoria)
# ============================================================

def extraer_chunks_de_html_chapter(
    html_chapter, configuration_guide, device_specification, chapter_name_toc=None
):
    """
    Procesa el HTML de un chapter y retorna (chunks, total_tablas, total_sections).
    Pase 1: tablas Command or Action | Purpose -> chunks 'step_table'.
    Pase 2: tablas Procedure (table.stepTable) -> chunks 'procedure'.
    """
    soup = BeautifulSoup(html_chapter, "html.parser")
    tablas = soup.find_all("table")

    sections_buffer = {}
    section_order = []
    total_tablas = 0

    for tabla in tablas:
        headers = extraer_encabezados(tabla)
        if not tabla_valida(headers):
            continue

        total_tablas += 1
        chapter_name = chapter_name_toc or ""
        section_name = obtener_section_de_elemento(tabla)
        section_key = (configuration_guide, chapter_name, section_name)

        if section_key not in sections_buffer:
            sections_buffer[section_key] = {
                "section_id": make_section_id(
                    device_specification.get("product", ""),
                    configuration_guide, chapter_name, section_name
                ),
                "commands": [],
                "examples": [],
                "purposes": [],
            }
            section_order.append(section_key)

        for fila in tabla.find_all("tr")[1:]:
            raw_command, raw_purpose = extraer_command_y_purpose(fila)
            if not raw_command:
                continue
            command, example = parse_command_and_example(raw_command)
            if command:
                sections_buffer[section_key]["commands"].append(command)
            if example:
                sections_buffer[section_key]["examples"].append(example)
            if raw_purpose:
                sections_buffer[section_key]["purposes"].append(raw_purpose)

    chunks = []
    for key in section_order:
        buf = sections_buffer[key]
        guide_name, chapter_name, section_name = key
        commands_text = build_numbered_commands(buf["commands"])
        examples_text = build_plain_examples(buf["examples"])
        purpose_text = build_numbered_purposes(buf["purposes"])
        if not commands_text and not examples_text:
            continue
        chunks.append({
            "section_id":          buf["section_id"],
            "device_type":         device_specification.get("device_type", ""),
            "product":             device_specification.get("product", ""),
            "os":                  device_specification.get("os", ""),
            "version":             device_specification.get("version", ""),
            "configuration_guide": guide_name,
            "chapter":             chapter_name,
            "section":             section_name,
            "block_type":          "step_table",
            "commands":            commands_text,
            "purpose":             purpose_text,
            "examples":            examples_text,
        })

    # ------------------------------------------------------------------
    # Pase 2: procedimientos "Procedure / Step N" (<table class="stepTable">)
    # ------------------------------------------------------------------
    # Por cada step: la CLI de los <pre> va a 'examples' (numerada por step,
    # conservando los prompts) y la narrativa explicativa va a 'purpose'
    # (numerada). Se numera por el número real del Step para que examples[N] y
    # purpose[N] queden alineados. commands queda vacío. Un chunk por procedimiento.
    chapter_name = chapter_name_toc or ""
    for step_table in soup.select("table.stepTable"):
        section_name = obtener_section_de_elemento(step_table)

        pasos = []   # [(num, cli, narrativa), ...]
        for fila in step_table.find_all("tr"):
            celdas = fila.find_all("td", recursive=False)
            if len(celdas) < 2:
                continue
            label = limpiar_espacios(celdas[0].get_text(" ", strip=True))
            m = re.search(r"\d+", label)
            num = m.group(0) if m else str(len(pasos) + 1)
            contenido = celdas[-1]
            cli = clean_text("\n".join(p.get_text() for p in contenido.find_all("pre")))
            for p in contenido.find_all("pre"):
                p.extract()
            narrativa = re.sub(r"\s*Example:\s*", " ", contenido.get_text(" ", strip=True))
            narrativa = limpiar_espacios(narrativa)
            if not (cli or narrativa):
                continue
            pasos.append((num, cli, narrativa))

        if not pasos:
            continue

        examples_text = "\n".join(f"{num}. {cli}" for num, cli, _ in pasos if cli)
        purpose_text  = "\n".join(f"{num}. {narr}" for num, _, narr in pasos if narr)
        if not examples_text and not purpose_text:
            continue

        proc_id = make_section_id(
            device_specification.get("product", ""),
            configuration_guide, chapter_name, section_name
        ) + "_proc_" + hashlib.md5(examples_text.encode("utf-8")).hexdigest()[:6]
        chunks.append({
            "section_id":          proc_id,
            "device_type":         device_specification.get("device_type", ""),
            "product":             device_specification.get("product", ""),
            "os":                  device_specification.get("os", ""),
            "version":             device_specification.get("version", ""),
            "configuration_guide": configuration_guide,
            "chapter":             chapter_name,
            "section":             section_name,
            "block_type":          "procedure",
            "commands":            "",
            "purpose":             purpose_text,
            "examples":            examples_text,
        })

    return chunks, total_tablas, len(section_order)


# ============================================================
# Pipeline principal
# ============================================================

def extraer_chunks_desde_url_base(url_base, ruta_salida_jsonl):
    """
    Pipeline completo partiendo de una única URL base (XE o XR):

    1. Descarga la URL base, extrae las guías y la metadata base (breadcrumb).
    2. Para cada guía, completa device_type/product/os/version desde su HTML.
    3. Recorre el TOC y descarga cada chapter.
    4. Extrae chunks (step_table + procedure) y escribe el JSONL.
    """

    total_guias_procesadas = 0
    total_chunks_global = 0

    with make_client(url_base) as client:
        with open(ruta_salida_jsonl, "w", encoding="utf-8") as out_jsonl:

            print(f"URL base: {url_base}")
            html_base = obtener_html(url_base, client)
            soup_base = BeautifulSoup(html_base, "html.parser")
            metadata_url_base = extraer_metadata_desde_breadcrumb(soup_base)

            if metadata_url_base[0]:
                print(f"device_type base : {metadata_url_base[0]}")
                print(f"product base     : {metadata_url_base[1] or '(no detectado)'}")
            else:
                print("Metadata de equipo no catalogada en breadcrumb base; "
                      "se resolverá por guía.")

            guias = extraer_guias_desde_url_base(soup_base, url_base)
            print(f"Guías encontradas en la URL base: {len(guias)}\n")

            for idx, guia_info in enumerate(guias, start=1):
                guide_title = guia_info["guide_title"]
                guide_url   = guia_info["guide_url"]
                os_heading  = guia_info["os"]
                ver_heading = guia_info["version"]

                print(f"[{idx}/{len(guias)}] {guide_title}")
                print(f"  URL: {guide_url}")

                try:
                    html_guia = obtener_html(guide_url, client, referer=url_base)
                    soup_guia = BeautifulSoup(html_guia, "html.parser")

                    device_type, product, os_final, version_final = extraer_metadata_guia(
                        soup_guia, guide_title, (os_heading, ver_heading),
                        metadata_url_base, guide_url
                    )

                    print(f"  device_type : {device_type or '(no detectado)'}")
                    print(f"  product     : {product or '(no detectado)'}")
                    print(f"  os          : {os_final or '(no detectado)'}")
                    print(f"  version     : {version_final or '(no detectada)'}")

                    device_specification = {
                        "device_type": device_type,
                        "product":     product,
                        "os":          os_final,
                        "version":     version_final,
                    }

                    configuration_guide = obtener_configuration_guide(soup_guia) or guide_title
                    chapters = obtener_urls_chapters(soup_guia, guide_url)

                    if not chapters:
                        print(f"  Sin chapters en TOC, omitiendo.\n")
                        continue

                    total_guias_procesadas += 1
                    chunks_guia = 0

                    for ch_idx, chapter_info in enumerate(chapters, start=1):
                        nombre_chapter = chapter_info["chapter_name_toc"]
                        url_chapter    = chapter_info["url"]

                        try:
                            html_chapter = obtener_html(url_chapter, client, referer=guide_url)
                            chunks, tablas, _ = extraer_chunks_de_html_chapter(
                                html_chapter, configuration_guide,
                                device_specification, nombre_chapter
                            )
                            chunks_guia += len(chunks)
                            for chunk in chunks:
                                out_jsonl.write(json.dumps(chunk, ensure_ascii=False) + "\n")
                            time.sleep(1)

                        except Exception as e:
                            print(f"    ERROR chapter [{ch_idx}] {nombre_chapter}: {e}")

                    total_chunks_global += chunks_guia
                    print(f"  Chunks generados: {chunks_guia}\n")
                    time.sleep(2)

                except Exception as e:
                    print(f"  ERROR procesando guía: {e}\n")

    print("=" * 60)
    print(f"Guías procesadas : {total_guias_procesadas}")
    print(f"Chunks generados : {total_chunks_global}")
    print(f"JSONL en         : {ruta_salida_jsonl}")
    print("=" * 60)


# ============================================================
# Ejecución
# ============================================================

# Presets por OS. Rellena la URL base de XE con la página de
# Configuration Guides de IOS XE que use tu compañero.
TARGETS = {
    "xr": {
        "url_base": "https://www.cisco.com/c/en/us/support/ios-nx-os-software/ios-xr-software/products-installation-and-configuration-guides-list.html",
        "jsonl":    "cisco_ios_xr_chunks.jsonl",
        "reporte":  "reporte_extraccion_xr.txt",
    },
    "xe": {
        "url_base": "<URL_BASE_XE>",
        "jsonl":    "cisco_ios_xe_chunks.jsonl",
        "reporte":  "reporte_extraccion_xe.txt",
    },
}


def _usage_and_exit():
    print(
        "Uso:\n"
        f"  python {os.path.basename(__file__)} <preset>            "
        f"(preset: {', '.join(TARGETS)})\n"
        f"  python {os.path.basename(__file__)} <URL_BASE> <SALIDA_JSONL> <REPORTE_TXT>",
        file=sys.stderr,
    )
    sys.exit(1)


if __name__ == "__main__":
    args = sys.argv[1:]

    if len(args) == 1 and args[0] in TARGETS:
        t = TARGETS[args[0]]
        url_base, salida_jsonl, salida_reporte = t["url_base"], t["jsonl"], t["reporte"]
        if url_base.startswith("<"):
            print(f"El preset '{args[0]}' no tiene URL base configurada todavía. "
                  f"Edita TARGETS['{args[0]}']['url_base'] o usa la forma con argumentos.",
                  file=sys.stderr)
            sys.exit(1)
    elif len(args) == 3:
        url_base, salida_jsonl, salida_reporte = args
    else:
        _usage_and_exit()

    ruta_jsonl = salida_jsonl if os.path.isabs(salida_jsonl) else os.path.join(os.getcwd(), salida_jsonl)
    ruta_reporte = salida_reporte if os.path.isabs(salida_reporte) else os.path.join(os.getcwd(), salida_reporte)

    with open(ruta_reporte, "w", encoding="utf-8") as reporte:
        with redirect_stdout(reporte):
            extraer_chunks_desde_url_base(url_base, ruta_jsonl)
