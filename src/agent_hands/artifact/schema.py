# TDD stub: names only, no behaviour.
from enum import StrEnum
from pydantic import BaseModel, ConfigDict
class Sensitivity(StrEnum):
    PUBLIC = "public"; INTERNAL = "internal"; PII = "pii"; SECRET = "secret"
class ParamError(Exception):
    param = None
class CapabilityArtifact(BaseModel):
    model_config = ConfigDict(extra="allow")
def required_bump(old, new):
    raise NotImplementedError
