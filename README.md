# Resident

Resident is an experimental persistent digital individual. It maintains an event-backed history, revisits memories and unfinished threads, and continues a bounded cognitive loop between conversations. Its activities and responses can be inspected through their recorded provenance.

## Features

- **Persistent life cycle:** scheduled wake, reflection, rest, and sleep, including explicit no-op outcomes.
- **Memory and continuity:** event history, memory retrieval, thought and question threads, and provenance-checked re-entry after an absence.
- **Optional world input:** external material can enter the same experience history through the World Window.
- **Read-only monitor:** timeline, provenance, threads, questions, retrieval traces, and continuity views alongside the conversation interface.
- **Optional Agency:** an independently invoked action layer that applies a deterministic policy, runs `py-claw` in an isolated subprocess, and records action outcomes in the Event Store. The default policy permits read-only actions.

## Quick start

Requires Python 3.11+, Node.js, and npm. From the repository root:

```bash
python3 -m venv backend/.venv
backend/.venv/bin/python -m pip install -e './backend[dev]'
npm ci --prefix frontend
cp frontend/.env.example frontend/.env.local
```

Start the API and WebUI in separate terminals:

```bash
make backend
make frontend
```

Open [http://localhost:5173](http://localhost:5173). The API listens on `http://localhost:8010`. Run the backend tests with `make test`.

The WebUI reads `RESIDENT_API_TARGET` from `frontend/.env.local` when the Vite development server starts. Set it to an API address reachable from the computer running Vite. For a remote API bound to its own loopback interface, forward that port through SSH and point this setting at the local forwarded port. Restart the frontend after changing the env file.

The default profile runs without a configured model provider. For model-backed operation, set `RESIDENT_MODEL_BASE_URL`, `RESIDENT_MODEL_API_KEY`, and `RESIDENT_MODEL_NAME`. Optional capabilities are controlled through environment variables; see the [living baseline guide](docs/LIVING_BASELINE.md) and the [Agency design](docs/ADR-0018-agency-action-layer.md). Enable the Agency with `RESIDENT_PROFILE=active-v3 make backend`; action execution also requires a configured `py-claw` runtime.

## Project structure

| Path | Purpose |
| --- | --- |
| [`backend/src/resident/`](backend/src/resident/) | FastAPI application, life cycle, memory, Event Store, world input, and Agency |
| [`backend/tests/`](backend/tests/) | Backend test suite |
| [`backend/scripts/`](backend/scripts/) | Simulation, analysis, and maintenance tools |
| [`frontend/src/`](frontend/src/) | React WebUI and read-only monitor |
| `resident_home/` | Local runtime data and private workspace |
| [`docs/`](docs/) | Design decisions, release evidence, and experiment reports |

## Documentation

- [Vision](VISION.md) and [architecture](ARCHITECTURE.md)
- [Event model](EVENT_MODEL.md) and [development plan](DEVELOPMENT.md)
- [v0.1 release evidence](docs/RELEASE-v0.1.md) and [v1.0 Agency audit](docs/RELEASE-AUDIT-v1.0.md)
