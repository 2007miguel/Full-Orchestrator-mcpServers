#informacion en fotmato json de la estructura de la guia de configuracion, con los campos configuration_guide, chapter y section. 
#Se omiten filas incompletas y se limpian valores vacíos o con espacios extra. El resultado se guarda en un archivo JSON con una estructura anidada que refleja la jerarquía de la guía de configuración. Además, se verifica que el CSV contenga las columnas requeridas antes de procesar los datos.
import csv
import json
from pathlib import Path


INPUT_CSV = " isr_4000_ios_xe_17-x.csv"
OUTPUT_JSON = " isr_4000_ios_xe_17-x.json"


def clean(value):
    """Limpia valores vacíos o espacios extra."""
    if value is None:
        return ""
    return str(value).strip()


def build_nested_json(input_csv):
    data = {
        "configuration_guide": {}
    }

    with open(input_csv, mode="r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)

        required_columns = {"configuration_guide", "chapter", "section"}
        csv_columns = set(reader.fieldnames or [])

        missing = required_columns - csv_columns
        if missing:
            raise ValueError(
                f"Faltan columnas requeridas en el CSV: {missing}. "
                f"Columnas encontradas: {reader.fieldnames}"
            )

        for row in reader:
            guide = clean(row.get("configuration_guide"))
            chapter = clean(row.get("chapter"))
            section = clean(row.get("section"))

            # Omitir filas incompletas
            if not guide or not chapter or not section:
                continue

            # Crear guide si no existe
            if guide not in data["configuration_guide"]:
                data["configuration_guide"][guide] = {
                    "chapter": {}
                }

            # Crear chapter si no existe
            if chapter not in data["configuration_guide"][guide]["chapter"]:
                data["configuration_guide"][guide]["chapter"][chapter] = {
                    "section": {}
                }

            # Crear section si no existe
            if section not in data["configuration_guide"][guide]["chapter"][chapter]["section"]:
                data["configuration_guide"][guide]["chapter"][chapter]["section"][section] = {}

    return data


def main():
    nested_json = build_nested_json(INPUT_CSV)

    with open(OUTPUT_JSON, mode="w", encoding="utf-8") as f:
        json.dump(nested_json, f, ensure_ascii=False, indent=2)

    print(f"JSON generado correctamente: {OUTPUT_JSON}")


if __name__ == "__main__":
    main()