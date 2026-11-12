import re
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProjectInput(Input):
    name: str = Field(pattern=r"^[a-z][a-z0-9-]{0,39}$")


class DeploymentInput(Input):
    runtime: Literal["static", "node", "dockerfile", "image"] = "static"
    target: Literal["production", "preview"] = "production"
    build_command: str | None = Field(default=None, max_length=2000)
    start_command: str | None = Field(default=None, max_length=2000)
    output_directory: str = "."
    dockerfile: str = "Dockerfile"
    port: int = Field(default=3000, ge=1024, le=65535)
    health_check: str = "/"
    sleep: bool = False
    outbound_network: Literal["none", "web"] = "none"
    memory_mb: int = Field(default=256, ge=64, le=1024)
    cpus: float = Field(default=0.5, ge=0.1, le=1)
    database_id: str | None = Field(default=None, pattern=r'^[a-f0-9]{24}$')
    volume_id: str | None = Field(default=None, pattern=r'^[a-f0-9]{24}$')
    migration_command: str | None = Field(default=None, min_length=1, max_length=2000,
        description='Runs once in an isolated runtime container before readiness. Use backward-compatible migrations: code rollback does not revert schema or data.')

    @model_validator(mode='after')
    def data_requirements(self):
        if self.runtime == 'static' and self.outbound_network != 'none':
            raise ValueError('Static files do not have an outbound network')
        if self.runtime == 'static' and (self.database_id or self.volume_id or self.migration_command):
            raise ValueError('Data resources require a dynamic runtime')
        if self.runtime == 'image' and (self.build_command is not None or self.start_command is not None
                                       or self.output_directory != '.' or self.dockerfile != 'Dockerfile'):
            raise ValueError('Prebuilt images use their own entrypoint; source build settings are not supported')
        if self.migration_command and not self.database_id:
            raise ValueError('A migration requires an explicit database_id')
        return self

    def configuration(self, *, defaults=None):
        value = self.model_dump()
        for key, default in (defaults or {}).items():
            if key not in self.model_fields_set:
                value[key] = default
        # Preserve the serialized contract of existing idempotency keys.
        for key in ['database_id', 'volume_id', 'migration_command']:
            if value[key] is None:
                del value[key]
        if value["outbound_network"] == "none":
            del value["outbound_network"]
        return value

    @field_validator("output_directory", "dockerfile")
    @classmethod
    def relative_path(cls, value):
        if not value or len(value) > 240 or value.startswith("/") or "\\" in value or any(p == ".." for p in value.split("/")):
            raise ValueError("Use a relative path inside the uploaded project")
        if any(ord(c) < 32 for c in value):
            raise ValueError("Control characters are not allowed")
        return value

    @field_validator("health_check")
    @classmethod
    def health_path(cls, value):
        if not re.fullmatch(r"/[a-zA-Z0-9/_.?=&%-]*", value) or value.startswith("//") or len(value) > 200:
            raise ValueError("Use an HTTP path, not a URL")
        return value


class PreflightInput(Input):
    configuration: DeploymentInput
    archive_bytes: int = Field(gt=0)
    unpacked_bytes: int = Field(gt=0)
    files: int = Field(gt=0)


class EnvironmentInput(Input):
    values: dict[str, str]

    @field_validator("values")
    @classmethod
    def environment(cls, values):
        if len(values) > 100 or any(not re.fullmatch(r"[A-Z_][A-Z0-9_]{0,63}", k) or len(v) > 16384 or "\x00" in v for k, v in values.items()):
            raise ValueError("Invalid environment variables")
        if len(str(values)) > 65536:
            raise ValueError("Environment exceeds 64 KiB")
        if any(k.startswith("PAAS_") or k in {"DATABASE_URL", "PORT"} for k in values):
            raise ValueError("Platform-managed variables cannot be overridden")
        return values


class EnvironmentPatchInput(EnvironmentInput):
    values: dict[str, str] = Field(default_factory=dict)
    remove: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("remove")
    @classmethod
    def removed_keys(cls, keys):
        EnvironmentInput.environment({key: "" for key in keys})
        return keys

    @model_validator(mode="after")
    def distinct_operations(self):
        if not self.values and not self.remove:
            raise ValueError("Provide variables to set or remove")
        if set(self.values) & set(self.remove):
            raise ValueError("A variable cannot be set and removed in the same request")
        return self


class RedeployInput(Input):
    target: Literal["preview", "production"] = "preview"


class TokenInput(Input):
    name: str = Field(min_length=1, max_length=80)
    scopes: list[str]
    project_ids: list[str] | None = None
    expires_in_days: int = Field(default=30, ge=1, le=365)


class ReleaseInput(Input):
    deployment_id: str = Field(pattern=r"^[a-f0-9]{24}$")


class DataInput(Input):
    stage: Literal['production', 'preview'] = 'production'
