import time
from dataclasses import asdict, dataclass, field


@dataclass(frozen=True)
class StepHandle:
    """Everything needed to finish a step from another process: store or send it as a dict."""

    step_id: str
    name: str
    workflow_id: str
    run_id: str
    attempt_number: int = 1
    trace_id: str | None = None
    correlation_id: str | None = None
    workflow_name: str | None = None
    started_at: float = field(default_factory=time.time)  # wall clock, so it survives a process hop

    @property
    def id(self) -> str:
        return self.step_id

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "StepHandle":
        return cls(**data)
