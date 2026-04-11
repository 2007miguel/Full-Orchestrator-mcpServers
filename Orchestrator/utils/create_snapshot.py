import os
import re
from pathlib import Path
import shutil

def create_snapshot_from_string(content: str, base_dir: str = 'snapshot'):
    """
    DEPRECATED: This function is complex. Use the combination of merge_snapshots 
    and create_snapshot_from_dict for clearer, more robust snapshot creation.
    This function remains for compatibility but will be removed in the future.
    """
    files_dict = _parse_snapshot_to_dict(content)
    
    if not files_dict:
        print("\nNo se encontraron archivos para crear en el snapshot.")
        return None

    return create_snapshot_from_dict(files_dict, base_dir)

def _parse_snapshot_to_dict(content: str) -> dict[str, str]:
    """Parses a snapshot string into a dictionary of {filepath: content}."""
    pattern = re.compile(r'\[([^\]]+)\]\n(.*?)\n\[/\1\]', re.DOTALL)
    files_dict = {}
    matches = pattern.finditer(content)
    for match in matches:
        relative_path_str = match.group(1)
        file_content = match.group(2).strip()
        files_dict[relative_path_str] = file_content
    return files_dict

def merge_snapshots(base_content: str, update_content: str) -> str:
    """
    Merges two snapshot content strings. Configurations in update_content will
    overwrite those in base_content if file paths are the same.
    """
    base_files = _parse_snapshot_to_dict(base_content)
    update_files = _parse_snapshot_to_dict(update_content)

    base_files.update(update_files)

    # Reconstruct the snapshot string from the merged dictionary
    merged_content_parts = []
    for path, file_content in base_files.items():
        merged_content_parts.append(f"[{path}]\n{file_content}\n[/{path}]")
    
    return "\n".join(merged_content_parts)

def create_snapshot_from_dict(files_dict: dict[str, str], base_dir: str = 'snapshot'):
    files_created = 0

    # Corregido: Iterar sobre el diccionario de archivos recibido, no sobre una variable 'content' inexistente.
    for relative_path_str, file_content in files_dict.items():
        output_path = Path(base_dir) / relative_path_str
        output_path.parent.mkdir(parents=True, exist_ok=True)

        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(file_content)
        
        files_created += 1
    
    if files_created > 0:
        base_dir_path = Path(base_dir)
        archive_name = base_dir_path
        root_dir = base_dir_path.parent
        dir_to_zip = base_dir_path.name

        try:
            zip_path = shutil.make_archive(str(archive_name), 'zip', root_dir, dir_to_zip)
            return zip_path
        finally:
            shutil.rmtree(base_dir_path)
    else:
        print("\nNo se encontraron archivos para crear en el snapshot.")
        return None

def create_snapshot_from_config(config_file_path: str, base_dir: str = 'snapshot'):
    try:
        with open(config_file_path, 'r', encoding='utf-8') as f:
            content = f.read()
    except FileNotFoundError:
        print(f"Error: No se encontró el archivo '{config_file_path}'.")
        return None
    except Exception as e:
        print(f"Ocurrió un error al leer el archivo: {e}")
        return None
        
    return create_snapshot_from_string(content, base_dir)

if __name__ == "__main__":
    # Construye la ruta al directorio de templates de forma relativa al script
    script_dir = Path(__file__).resolve().parent
    config_path = script_dir.parent / "templates" / "config.txt"
    snapshot_path = Path.home() / "Documents" / "snapshot"

    create_snapshot_from_config(str(config_path), base_dir=str(snapshot_path))