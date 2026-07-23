import sys


def limpiar_duplicados(ruta_entrada, ruta_salida):
    vistos = set()
    total = 0
    conservados = 0
    duplicados = 0

    with open(ruta_entrada, "r", encoding="utf-8") as entrada, \
            open(ruta_salida, "w", encoding="utf-8") as salida:
        for linea in entrada:
            if not linea.strip():
                continue

            total += 1
            clave = linea.rstrip("\r\n")
            if clave in vistos:
                duplicados += 1
                continue

            vistos.add(clave)
            salida.write(clave + "\n")
            conservados += 1

    print(f"Chunks originales  : {total}")
    print(f"Duplicados         : {duplicados}")
    print(f"Chunks conservados : {conservados}")
    print(f"Salida             : {ruta_salida}")


def main():
    if len(sys.argv) != 3:
        print(
            "Uso: python limpiar_duplicados_jsonl.py <ENTRADA_JSONL> <SALIDA_JSONL>",
            file=sys.stderr,
        )
        sys.exit(1)

    limpiar_duplicados(sys.argv[1], sys.argv[2])


if __name__ == "__main__":
    main()
