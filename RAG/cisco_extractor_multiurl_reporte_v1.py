# Extracción de tablas de Steps / Command or Action / Purpose
from bs4 import BeautifulSoup
from urllib.parse import urljoin
import httpx
import os
import csv
import re
import time
from contextlib import redirect_stdout


# ============================================================
# Limpieza de texto
# ============================================================

def limpiar_espacios(texto):
    """
    Convierte múltiples espacios, tabs y saltos de línea en un solo espacio.
    """
    return re.sub(r"\s+", " ", texto).strip()


def limpiar_step(celda):
    """
    Limpia la columna Step.
    """
    return limpiar_espacios(celda.get_text(" ", strip=True))


def limpiar_purpose(celda):
    """
    Limpia la columna Purpose como texto continuo.
    Evita saltos de línea innecesarios.
    """
    texto = celda.get_text(" ", strip=True)
    return limpiar_espacios(texto)


def limpiar_command_or_action(celda):
    """
    Limpia la columna Command or Action.

    Correcciones:
    - No separa con saltos de línea etiquetas inline como span, var, kbd, b.
    - Mantiene en una sola línea comandos como:
      configure terminal
      interface interface-id
    - Separa correctamente Example:
    - Separa prompts Cisco solo cuando realmente corresponden a líneas de ejemplo.
    """

    # Copiar comportamiento de saltos reales del HTML
    for br in celda.find_all("br"):
        br.replace_with("\n")

    # Insertar saltos solo alrededor de bloques, no de spans/var/kbd/b
    for tag in celda.find_all(["p", "div", "pre", "section"]):
        tag.insert_before("\n")
        tag.insert_after("\n")

    # IMPORTANTE:
    # Usar espacio como separador para no partir etiquetas inline.
    texto = celda.get_text(" ", strip=True)

    # Normalizar espacios, pero conservar saltos que ya insertamos
    texto = re.sub(r"[ \t]+", " ", texto)
    texto = re.sub(r" *\n *", "\n", texto)

    # Colocar Example en línea independiente
    texto = re.sub(
        r"\s*Example:\s*",
        "\nExample:\n",
        texto,
        flags=re.IGNORECASE
    )

    # Corregir casos donde el prompt queda separado del comando:
    # Device# configure terminal debe quedar en una sola línea.
    texto = re.sub(
        r"(Router(?:\([^)]*\))?#|Device(?:\([^)]*\))?#)\s*\n\s*",
        r"\1 ",
        texto
    )

    # Separar nuevos prompts si quedaron pegados después de texto previo
    # pero sin separar el prompt de su comando.
    texto = re.sub(
        r"(?<!^)\s+(?=(?:Router|Device)(?:\([^)]*\))?#\s+\S)",
        "\n",
        texto
    )

    # Corrección para ejemplos tipo banner
    texto = re.sub(
        r"((?:Router|Device)\([^)]*\)#\s+banner\s+[^\n]*?\s+X)\s+(Enter TEXT message\.)",
        r"\1\n\2",
        texto,
        flags=re.IGNORECASE
    )

    texto = re.sub(
        r"(End with the character\s+'X'\.)\s+(--)",
        r"\1\n\2",
        texto,
        flags=re.IGNORECASE
    )

    texto = re.sub(
        r"(--[^-\n].*?--)\s+(X)(?=\s*(?:Router|Device)|\s*$)",
        r"\1\n\2",
        texto
    )

    # Limpieza final: elimina líneas vacías, no rompe comandos inline
    lineas = []
    for linea in texto.splitlines():
        linea = limpiar_espacios(linea)
        if linea:
            lineas.append(linea)

    return "\n".join(lineas)


# ============================================================
# Requests
# ============================================================

