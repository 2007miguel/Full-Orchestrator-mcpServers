import json
import re
from pathlib import Path

archivo = Path("rag_chunks_sections_filtrado_criterios_v2.jsonl")
temporal = archivo.with_suffix(".tmp")

with archivo.open("r", encoding="utf-8", newline="") as entrada, \
     temporal.open("w", encoding="utf-8", newline="") as salida:

    for linea in entrada:
        datos = json.loads(linea)
        tipo = datos.get("device_type", "").lower()

        linea = re.sub(
            r'"os":\s*"[^"]*"',
            '"os": "Cisco IOS XE"',
            linea,
            count=1,
        )

        if tipo == "router":
            producto = "4000 Series Integrated Services Routers"
        elif tipo == "switch":
            producto = "Catalyst 9300 Series Switches"
        else:
            producto = None

        if producto:
            linea = re.sub(
                r'"product":\s*"[^"]*"',
                f'"product": "{producto}"',
                linea,
                count=1,
            )

        version = re.search(r"\d+(?:\.(?:\d+|x))+", datos.get("version", ""))
        if version:
            linea = re.sub(
                r'"version":\s*"[^"]*"',
                f'"version": "{version.group()}"',
                linea,
                count=1,
            )

        linea = re.sub(
            r'"commands":',
            '"block_type": "step_table", "commands":',
            linea,
            count=1,
        )

        linea = re.sub(
            r'("commands":\s*"(?:\\.|[^"\\])*")',
            r'\1, "purpose": ""',
            linea,
            count=1,
        )

        salida.write(linea)

temporal.replace(archivo)
