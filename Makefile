VENV := backend/.venv

.PHONY: backend frontend test pyclaw-test sync-public sync-public-dry

backend:
	cd backend && RESIDENT_AUTO_WAKE=1 .venv/bin/python -m uvicorn resident.main:app --reload --host 0.0.0.0 --port 8010

frontend:
	cd frontend && npm run dev

test:
	cd backend && .venv/bin/python -m pytest -q

# py-claw (git submodule, ADR-0024): run its pytest suite from the in-repo
# py-claw/ tree (populate with `git submodule update --init py-claw`). Uses
# py-claw/.venv if it exists (the Windows deploy builds it
# there); otherwise falls back to python3.
pyclaw-test:
	@if [ -x py-claw/.venv/bin/python ]; then \
		cd py-claw && .venv/bin/python -m pytest tests; \
	else \
		cd py-claw && python3 -m pytest tests; \
	fi

# Publish the CLEAN code+core-docs mirror to the public repo (never leaks data).
# This is the only sanctioned way to update github.com/cacaview/resident_concept.
sync-public:
	backend/scripts/sync_public.sh

sync-public-dry:
	backend/scripts/sync_public.sh --dry-run
