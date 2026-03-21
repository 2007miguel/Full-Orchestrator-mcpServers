import os
import re
from pathlib import Path
import shutil

def create_snapshot_from_config(config_file_path: str, base_dir: str = 'snapshot'):

    try:
        with open(config_file_path, 'r', encoding='utf-8') as f:
            content = f.read()
    except FileNotFoundError:
        print(f"Error: No se encontró el archivo '{config_file_path}'.")
        return
    except Exception as e:
        print(f"Ocurrió un error al leer el archivo: {e}")
        return
    pattern = re.compile(r'\[([^\]]+)\]\n(.*?)\n\[/\1\]', re.DOTALL)

    matches = pattern.finditer(content)
    files_created = 0

    for match in matches:
        relative_path_str = match.group(1)
        file_content = match.group(2).strip()

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
        finally:
            shutil.rmtree(base_dir_path)
    else:
        print("\nNo se encontraron archivos para crear en el snapshot.")

if __name__ == "__main__":
    snapshot_path = Path.home() / "Documents" / "snapshot"

    create_snapshot_from_config('config.txt', base_dir=str(snapshot_path))