def obtener_html(url, client, referer=None, max_reintentos=3):
    """
    Hace request a una URL y retorna el HTML.
    Incluye reintentos para manejar 403, 429 o errores temporales.
    """

    headers_extra = {}

    if referer:
        headers_extra["Referer"] = referer

    for intento in range(1, max_reintentos + 1):
        try:
            response = client.get(url, headers=headers_extra)

            if response.status_code == 403:
                print(f"403 Forbidden en intento {intento}: {url}")
                time.sleep(3 * intento)
                continue

            if response.status_code == 429:
                print(f"429 Too Many Requests en intento {intento}: {url}")
                time.sleep(5 * intento)
                continue

            response.raise_for_status()
            return response.text

        except httpx.HTTPStatusError as e:
            print(f"Error HTTP intento {intento}: {e}")
            time.sleep(3 * intento)

        except httpx.RequestError as e:
            print(f"Error de conexión intento {intento}: {e}")
            time.sleep(3 * intento)

    raise Exception(f"No se pudo obtener la URL después de {max_reintentos} intentos: {url}")


def obtener_configuration_guide(soup):
    """
    Extrae el título de la guía.

    Ejemplo:
    <h1 id="fw-pagetitle">
        Cisco 4000 Series ISRs Software Configuration Guide
    </h1>
    """

    titulo = soup.find("h1", id="fw-pagetitle")

    if not titulo:
        return ""

    return limpiar_espacios(titulo.get_text(" ", strip=True))


def obtener_urls_chapters(soup, url_base):
    """
    Extrae las URLs de los capítulos desde:

    <ul id="bookToc">
        <li><a href="...">Basic Router Configuration</a></li>
    </ul>
    """

    book_toc = soup.find("ul", id="bookToc")

    if not book_toc:
        return []

    chapters = []

    for a in book_toc.find_all("a", href=True):
        nombre_chapter = limpiar_espacios(a.get_text(" ", strip=True))
        href = a["href"]

        url_chapter = urljoin(url_base, href)

        chapters.append({
            "chapter_name_toc": nombre_chapter,
            "url": url_chapter
        })

    return chapters


# ============================================================
# Extracción de encabezados y validación de tabla
# ============================================================

def extraer_encabezados(tabla):
    """
    Extrae encabezados de la primera fila de la tabla.
    """
    primera_fila = tabla.find("tr")

    if not primera_fila:
        return []

    return [
        limpiar_espacios(th.get_text(" ", strip=True))
        for th in primera_fila.find_all(["th", "td"])
    ]


def tabla_valida(headers):
    """
    Verifica si la tabla tiene el formato:
    vacío | Command or Action | Purpose
    """

    if len(headers) < 3:
        return False

    return (
        headers[1].strip().lower() == "command or action"
        and headers[2].strip().lower() == "purpose"
    )


def normalizar_fila(fila, total_columnas=3):
    """
    Garantiza que cada fila tenga exactamente el número esperado de columnas.
    """
    if len(fila) < total_columnas:
        fila += [""] * (total_columnas - len(fila))

    return fila[:total_columnas]


# ============================================================
# Chapter, section y summary steps
# ============================================================

def es_titulo_capitulo(tag):
    """
    Determina si un encabezado corresponde a un capítulo.
    """

    if not tag or tag.name != "h2":
        return False

    texto = limpiar_espacios(tag.get_text(" ", strip=True)).lower()
    clases = tag.get("class", [])

    return (
        "chapter" in texto
        or any("chapter" in clase.lower() for clase in clases)
    )


def es_summary_steps_titulo(tag):
    """
    Detecta el título SUMMARY STEPS.
    """

    if not tag:
        return False

    texto = limpiar_espacios(tag.get_text(" ", strip=True))

    return texto.lower() == "summary steps"


def es_titulo_ignorable(tag):
    """
    Omite encabezados que no deben usarse como section.
    """

    if not tag:
        return True

    texto = limpiar_espacios(tag.get_text(" ", strip=True)).lower()

    if not texto:
        return True

    titulos_ignorables = {
        "summary steps",
        "detailed steps",
        "before you begin"
    }

    if texto in titulos_ignorables:
        return True

    if es_summary_steps_titulo(tag):
        return True

    return False


