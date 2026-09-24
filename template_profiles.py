"""Public provider reference templates. Never reads local user configuration."""
from __future__ import annotations

import json
from pathlib import Path

PLACEHOLDERS = ('__ASTERGATE_API_KEY__', '__ASTERGATE_CA__', '__MODEL_CATALOG__')


def template(app):
    if app not in ('claude', 'codex'):
        raise ValueError('Unknown template client')
    path = Path(__file__).with_name('templates')/'aster'/f'{app}.json'
    return json.loads(path.read_text(encoding='utf-8'))


def render(app, key, assets):
    if not isinstance(key, str) or not key.strip() or key in PLACEHOLDERS:
        raise ValueError('A personal AsterGate API key is required')
    assets = Path(assets).expanduser().resolve()
    replacements = dict(zip(PLACEHOLDERS, (key, str(assets/'astergate-ca.crt'), str(assets/'model-catalog.json'))))

    def replace(value):
        if isinstance(value, dict):
            return {name: replace(item) for name, item in value.items()}
        if isinstance(value, list):
            return [replace(item) for item in value]
        return replacements.get(value, value) if isinstance(value, str) else value

    return replace(template(app))
