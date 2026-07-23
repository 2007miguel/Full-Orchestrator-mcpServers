import json
import sys
from pathlib import Path


def normalizar_json_texto(text):
    salida = []
    in_string = False
    escaped = False

    for ch in text:
        if in_string:
            if escaped:
                salida.append(ch)
                escaped = False
            elif ch == "\\":
                salida.append(ch)
                escaped = True
            elif ch == '"':
                salida.append(ch)
                in_string = False
            elif ch == "\n":
                salida.append("\\n")
            elif ch == "\r":
                salida.append("\\r")
            elif ch == "\t":
                salida.append("\\t")
            elif ch == "\b":
                salida.append("\\b")
            elif ch == "\f":
                salida.append("\\f")
            elif ch == "\u2028":
                salida.append("\\u2028")
            elif ch == "\u2029":
                salida.append("\\u2029")
            elif ord(ch) < 32:
                salida.append(f"\\u{ord(ch):04x}")
            else:
                salida.append(ch)
            continue

        if ch == '"':
            salida.append(ch)
            in_string = True
        elif ch in ("\u2028", "\u2029"):
            salida.append("\n")
        elif ord(ch) < 32 and ch not in ("\n", "\r", "\t"):
            salida.append(" ")
        else:
            salida.append(ch)

    return "".join(salida)


def iter_chunks(path):
    text = normalizar_json_texto(path.read_text(encoding="utf-8-sig")).lstrip()
    if not text:
        return

    if text.startswith("["):
        try:
            items = json.loads(text)
        except json.JSONDecodeError as e:
            raise ValueError(f"Error leyendo {path}: {e}") from e
        for item in items:
            yield item
        return

    decoder = json.JSONDecoder()
    idx = 0
    length = len(text)

    while idx < length:
        while idx < length and text[idx].isspace():
            idx += 1
        if idx >= length:
            break

        try:
            chunk, end = decoder.raw_decode(text, idx)
        except json.JSONDecodeError as e:
            raise ValueError(f"Error leyendo {path}: {e}") from e
        yield chunk
        idx = end


def unir_json(carpeta, salida):
    carpeta = Path(carpeta)
    salida = Path(salida)

    archivos = list(carpeta.glob("*.json")) + list(carpeta.glob("*.jsonl"))
    total_chunks = 0

    with salida.open("w", encoding="utf-8") as out:
        for archivo in archivos:
            for chunk in iter_chunks(archivo):
                out.write(json.dumps(chunk, ensure_ascii=False) + "\n")
                total_chunks += 1

    print(f"Archivos unidos : {len(archivos)}")
    print(f"Chunks totales  : {total_chunks}")
    print(f"Salida          : {salida}")


def main():
    carpeta = sys.argv[1] if len(sys.argv) > 1 else "unir_datset_competo_130k+ios15"
    salida = sys.argv[2] if len(sys.argv) > 2 else "RAG_base_completo.jsonl"
    unir_json(carpeta, salida)


if __name__ == "__main__":
    main()
