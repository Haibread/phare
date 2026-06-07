"""Wire DTOs. Domain/ORM models never get serialized directly; map to these explicitly.

All JSON fields are camelCase on the wire (alias generator), populatable by field name.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class ApiModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class HealthResponse(ApiModel):
    status: str
    service: str
    version: str