def obtener_section_de_tabla(tabla):
    """
    Busca el título de sección más cercano antes de la tabla.

    Reglas:
    - Prioriza el encabezado más cercano: h4, h3 o h2.
    - Si el más cercano es h4, combina con el h3 anterior:
      h3 - h4
    - Omite h4 = Before you begin.
    - Omite SUMMARY STEPS y DETAILED STEPS.
    """

    heading = tabla.find_previous(["h2", "h3", "h4"])

    while heading:
        if es_titulo_capitulo(heading) or es_titulo_ignorable(heading):
            heading = heading.find_previous(["h2", "h3", "h4"])
            continue

        texto_heading = limpiar_espacios(heading.get_text(" ", strip=True))

        if heading.name == "h4":
            h3_anterior = heading.find_previous("h3")

            while h3_anterior and es_titulo_ignorable(h3_anterior):
                h3_anterior = h3_anterior.find_previous("h3")

            if h3_anterior:
                texto_h3 = limpiar_espacios(h3_anterior.get_text(" ", strip=True))
                return f"{texto_h3} - {texto_heading}"

            return texto_heading

        return texto_heading

    return ""


def obtener_chapter_de_tabla(tabla):
    """
    Busca el h2 de capítulo más cercano antes de la tabla
    y elimina la palabra Chapter.
    """

    h2_anterior = tabla.find_previous("h2")

    while h2_anterior:
        if es_titulo_capitulo(h2_anterior):
            chapter = limpiar_espacios(h2_anterior.get_text(" ", strip=True))

            chapter = re.sub(
                r"^\s*chapter\s*:?\s*",
                "",
                chapter,
                flags=re.IGNORECASE
            )

            return chapter.strip()

        h2_anterior = h2_anterior.find_previous("h2")

    return ""


def limpiar_summary_step_item(li):
    """
    Limpia cada item de la lista SUMMARY STEPS.
    """

    texto = li.get_text(" ", strip=True)
    return limpiar_espacios(texto)


def obtener_summary_steps_de_tabla(tabla):
    """
    Busca el bloque SUMMARY STEPS más cercano antes de la tabla.

    Flujo esperado:
    h2 section
    h3 SUMMARY STEPS
    ol.summary_steps
    table
    """

    h3_summary = tabla.find_previous(
        lambda tag: tag.name in ["h3", "h4"] and es_summary_steps_titulo(tag)
    )

    if not h3_summary:
        return ""

    # Validar que este SUMMARY STEPS pertenece a la misma sección
    h2_de_summary = h3_summary.find_previous("h2")
    h2_de_tabla = tabla.find_previous("h2")

    if h2_de_summary != h2_de_tabla:
        return ""

    lista = h3_summary.find_next(
        "ol", class_=lambda c: c and "summary_steps" in c
    )

    if not lista:
        return ""

    # Validar que la lista aparece antes de la tabla
    siguiente_tabla = lista.find_next("table")

    if siguiente_tabla != tabla:
        return ""

    pasos = []

    for idx, li in enumerate(lista.find_all("li", recursive=False), start=1):
        paso = limpiar_summary_step_item(li)

        if paso:
            pasos.append(f"{idx}. {paso}")

    return "\n".join(pasos)


# ============================================================
# Extracción robusta de celdas Step / Command / Purpose
# ============================================================

def clase_contiene(tag, nombre_clase):
    """
    Verifica si una celda tiene una clase específica.
    """

    clases = tag.get("class", [])

    return any(nombre_clase in clase for clase in clases)


def extraer_celdas_step(fila):
    """
    Extrae Step, Command or Action y Purpose de una fila.

    Prioriza clases HTML:
    - step--command
    - step--purpose

    Si no existen, usa posición normal:
    columna 0, columna 1, columna 2.
    """

    celdas = fila.find_all(["td", "th"])

    if not celdas:
        return None

    celda_command = fila.find(
        lambda tag: tag.name in ["td", "th"] and clase_contiene(tag, "step--command")
    )

    celda_purpose = fila.find(
        lambda tag: tag.name in ["td", "th"] and clase_contiene(tag, "step--purpose")
    )

    celda_step = None

    for celda in celdas:
        if celda != celda_command and celda != celda_purpose:
            celda_step = celda
            break

    # Fallback por posición si no hay clases
    if not celda_step and len(celdas) >= 1:
        celda_step = celdas[0]

    if not celda_command and len(celdas) >= 2:
        celda_command = celdas[1]

    if not celda_purpose and len(celdas) >= 3:
        celda_purpose = celdas[2]

    if not celda_step or not celda_command or not celda_purpose:
        return None

    return [
        limpiar_step(celda_step),
        limpiar_command_or_action(celda_command),
        limpiar_purpose(celda_purpose)
    ]


