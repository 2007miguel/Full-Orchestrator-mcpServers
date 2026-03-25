from fastapi import FastAPI
from pydantic import BaseModel
import os

def cargar_respuestas_desde_archivos():
    """
    Carga las respuestas desde los archivos .txt en el directorio 'responses'.
    Cada archivo se convierte en un único string multilínea.
    """
    respuestas = []
    # Obtiene la ruta absoluta del directorio del script actual
    dir_actual = os.path.dirname(os.path.abspath(__file__))
    ruta_respuestas = os.path.join(dir_actual, 'responses')

    if os.path.isdir(ruta_respuestas):
        # Itera sobre los archivos ordenados para mantener consistencia
        for nombre_archivo in sorted(os.listdir(ruta_respuestas)):
            if nombre_archivo.endswith(".txt"):
                with open(os.path.join(ruta_respuestas, nombre_archivo), 'r', encoding='utf-8') as f:
                    # Une todas las líneas en un solo string con saltos de línea
                    respuestas.append("\\n".join(line.strip() for line in f if line.strip()))
    return respuestas

# Inicializa la aplicación FastAPI
app = FastAPI()

# Carga las respuestas dinámicamente desde los archivos
RESPUESTAS = cargar_respuestas_desde_archivos()

# Variable para mantener el estado del contador.
# Se usa un diccionario para que sea mutable y se pueda modificar dentro de la función.
estado = {"contador": 0}

# Modelo de entrada para validar que se reciba un string
class EntradaTexto(BaseModel):
    prompt: str

@app.post("/generate")
def procesar_texto(entrada: EntradaTexto):
    """
    Recibe un texto y responde con uno de los tres strings definidos,
    rotando la respuesta en cada llamada.
    """
    # Selecciona la respuesta actual usando el contador
    respuesta_actual = RESPUESTAS[estado["contador"]]

    # Incrementa el contador y lo reinicia si llega a 3 usando el operador módulo
    estado["contador"] = (estado["contador"] + 1) % len(RESPUESTAS)

    # Devuelve la respuesta en un formato JSON
    return {"output": respuesta_actual}

@app.get("/")
def raiz():
    return {"mensaje": "Servidor funcionando. Usa el endpoint /generate con un método POST."}
