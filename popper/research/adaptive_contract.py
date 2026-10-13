"""Frozen task authorization, separate from append-only agent proposals."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator
from ..core import digest


class AdaptiveTaskContract(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True, strict=True, serialize_by_alias=True, populate_by_name=True)
    schema_name: Literal['adaptive-task/v1'] = Field(default='adaptive-task/v1', alias='schema')
    planning_protocol: Literal['research-runtime/v2'] = 'research-runtime/v2'
    task_id: str = Field(min_length=1)
    objective: str = Field(min_length=1)
    target_column: str = Field(min_length=1)
    feature_names: list[str] = Field(min_length=1)
    data_hashes: dict[str, str]
    baseline: dict
    analysis_unit: str = Field(min_length=1)
    independence_verified: bool = False
    sampling: dict | None = None
    groups: dict[str, str]
    metric: Literal['mae'] = 'mae'
    effect: Literal['relative_mae_improvement'] = 'relative_mae_improvement'
    unit: Literal['ratio'] = 'ratio'
    threshold: Literal[0.05] = 0.05
    confidence: Literal[0.95] = 0.95
    bootstrap_samples: Literal[10000] = 10000
    minimum_units: Literal[30] = 30
    bootstrap_seed: int = Field(ge=0)
    model_seed: Literal[17, 29]
    max_experiments: Literal[8] = 8
    max_logical_requests: Literal[12] = 12
    max_model_calls: Literal[24] = 24
    max_model_tokens: Literal[250000] = 250000
    wall_seconds: Literal[5400] = 5400
    code_files: list[str] = Field(default_factory=lambda: ['model.py'])
    materials: list[dict] = Field(default_factory=list, max_length=5)

    @model_validator(mode='after')
    def validate_identities(self):
        if set(self.data_hashes) != {'train', 'dev', 'confirmation'}:
            raise ValueError('All three input identities are required')
        if any(len(v) != 64 or any(c not in '0123456789abcdef' for c in v) for v in self.data_hashes.values()):
            raise ValueError('Invalid input SHA-256')
        if self.code_files != ['model.py'] or len(set(self.feature_names)) != len(self.feature_names):
            raise ValueError('Unsupported code permissions or duplicate features')
        if not self.groups or any(not k or not v for k, v in self.groups.items()):
            raise ValueError('Analysis-unit identities are required')
        return self

    @property
    def sha256(self):
        return digest(self.model_dump())

    def public(self):
        return self.model_dump(exclude={'data_hashes', 'groups', 'bootstrap_seed'})
