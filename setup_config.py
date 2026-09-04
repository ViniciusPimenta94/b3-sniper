#!/usr/bin/env python3

from pathlib import Path
import shutil
import sys

root = Path(__file__).resolve().parent

config = root / "config.json"
example = root / "config.example.json"

print(f"📁 Diretório do projeto: {root}")
print(f"📄 Procurando config.json: {config}")
print(f"📄 Procurando config.example.json: {example}")

if config.is_file():
    print("✅ config.json já existe.")
elif example.is_file():
    shutil.copy(example, config)
    print("✅ config.json criado a partir de config.example.json.")
else:
    print("❌ config.example.json não encontrado!")
    sys.exit(1)
