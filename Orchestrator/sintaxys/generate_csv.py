import json
import csv

# Nombre del archivo JSON de entrada
json_file_path = 'cuantizacion8.json'
# Nombre del archivo CSV de salida
csv_file_path = 'cuantizacion8.csv'

try:
    # Abrir y cargar el archivo JSON
    with open(json_file_path, 'r', encoding='utf-8') as f:
        data = json.load(f)

    # Abrir el archivo CSV para escritura
    with open(csv_file_path, 'w', newline='', encoding='utf-8') as csvfile:
        # Definir los nombres de las columnas
        fieldnames = ['model_name', 'predictions']
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)

        # Escribir la cabecera
        writer.writeheader()

        # Iterar sobre cada resultado en la lista "results" del JSON
        for result in data.get('results', []):
            model_name = result.get('model_name')
            
            # Iterar sobre cada predicción en la lista "predictions"
            if model_name and 'predictions' in result:
                for prediction in result['predictions']:
                    # Escribir una fila por cada predicción
                    writer.writerow({
                        'model_name': model_name,
                        'predictions': prediction
                    })

    print(f"Archivo '{csv_file_path}' generado exitosamente.")

except FileNotFoundError:
    print(f"Error: El archivo '{json_file_path}' no fue encontrado.")
except json.JSONDecodeError:
    print(f"Error: El archivo '{json_file_path}' no es un JSON válido.")
except Exception as e:
    print(f"Ocurrió un error inesperado: {e}")