# ============================================================
# Extracción de tablas desde un HTML de chapter
# ============================================================

def extraer_filas_de_html_chapter(html_chapter, configuration_guide):
    """
    Aplica la lógica de extracción de tablas sobre el HTML de un chapter.
    Devuelve:
    - filas listas para escribir en CSV
    - total de tablas válidas extraídas
    """

    soup = BeautifulSoup(html_chapter, "html.parser")
    tablas = soup.find_all("table")

    filas_extraidas = []
    total_tablas_extraidas = 0

    for tabla in tablas:
        headers = extraer_encabezados(tabla)

        if not tabla_valida(headers):
            continue

        total_tablas_extraidas += 1

        chapter = obtener_chapter_de_tabla(tabla)
        section = obtener_section_de_tabla(tabla)
        summary_steps = obtener_summary_steps_de_tabla(tabla)

        filas = tabla.find_all("tr")[1:]

        for fila in filas:
            fila_csv = extraer_celdas_step(fila)

            if not fila_csv:
                continue

            fila_csv = normalizar_fila(fila_csv, 3)

            filas_extraidas.append([
                configuration_guide,
                chapter,
                section,
                summary_steps
            ] + fila_csv)

    return filas_extraidas, total_tablas_extraidas

# ============================================================
# Automatización completa desde URL base
# ============================================================

def extraer_configuration_guide_completa(urls_base, ruta_salida_csv):
    """
    Flujo completo:

    1. Descarga cada URL base de Configuration Guide.
    2. Obtiene el título h1#fw-pagetitle.
    3. Obtiene los chapters desde ul#bookToc.
    4. Descarga cada chapter.
    5. Aplica la extracción de tablas.
    6. Guarda un CSV único con los datos de todas las URLs.
    """

    if isinstance(urls_base, str):
        urls_base = [urls_base]

    total_filas_global = 0
    total_tablas_extraidas_global = 0
    total_chapters_con_tablas_global = 0

    with open(ruta_salida_csv, "w", encoding="utf-8-sig", newline="") as outfile:
        writer = csv.writer(
            outfile,
            delimiter=",",
            quotechar='"',
            quoting=csv.QUOTE_ALL
        )

        writer.writerow([
            "configuration guide",
            "chapter",
            "section",
            "summary steps",
            "Steps",
            "Command or Action",
            "Purpose"
        ])

        for guia_idx, url_base in enumerate(urls_base, start=1):
            headers_http = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9,es;q=0.8",
                "Accept-Encoding": "gzip, deflate, br",
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
                "Upgrade-Insecure-Requests": "1",
                "Referer": url_base,
            }

            print("=" * 80)
            print(f"Procesando URL base [{guia_idx}/{len(urls_base)}]:")
            print(url_base)
            print("=" * 80)

            total_filas = 0
            total_tablas_extraidas = 0
            total_chapters_con_tablas = 0

            with httpx.Client(
                headers=headers_http,
                timeout=60.0,
                follow_redirects=True,
                http2=True
            ) as client:

                print("Obteniendo URL base:")
                print(url_base)

                try:
                    html_base = obtener_html(url_base, client)
                except Exception as e:
                    print(f"Error obteniendo URL base: {url_base}")
                    print(f"Detalle: {e}")
                    continue

                soup_base = BeautifulSoup(html_base, "html.parser")

                configuration_guide = obtener_configuration_guide(soup_base)

                if not configuration_guide:
                    print("No se pudo obtener el título de la Configuration Guide.")
                    continue

                print(f"Configuration Guide detectada: {configuration_guide}")

                chapters = obtener_urls_chapters(soup_base, url_base)

                if not chapters:
                    print("No se encontraron chapters dentro de ul#bookToc.")
                    continue

                print(f"Chapters encontrados: {len(chapters)}")

                for idx, chapter_info in enumerate(chapters, start=1):
                    nombre_chapter_toc = chapter_info["chapter_name_toc"]
                    url_chapter = chapter_info["url"]

                    print(f"\n[{idx}/{len(chapters)}] Procesando chapter:")
                    print(nombre_chapter_toc)
                    print(url_chapter)

                    try:
                        html_chapter = obtener_html(
                            url=url_chapter,
                            client=client,
                            referer=url_base
                        )

                        filas, tablas_extraidas_chapter = extraer_filas_de_html_chapter(
                            html_chapter=html_chapter,
                            configuration_guide=configuration_guide
                        )

                        if filas:
                            total_chapters_con_tablas += 1

                        total_tablas_extraidas += tablas_extraidas_chapter

                        for fila in filas:
                            writer.writerow(fila)

                        total_filas += len(filas)

                        print(f"Tablas extraídas en este chapter: {tablas_extraidas_chapter}")
                        print(f"Filas extraídas en este chapter: {len(filas)}")

                        # Pausa corta para no hacer requests agresivos
                        time.sleep(2)

                    except Exception as e:
                        print(f"Error procesando chapter: {nombre_chapter_toc}")
                        print(f"Detalle: {e}")

            total_filas_global += total_filas
            total_tablas_extraidas_global += total_tablas_extraidas
            total_chapters_con_tablas_global += total_chapters_con_tablas

            print("\nResumen de esta URL base:")
            print(f"URL base: {url_base}")
            print(f"Chapters con tablas válidas: {total_chapters_con_tablas}")
            print(f"Total de tablas extraídas: {total_tablas_extraidas}")
            print(f"Total de filas extraídas: {total_filas}")
            print("\n")

    print("Proceso finalizado.")
    print(f"CSV generado en: {ruta_salida_csv}")
    print(f"Chapters con tablas válidas total: {total_chapters_con_tablas_global}")
    print(f"Total de tablas extraídas: {total_tablas_extraidas_global}")
    print(f"Total de filas extraídas: {total_filas_global}")


