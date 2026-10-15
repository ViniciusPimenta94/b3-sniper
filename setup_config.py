#!/usr/bin/env python3

from pathlib import Path
import sys

root = Path(__file__).resolve().parent

config = root / "config.json"
local_config = root / "config.local.json"

print(f"📁 Diretório do projeto: {root}")
print(f"📄 Procurando config.json: {config}")
print(f"📄 Procurando config.local.json: {local_config}")

if config.is_file():
    print("✅ config.json já existe.")
    if local_config.is_file():
        print("✅ config.local.json será usado localmente para credenciais.")
else:
    print("❌ config.json não encontrado!")
    sys.exit(1)
