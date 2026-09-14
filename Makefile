VENV := backend/.venv

.PHONY: backend frontend test

backend:
	cd backend && RESIDENT_AUTO_WAKE=1 .venv/bin/python -m uvicorn resident.main:app --reload --host 0.0.0.0 --port 8010

frontend:
	cd frontend && npm run dev

test:
	cd backend && .venv/bin/python -m pytest -q
