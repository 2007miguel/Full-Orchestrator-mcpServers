import json
import os

def limpiar_y_contar_jsonl(archivo_entrada, archivo_salida):
    """
    Filtra un archivo JSONL para mantener solo los campos especificados y
    cuenta las entradas por tipo de dispositivo.

    Args:
        archivo_entrada (str): La ruta al archivo JSONL de entrada.
        archivo_salida (str): La ruta donde se guardará el nuevo archivo JSONL filtrado.
    """
    # Lista de campos que se deben conservar en el nuevo JSONL
    campos_a_mantener = [
        "section_id",
        "device_type",
        "product",
        "os",
        "version",
        "configuration_guide",
        "chapter",
        "section",
        "commands",
        "examples"
    ]

    # Diccionario para contar las líneas por tipo de dispositivo
    conteo_por_dispositivo = {
        "router": 0,
        "switch": 0,
        "unknown": 0
    }
    
    lineas_procesadas = 0

    try:
        with open(archivo_entrada, 'r', encoding='utf-8') as f_in, \
             open(archivo_salida, 'w', encoding='utf-8') as f_out:

            for i, linea in enumerate(f_in, 1):
                try:
                    data = json.loads(linea)
                    
                    # Crear un nuevo diccionario solo con los campos deseados
                    linea_limpia = {campo: data.get(campo) for campo in campos_a_mantener}
                    
                    # Escribir el nuevo objeto JSON en el archivo de salida
                    json.dump(linea_limpia, f_out, ensure_ascii=False)
                    f_out.write('\n')
                    
                    # Contar por tipo de dispositivo
                    device_type = data.get("device_type")
                    if device_type in conteo_por_dispositivo:
                        conteo_por_dispositivo[device_type] += 1
                    else:
                        conteo_por_dispositivo["unknown"] += 1
                        
                    lineas_procesadas += 1

                except json.JSONDecodeError:
                    print(f"Advertencia: Se encontró una línea mal formada en la línea {i} y fue omitida: {linea.strip()}")
                except KeyError as e:
                    print(f"Advertencia: Faltó la clave {e} en la línea {i}. Se omitió la línea.")

            print("¡Limpieza completada!")
            print("-" * 30)
            print("Resumen del procesamiento:")
            print(f"Total de líneas procesadas: {lineas_procesadas}")
            print("\nConteo por tipo de dispositivo:")
            for dispositivo, cantidad in conteo_por_dispositivo.items():
                if cantidad > 0:
                    print(f"- '{dispositivo}': {cantidad} entradas.")
            print("-" * 30)
            print(f"Archivo limpio guardado en: {archivo_salida}")

    except FileNotFoundError:
        print(f"Error: El archivo de entrada '{archivo_entrada}' no fue encontrado.")
    except Exception as e:
        print(f"Ocurrió un error inesperado: {e}")

# --- Configuración del Script ---

# 1. Especifica los nombres de los archivos de entrada y salida.
#    Asegúrate de que las rutas sean correctas.
directorio_base = "c:\\Users\\juan\\Documents\\Miguel\\tesis\\System\\Orchestrator-mcpServers\\RAG\\datos_resultado_de_codMultireporte"
archivo_origen = os.path.join(directorio_base, 'rag_chunks_sections_filtrado_criterios.jsonl')
archivo_destino = os.path.join(directorio_base, 'rag_chunks_sections_filtrado_criterios_v2.jsonl')

# 2. Ejecutar la función de limpieza y conteo
limpiar_y_contar_jsonl(archivo_origen, archivo_destino)
