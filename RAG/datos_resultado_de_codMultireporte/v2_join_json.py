import json
from pathlib import Path


ROUTER_JSON = " isr_4000_ios_xe_17-x.json"
SWITCH_JSON = "cisco_catalyst_9300_ios_xe_dublin_17_12_x.json"
OUTPUT_JSON = "cisco_documentation_corpus.json"


def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def extract_device(json_data):
    """
    Espera una estructura tipo:
    {
      "device": {
        "device_specification": {...}
      }
    }
    """
    if "device" in json_data:
        return json_data["device"]

    if "device_specification" in json_data:
        return {
            "device_specification": json_data["device_specification"]
        }

    raise ValueError("El JSON no tiene una estructura válida de device.")


def main():
    router_data = load_json(ROUTER_JSON)
    switch_data = load_json(SWITCH_JSON)

    router_device = extract_device(router_data)
    switch_device = extract_device(switch_data)

    corpus = {
        "documentation_corpus": {
            "vendor": "Cisco",
            "devices": [
                router_device,
                switch_device
            ]
        }
    }

    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(corpus, f, ensure_ascii=False, indent=2)

    print(f"JSON unido correctamente: {OUTPUT_JSON}")


if __name__ == "__main__":
    main()