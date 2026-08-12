"""Job state machine for the AI Engineering Studio."""

import logging
from enum import Enum, auto

logger = logging.getLogger(__name__)


class JobState(Enum):
    CREATED = auto()
    GOVERNING = auto()
    GOVERNED = auto()
    PLANNING = auto()
    PLAN_CHECK = auto()
    READY = auto()
    EXECUTING = auto()
    TESTING = auto()
    REVIEWING = auto()
    REPAIRING = auto()       # V3: Flash retrying after failure
    REPLANNING = auto()      # V3: Kimi generating repair plan
    ESCALATING = auto()      # V3: Codex Emergency taking over
    WAITING_USER = auto()    # V3: Waiting for human input
    REWORK = auto()
    DONE = auto()
    FAILED = auto()
    CANCELLED = auto()


class TaskState(Enum):
    PENDING = auto()
    READY = auto()
    RUNNING = auto()
    TESTING = auto()
    DONE = auto()
    FAILED = auto()
    SKIPPED = auto()


VALID_TRANSITIONS: dict[JobState, set[JobState]] = {
    JobState.CREATED: {JobState.CREATED, JobState.GOVERNING, JobState.CANCELLED},
    JobState.GOVERNING: {
        JobState.GOVERNED, JobState.WAITING_USER,
        JobState.FAILED, JobState.CANCELLED,
    },
    JobState.GOVERNED: {
        JobState.PLANNING, JobState.WAITING_USER,
        JobState.FAILED, JobState.CANCELLED,
    },
    JobState.PLANNING: {
        JobState.PLAN_CHECK, JobState.WAITING_USER,
        JobState.FAILED, JobState.CANCELLED,
    },
    JobState.PLAN_CHECK: {
        JobState.READY, JobState.PLANNING, JobState.WAITING_USER,
        JobState.FAILED, JobState.CANCELLED,
    },
    JobState.READY: {
        JobState.EXECUTING, JobState.WAITING_USER, JobState.CANCELLED,
    },
    JobState.EXECUTING: {
        JobState.TESTING, JobState.REPAIRING, JobState.WAITING_USER,
        JobState.FAILED, JobState.CANCELLED,
    },
    JobState.TESTING: {
        JobState.REVIEWING, JobState.REPAIRING, JobState.WAITING_USER,
        JobState.FAILED, JobState.CANCELLED,
    },
    JobState.REPAIRING: {JobState.EXECUTING, JobState.REPLANNING, JobState.ESCALATING, JobState.FAILED, JobState.CANCELLED},
    JobState.REPLANNING: {JobState.PLANNING, JobState.ESCALATING, JobState.FAILED, JobState.CANCELLED},
    JobState.ESCALATING: {JobState.EXECUTING, JobState.WAITING_USER, JobState.FAILED, JobState.CANCELLED},
    JobState.WAITING_USER: {
        JobState.GOVERNING, JobState.PLANNING, JobState.EXECUTING,
        JobState.REVIEWING, JobState.REPLANNING, JobState.CANCELLED,
    },
    JobState.REVIEWING: {
        JobState.DONE, JobState.REWORK, JobState.WAITING_USER,
        JobState.FAILED, JobState.CANCELLED,
    },
    JobState.REWORK: {
        JobState.PLANNING, JobState.EXECUTING, JobState.WAITING_USER,
        JobState.FAILED, JobState.CANCELLED,
    },
    JobState.DONE: set(),
    JobState.FAILED: set(),
    JobState.CANCELLED: set(),
}


class StateMachine:
    """Manages job state transitions with validation."""

    def __init__(self):
        self._states: dict[str, JobState] = {}
        self._listeners: list[callable] = []

    def get_state(self, job_id: str) -> JobState:
        return self._states.get(job_id, JobState.CREATED)

    def get_state_name(self, job_id: str) -> str:
        return self.get_state(job_id).name.lower()

    def transition(self, job_id: str, to_state: JobState) -> bool:
        current = self.get_state(job_id)
        if current not in VALID_TRANSITIONS or to_state not in VALID_TRANSITIONS[current]:
            logger.warning(
                f"Invalid transition: {job_id} {current.name} -> {to_state.name}"
            )
            return False
        self._states[job_id] = to_state
        logger.info(f"State: {job_id} {current.name} -> {to_state.name}")
        for listener in self._listeners:
            try:
                listener(job_id, current, to_state)
            except Exception as e:
                logger.error(f"State listener error: {e}")
        return True

    def can_transition(self, job_id: str, to_state: JobState) -> bool:
        current = self.get_state(job_id)
        return current in VALID_TRANSITIONS and to_state in VALID_TRANSITIONS[current]

    def add_listener(self, listener: callable):
        self._listeners.append(listener)

    def remove_listener(self, listener: callable):
        if listener in self._listeners:
            self._listeners.remove(listener)

    def reset(self, job_id: str):
        self._states.pop(job_id, None)

    def restore(self, job_id: str, state: JobState):
        """Restore a persisted checkpoint state without emitting a transition."""
        self._states[job_id] = state
