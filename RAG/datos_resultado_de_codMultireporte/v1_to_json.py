import pandas as pd
import json
import re
import hashlib
import unicodedata
from pathlib import Path


# =========================
# Configuración base
# =========================

INPUT_CSV = " isr_4000_ios_xe_17-x.csv"
OUTPUT_JSON = " isr_4000_ios_xe_17-x.json"

DEVICE_SPECIFICATION = {
    "device_type": "router",          # router | switch
    "product": "ISR 4000",            # ISR 4000 | Catalyst 9300
    "os": "IOS XE",
    "version": "17.x"
}


# =========================
# Utilidades
# =========================

def clean_text(value):
    """Limpia texto conservando saltos útiles."""
    if pd.isna(value):
        return ""

    text = str(value)
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    lines = []
    for line in text.split("\n"):
        line = line.strip()
        if line:
            lines.append(line)

    return "\n".join(lines).strip()


def normalize_command(value):
    """
    Convierte comandos partidos en varias líneas en una sola línea.

    Ejemplo:
    configure
    terminal

    Resultado:
    configure terminal
    """
    text = clean_text(value)
    return " ".join(text.split()).strip()


def parse_command_and_example(command_or_action):
    """
    Separa el campo 'Command or Action' en:
    - command
    - example

    Entrada típica:
    enable
    Example:
    Device> enable
    """

    text = clean_text(command_or_action)

    if not text:
        return "", ""

    parts = re.split(r"(?i)\bExample:\s*", text, maxsplit=1)

    command_raw = parts[0].strip()
    example_raw = parts[1].strip() if len(parts) > 1 else ""

    command = normalize_command(command_raw)
    example = clean_text(example_raw)

    return command, example


def slugify(value, max_len=60):
    """Crea identificadores seguros."""
    value = str(value)
    value = unicodedata.normalize("NFKD", value)
    value = value.encode("ascii", "ignore").decode("ascii")
    value = value.lower()
    value = re.sub(r"[^a-z0-9]+", "_", value)
    value = re.sub(r"_+", "_", value).strip("_")
    return value[:max_len]


def make_id(*parts, prefix="SEC"):
    """Crea un ID estable."""
    raw = "||".join(str(p) for p in parts)
    digest = hashlib.md5(raw.encode("utf-8")).hexdigest()[:8]
    readable = slugify(parts[-1], max_len=45)
    return f"{prefix}_{readable}_{digest}"


def build_numbered_commands(commands):
    """Devuelve comandos enumerados uno debajo del otro."""
    return "\n".join(
        f"{i}. {cmd}"
        for i, cmd in enumerate(commands, start=1)
        if cmd
    )


def build_plain_examples(examples):
    """Devuelve ejemplos uno debajo del otro, sin numerar."""
    return "\n".join(
        example
        for example in examples
        if example
    )


# =========================
# Construcción del JSON
# =========================

def build_hierarchical_json(input_csv, device_specification):
    df = pd.read_csv(input_csv, encoding="utf-8-sig", keep_default_na=False)

    df.columns = [c.strip() for c in df.columns]

    required_columns = [
        "configuration_guide",
        "chapter",
        "section",
        "Command or Action"
    ]

    for col in required_columns:
        if col not in df.columns:
            raise ValueError(f"Falta la columna requerida: {col}")

    root = {
        "device": {
            "device_specification": {
                **device_specification,
                "configuration_guides": []
            }
        }
    }

    guide_map = {}
    chapter_map = {}
    section_map = {}

    for _, row in df.iterrows():
        guide_name = clean_text(row["configuration_guide"])
        chapter_name = clean_text(row["chapter"])
        section_name = clean_text(row["section"])
        command_or_action = row["Command or Action"]

        if not guide_name or not chapter_name or not section_name:
            continue

        command, example = parse_command_and_example(command_or_action)

        if not command and not example:
            continue

        # =========================
        # Nivel guide
        # =========================

        if guide_name not in guide_map:

            guide_obj = {
                "configuration_guide": guide_name,
                "chapters": []
            }

            guide_map[guide_name] = guide_obj
            root["device"]["device_specification"]["configuration_guides"].append(guide_obj)

        guide_obj = guide_map[guide_name]

        # =========================
        # Nivel chapter
        # =========================

        chapter_key = (guide_name, chapter_name)

        if chapter_key not in chapter_map:

            chapter_obj = {
                "chapter": chapter_name,
                "sections": []
            }

            chapter_map[chapter_key] = chapter_obj
            guide_obj["chapters"].append(chapter_obj)

        chapter_obj = chapter_map[chapter_key]

        # =========================
        # Nivel section
        # =========================

        section_key = (guide_name, chapter_name, section_name)

        if section_key not in section_map:
            section_id = make_id(
                device_specification.get("product", ""),
                guide_name,
                chapter_name,
                section_name,
                prefix="SECTION"
            )

            section_obj = {
                "section_id": section_id,
                "section": section_name,

                # Listas internas temporales
                "_commands_list": [],
                "_examples_list": []
            }

            section_map[section_key] = section_obj
            chapter_obj["sections"].append(section_obj)

        section_obj = section_map[section_key]

        if command:
            section_obj["_commands_list"].append(command)

        if example:
            section_obj["_examples_list"].append(example)

    # =========================
    # Convertir listas internas a texto final
    # =========================

    for guide in root["device"]["device_specification"]["configuration_guides"]:
        for chapter in guide["chapters"]:
            for section in chapter["sections"]:
                commands_list = section.pop("_commands_list", [])
                examples_list = section.pop("_examples_list", [])

                section["commands"] = build_numbered_commands(commands_list)
                section["examples"] = build_plain_examples(examples_list)

    total_sections = len(section_map)

    return root, total_sections


# =========================
# Ejecutar
# =========================

if __name__ == "__main__":
    data, section_count = build_hierarchical_json(INPUT_CSV, DEVICE_SPECIFICATION)

    output_path = Path(OUTPUT_JSON)

    with output_path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"Total de secciones procesadas: {section_count}")
    print(f"JSON generado correctamente: {output_path}")