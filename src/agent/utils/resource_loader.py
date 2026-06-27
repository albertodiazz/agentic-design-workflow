"""Utilities to load prompt, JSON and JavaScript resources.

Conventions:
- skills/*.md contain LLM instructions.
- json/**/*.json contain parseable contracts/configuration/examples.
- js/*.js contain Penpot Plugin API scripts.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping


UTILS_DIR = Path(__file__).resolve().parent
SKILLS_DIR = UTILS_DIR / "skills"
JSON_DIR = UTILS_DIR / "json"
JS_DIR = UTILS_DIR / "js"


@lru_cache(maxsize=128)
def read_text_resource(root: str, relative_path: str) -> str:
    base = {
        "skills": SKILLS_DIR,
        "json": JSON_DIR,
        "js": JS_DIR,
    }[root]

    path = (base / relative_path).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Resource not found: {path}")

    return path.read_text(encoding="utf-8").strip()


def read_skill(relative_path: str) -> str:
    return read_text_resource("skills", relative_path)


def load_js(relative_path: str) -> str:
    return read_text_resource("js", relative_path)


@lru_cache(maxsize=64)
def load_json_resource(relative_path: str) -> Any:
    text = read_text_resource("json", relative_path)
    return json.loads(text)


def render_template(template: str, variables: Mapping[str, Any]) -> str:
    rendered = template
    for key, value in variables.items():
        rendered = rendered.replace("{{" + key + "}}", str(value))
    return rendered


def render_skill(relative_path: str, variables: Mapping[str, Any]) -> str:
    return render_template(read_skill(relative_path), variables)
