#Aplicado sobre los datos unidos v3 
import json


INPUT_JSON = "cisco_documentation_corpus.json"
OUTPUT_JSONL = "rag_chunks_sections.jsonl"


def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def write_jsonl(rows, path):
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def flatten_sections(data):
    rows = []

    devices = data["documentation_corpus"]["devices"]

    for device in devices:
        spec = device["device_specification"]

        device_type = spec.get("device_type", "")
        product = spec.get("product", "")
        os_name = spec.get("os", "")
        version = spec.get("version", "")

        for guide in spec.get("configuration_guides", []):
            configuration_guide = guide.get("configuration_guide", "")

            for chapter in guide.get("chapters", []):
                chapter_name = chapter.get("chapter", "")

                for section in chapter.get("sections", []):
                    row = {
                        "section_id": section.get("section_id", ""),
                        "device_type": device_type,
                        "product": product,
                        "os": os_name,
                        "version": version,
                        "configuration_guide": configuration_guide,
                        "chapter": chapter_name,
                        "section": section.get("section", ""),
                        "commands": section.get("commands", ""),
                        "examples": section.get("examples", "")
                    }

                    rows.append(row)

    return rows


if __name__ == "__main__":
    data = load_json(INPUT_JSON)
    chunks = flatten_sections(data)
    write_jsonl(chunks, OUTPUT_JSONL)
    
    total_sections = len(chunks)

    print(f"JSONL generado: {OUTPUT_JSONL}")
    print(f"Total de secciones procesadas: {total_sections}")