PY := $(if $(wildcard .venv/bin/python),.venv/bin/python,python3)

.PHONY: test serve

test:
	$(PY) -m pytest -q

serve:
	$(PY) -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000
