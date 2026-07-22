# Prueba — evaluar predicciones ya generadas

Esta carpeta permite evaluar predicciones **ya sacadas** con el **mismo pipeline del sistema
completo** (`evaluate_system.py`): verificación con Batfish **más el bucle de reparación**.
La única diferencia es que la configuración inicial no la genera el modelo, sino que se lee
del CSV.

Por eso las métricas son directamente comparables con las de `evaluate_system.py`:
first-pass success, final success y refinement success.

## Estructura
- `predicciones/` — aquí dejas tus CSV de predicciones (uno o varios).
- `resultados/`  — aquí se escribe automáticamente un CSV de resultados por cada archivo evaluado.

## Formato del CSV de predicciones
Obligatorias (se toma la primera que aparezca de cada lista):
- Predicción: `final_prediction`, `prediction`, `predictions` o `generated_config`.
- Requerimiento: `requirement`, `request` o `intent`. **El bucle de reparación la necesita**:
  al pedirle al modelo que corrija una línea, hay que decirle qué se estaba intentando lograr.
  Si tu CSV no la tiene, usa `--no-refine`.

Opcional:
- Referencia para calcular ROUGE: `ground_truth`, `configuration`, `confguration` o `reference`.

## Cómo correr
Desde la **raíz del repositorio**:

```bash
# Evalúa TODOS los CSV que estén en evaluacion_sistema/prueba/predicciones/
python evaluacion_sistema/evaluate_predictions.py

# O un archivo puntual
python evaluacion_sistema/evaluate_predictions.py --input evaluacion_sistema/prueba/predicciones/mis_preds.csv

# Limitar filas para una prueba rápida
python evaluacion_sistema/evaluate_predictions.py --limit 20

# Solo verificar, sin reparar (no necesita columna de requerimiento ni el servidor FLM)
python evaluacion_sistema/evaluate_predictions.py --no-refine
```

En consola verás, por predicción, `PASS (first pass)`, `PASS (repaired in N iter)` o
`FAIL (razón)`, y al final el mismo resumen de métricas que el sistema completo.

El CSV de resultados trae `requirement, ground_truth, original_prediction, final_prediction`
— `original_prediction` es lo que venía en el CSV y `final_prediction` lo que quedó después
de reparar, así puedes ver qué cambió el bucle.

> Nota: requiere el servidor MCP de **Batfish** y, salvo con `--no-refine`, también el de
> **FLM** (el bucle de reparación le pide al modelo las líneas corregidas). En
> `Orchestrator/mcp_client.py` las rutas de `SERVERS` apuntan a Windows; en macOS ajusta
> la ruta del python del venv.
