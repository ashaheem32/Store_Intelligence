.PHONY: install test run detect lint

install:
	poetry install

test:
	.venv/bin/pytest --cov=app --cov-report=term-missing

run:
	.venv/bin/uvicorn app.main:app --reload --port 8000

detect:
	bash pipeline/run.sh ./Footage

lint:
	ruff check .
