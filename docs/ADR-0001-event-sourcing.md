# ADR-0001: Append-oriented event history

Status: Accepted

Resident needs a truthful, replayable history of what it actually experienced and did. Mutable summaries can drift. Therefore the event log is the historical source of truth, while derived views may be rebuilt.
