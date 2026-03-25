from core.prompt_manager import PromptManager
from models.execution_context import ExecutionContext
import json

def test_prompt_manager():
    # Inicializar PromptManager con la ruta a la carpeta templates
    pm = PromptManager("templates")

    # Crear un requirement falso
    req = {
        "intent": "Configurar rutas estáticas en R1 y R2 para establecer conectividad."
    }
    context = ExecutionContext(req)

    print("\n==================================================")
    print("1. CLASSIFIER PROMPT")
    print("==================================================")
    classifier_prompt = pm.build_classifier_prompt(context)
    print(classifier_prompt)


    # Simulamos la respuesta normalizada (que haría _normalize_flm_response)
    mock_classification_data = {
        "intent_type": "routing",
        "affected_devices": ["R1", "R2"]
    }
    
    # ExecutionController actualizaría el contexto con el prompt emitido y su respuesta simulada con status
    context.update(
        classifier_prompt, 
        {"status": "success", "data": mock_classification_data}
    )

    print("\n\n==================================================")
    print("2. TASKS PROMPT")
    print("==================================================")
    tasks_prompt = pm.build_tasks_prompt(context)
    print(tasks_prompt)

    # Simulamos la respuesta normalizada de las tareas generadas
    mock_tasks_data = {
        "tasks": [
            {"device": "R1", "action": "add static route x"},
            {"device": "R2", "action": "add static route y"}
        ]
    }
    context.update(
        tasks_prompt, 
        {"status": "success", "data": mock_tasks_data}
    )

    print("\n\n==================================================")
    print("3. CONFIG PROMPT")
    print("==================================================")
    config_prompt = pm.build_config_prompt(context)
    print(config_prompt)
    print("\n==================================================\n")


if __name__ == "__main__":
    test_prompt_manager()
