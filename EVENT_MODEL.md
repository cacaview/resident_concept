# Event Model v0.1

## Base event

```json
{
  "id": "evt_uuid",
  "created_at": "ISO-8601",
  "type": "thought.created",
  "actor": "resident",
  "visibility": "private",
  "content": {"text": "..."},
  "links": {
    "caused_by": ["evt_..."],
    "related_to": ["evt_..."],
    "thread_id": "thr_..."
  },
  "provenance": {
    "source": "mind_loop",
    "model": "provider/model",
    "run_id": "wake_...",
    "artifact_ids": []
  },
  "metadata": {}
}
```

## Event families

Conversation: `conversation.user_message`, `conversation.resident_message`, `conversation.session_started`, `conversation.session_ended`

Experience: `experience.created`, `observation.recorded`, `reading.started`, `reading.completed`

Thought: `thought.created`, `thought.revisited`, `thought.archived`

Question: `question.created`, `question.revisited`, `question.resolved`, `question.abandoned`

Belief: `belief.created`, `belief.revised`, `belief.retracted`

Thread: `thread.created`, `thread.activated`, `thread.dormant`, `thread.revisited`, `thread.closed`

Project: `project.created`, `project.state_changed`, `project.note`, `project.abandoned`, `project.completed`

Artifact: `artifact.created`, `artifact.updated`, `artifact.archived`

Wake/sleep: `wake.started`, `wake.route_selected`, `wake.noop`, `wake.completed`, `sleep.started`, `sleep.consolidation_completed`

Exploration: `exploration.started`, `exploration.note`, `exploration.failed`, `exploration.completed`

Re-entry: `reentry.evaluated`, `reentry.proposed`, `reentry.delivered`, `reentry.skipped`

Self: `self.observation`, `self.issue_detected`, `self.change_proposed`, `self.experiment_started`, `self.experiment_completed`

## Visibility

- `private`: durable Resident note
- `shareable`: may be surfaced to user
- `user_visible`: already shown
- `system`: runtime/audit event

## Derived views

Do not treat these as unquestioned primary truth: current interests, current mind, recurring themes, active threads, relationship model, diversity indicators. Build them from history and cache if needed.

## Provenance rule

Every re-entry sentence that makes a factual claim about Resident activity must be supported by one or more event IDs.
