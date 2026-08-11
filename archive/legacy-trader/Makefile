.PHONY: install templates test lint verify

install:
	python3 -m venv .venv
	.venv/bin/pip install -e '.[dev]'

templates:
	.venv/bin/python scripts/generate_templates.py
	.venv/bin/a6api-auto validate-templates

test:
	.venv/bin/python -m pytest -q

lint:
	.venv/bin/python -m ruff check .

verify: lint test
	.venv/bin/python -m compileall -q src tests scripts
	.venv/bin/a6api-auto validate-templates
