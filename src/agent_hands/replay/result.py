# TDD stub: names only, no behaviour.
from enum import StrEnum
from pydantic import BaseModel, ConfigDict
class _Stub(BaseModel):
    model_config = ConfigDict(extra="allow")
class FailureKind(StrEnum):
    TIMEOUT = "timeout"; APP_ERROR = "app_error"; CHECKPOINT_FAILED = "checkpoint_failed"
    POLICY_DENIED = "policy_denied"; COMMIT_STATE_UNKNOWN = "commit_state_unknown"
    PARAM_INVALID = "param_invalid"; TARGET_NOT_FOUND = "target_not_found"
class Failure(_Stub): pass
class Recovery(_Stub): pass
class Resolution(_Stub): pass
class StrategyAttempt(_Stub): pass
class StepRecord(_Stub): pass
class Succeeded(_Stub): pass
class Outcome(_Stub): pass
class Failed(_Stub): pass
class NeedsHuman(_Stub): pass
def parse_result(text): raise NotImplementedError
