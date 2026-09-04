#!/usr/bin/env python3
"""Copia config.example.json para config.json se config.json não existir."""
from pathlib import Path
import shutil

root = Path(__file__).resolve().parent
config = root / "config.json"
example = root / "config.example.json"

if not config.is_file() and example.is_file():
    shutil.copy(example, config)
    print("config.json criado a partir de config.example.json")