# ============================================================
# Ejecución
# ============================================================

if __name__ == "__main__":
    urls_base = [
        "https://www.cisco.com/c/en/us/td/docs/switches/lan/catalyst9300/software/release/17-12/configuration_guide/int_hw/b_1712_int_and_hw_9300_cg.html",
        "https://www.cisco.com/c/en/us/td/docs/switches/lan/catalyst9300/software/release/17-12/configuration_guide/vlan/b_1712_vlan_9300_cg.html",
        "https://www.cisco.com/c/en/us/td/docs/switches/lan/catalyst9300/software/release/17-12/configuration_guide/lyr2/b_1712_lyr2_9300_cg.html",
        "https://www.cisco.com/c/en/us/td/docs/switches/lan/catalyst9300/software/release/17-12/configuration_guide/rtng/b_1712_rtng_9300_cg.html",
        "https://www.cisco.com/c/en/us/td/docs/switches/lan/catalyst9300/software/release/17-12/configuration_guide/ip/b_1712_ip_9300_cg.html",
        "https://www.cisco.com/c/en/us/td/docs/switches/lan/catalyst9300/software/release/17-12/configuration_guide/sec/b_1712_sec_9300_cg.html",
        "https://www.cisco.com/c/en/us/td/docs/switches/lan/catalyst9300/software/release/17-12/configuration_guide/sys_mgmt/b_1712_sys_mgmt_9300_cg.html",
        "https://www.cisco.com/c/en/us/td/docs/switches/lan/catalyst9300/software/release/17-12/configuration_guide/nmgmt/b_1712_nmgmt_9300_cg.html",
        "https://www.cisco.com/c/en/us/td/docs/switches/lan/catalyst9300/software/release/17-12/configuration_guide/qos/b_1712_qos_9300_cg.html",
        "https://www.cisco.com/c/en/us/td/docs/switches/lan/catalyst9300/software/release/17-12/configuration_guide/stck_mgr_ha/b_1712_stck_mgr_ha_9300_cg.html",
    ]

    nombre_archivo_salida = "cisco_catalyst_9300_ios_xe_dublin_17_12_x.csv"
    nombre_reporte = "reporte.txt"

    ruta_salida_csv = os.path.join(os.getcwd(), nombre_archivo_salida)
    ruta_reporte_txt = os.path.join(os.getcwd(), nombre_reporte)

    with open(ruta_reporte_txt, "w", encoding="utf-8") as reporte:
        with redirect_stdout(reporte):
            extraer_configuration_guide_completa(
                urls_base=urls_base,
                ruta_salida_csv=ruta_salida_csv
            )
