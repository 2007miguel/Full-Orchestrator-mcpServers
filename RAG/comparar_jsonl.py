import json

ARCHIVO_1 = "rag_chunks_sections_filtrado_criterios_v2.jsonl"
ARCHIVO_2 = "RAG_base_completo_130k.jsonl"
SALIDA = "lineas_no_coincidentes.jsonl"
CAMPOS = ("configuration_guide", "chapter", "section")


def clave(fila):
    return tuple(fila.get(campo) for campo in CAMPOS)


with open(ARCHIVO_2, "r", encoding="utf-8") as archivo:
    claves_archivo_2 = {clave(json.loads(linea)) for linea in archivo if linea.strip()}

with open(ARCHIVO_1, "r", encoding="utf-8") as archivo:
    no_coincidentes = [
        linea
        for linea in archivo
        if linea.strip()
        for fila in [json.loads(linea)]
        if clave(fila) not in claves_archivo_2
    ]

with open(SALIDA, "w", encoding="utf-8") as archivo:
    archivo.writelines(no_coincidentes)

print(f"Filas no coincidentes: {len(no_coincidentes)}")
print(f"Resultado guardado en: {SALIDA}")
