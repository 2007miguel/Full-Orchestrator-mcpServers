import json

def limpiar_jsonl_por_guia(archivo_entrada, archivo_salida, guias_a_eliminar):
    """
    Filtra un archivo JSONL para eliminar las líneas que coincidan con una lista
    de guías de configuración.

    Args:
        archivo_entrada (str): La ruta al archivo JSONL de entrada.
        archivo_salida (str): La ruta donde se guardará el nuevo archivo JSONL filtrado.
        guias_a_eliminar (list): Una lista de strings con los nombres de las
                                 'configuration_guide' a eliminar.
    """
    try:
        # Diccionario para contar las líneas eliminadas por cada guía
        eliminadas_por_guia = {guia: 0 for guia in guias_a_eliminar}

        with open(archivo_entrada, 'r', encoding='utf-8') as f_in, \
             open(archivo_salida, 'w', encoding='utf-8') as f_out:
            
            lineas_eliminadas_total = 0
            lineas_conservadas = 0

            for i, linea in enumerate(f_in, 1):
                try:
                    # Cargar cada línea como un objeto JSON
                    data = json.loads(linea)
                    guia_actual = data.get("configuration_guide")
                    
                    # Verificar si la 'configuration_guide' está en la lista de eliminación
                    if guia_actual in guias_a_eliminar:
                        # Si está, se incrementa el contador para esa guía específica
                        eliminadas_por_guia[guia_actual] += 1
                        lineas_eliminadas_total += 1
                    else:
                        # Si no está, se escribe en el archivo de salida
                        json.dump(data, f_out)
                        f_out.write('\n')
                        lineas_conservadas += 1
                except json.JSONDecodeError:
                    print(f"Advertencia: Se encontró una línea mal formada en la línea {i} y fue omitida: {linea.strip()}")

            print("¡Limpieza completada!")
            print("-" * 30)
            print("Resumen de líneas eliminadas por guía:")
            for guia, cantidad in eliminadas_por_guia.items():
                print(f"- '{guia}': {cantidad} líneas eliminadas.")
            
            print("-" * 30)
            print(f"Total de líneas conservadas: {lineas_conservadas}")
            print(f"Total de líneas eliminadas: {lineas_eliminadas_total}")

    except FileNotFoundError:
        print(f"Error: El archivo de entrada '{archivo_entrada}' no fue encontrado.")
    except Exception as e:
        print(f"Ocurrió un error inesperado: {e}")

# --- Configuración del Script ---

# 1. Define la lista de 'configuration_guide' que quieres eliminar.
#    Agrega aquí todos los valores que necesites.
guias_para_borrar = [
    "Network Management Configuration Guide, Cisco IOS XE Dublin 17.12.x (Catalyst 9300 Switches)",
    "Quality of Service Configuration Guide, Cisco IOS XE Dublin 17.12.x (Catalyst 9300 Switches)",
    "Stacking and High Availability Configuration Guide, Cisco IOS XE Dublin 17.12.x (Catalyst 9300 Switches)",
    "SNMP Configuration Guide, Cisco IOS XE 17",
    "Quality of Service Configuration Guide, Cisco IOS XE 17.x",
    "MPLS: Layer 3 VPNs Configuration Guide, Cisco IOS XE 17"
]

# 2. Especifica los nombres de los archivos de entrada y salida.
archivo_origen = 'c:\\Users\\juan\\Documents\\Miguel\\tesis\\System\\Orchestrator-mcpServers\\RAG\\datos_resultado_de_codMultireporte\\rag_chunks_sections_v1.jsonl'
archivo_destino = 'c:\\Users\\juan\\Documents\\Miguel\\tesis\\System\\Orchestrator-mcpServers\\RAG\\datos_resultado_de_codMultireporte\\rag_chunks_sections_v2.jsonl'

# 3. Ejecutar la función de limpieza
limpiar_jsonl_por_guia(archivo_origen, archivo_destino, guias_para_borrar)
