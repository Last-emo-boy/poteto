from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import re


def valid_domain(value):
    return bool(len(value)<=220 and re.fullmatch(
        r'(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{0,61}[a-z0-9]',value))


@dataclass(frozen=True)
class Settings:
    root: Path
    domain: str = "w33d.xyz"
    upload_limit: int = 50 * 1024 * 1024
    unpack_limit: int = 200 * 1024 * 1024
    file_limit: int = 10000
    new_app_domain: str | None = None
    static_artifacts: bool | tuple[str, ...] = False
    static_source_builds: bool = False
    sandbox_projects: tuple[str, ...] = ()
    prebuilt_image_projects: tuple[str, ...] = ()
    sandbox_sleep_projects: tuple[str, ...] = ()
    personal_previews: bool = False
    sandbox_memory_limit_mib: int = 2048
    sandbox_cpu_limit: float = 1.5

    def __post_init__(self):
        if not valid_domain(self.domain):
            raise ValueError('PAAS_DOMAIN must be a lowercase DNS name')
        if self.new_app_domain and (not valid_domain(self.new_app_domain) or
                self.new_app_domain==self.domain or self.new_app_domain.endswith('.'+self.domain) or
                self.domain.endswith('.'+self.new_app_domain)):
            raise ValueError('PAAS_NEW_APP_DOMAIN must be a separate DNS domain from the legacy/business domain')

        if type(self.static_artifacts) is not bool and not (
            isinstance(self.static_artifacts, (tuple, list)) and
            all(isinstance(project, str) and re.fullmatch(r'[a-f0-9]{24}', project)
                for project in self.static_artifacts)
        ):
            raise ValueError('static_artifacts must be a boolean or a list of project IDs')
        for name in ('sandbox_projects', 'sandbox_sleep_projects', 'prebuilt_image_projects'):
            projects = getattr(self, name)
            if not isinstance(projects, (tuple, list)) or not all(
                isinstance(project, str) and re.fullmatch(r'[a-f0-9]{24}', project) for project in projects
            ):
                raise ValueError(f'{name} must be a list of project IDs')

        if type(self.static_source_builds) is not bool:
            raise ValueError('static_source_builds must be a boolean')
        if type(self.personal_previews) is not bool:
            raise ValueError('personal_previews must be a boolean')
        if type(self.sandbox_memory_limit_mib) is not int or not 256 <= self.sandbox_memory_limit_mib <= 16384:
            raise ValueError('sandbox_memory_limit_mib must be between 256 and 16384')
        if (type(self.sandbox_cpu_limit) not in (int, float) or not math.isfinite(self.sandbox_cpu_limit)
                or not 0.1 <= self.sandbox_cpu_limit <= 8):
            raise ValueError('sandbox_cpu_limit must be between 0.1 and 8')

    def upload_limits(self, runtime):
        if runtime == "image":
            from publisher.image_contract import UPLOAD_LIMIT, UNPACK_LIMIT, FILE_LIMIT
            return UPLOAD_LIMIT, UNPACK_LIMIT, FILE_LIMIT
        return self.upload_limit, self.unpack_limit, self.file_limit

    def artifact_enabled(self, project, config, *, restored=False, personal=False):
        selection = self.static_artifacts
        return ((personal and self.personal_previews) or selection is True or (selection is not False and project in selection)) and (
            config['runtime'] == 'static' and (
                not config.get('build_command') or self.static_source_builds or restored))

    def sandbox_enabled(self, project, config, *, personal=False):
        selected = personal and self.personal_previews
        return ((selected or project in self.sandbox_projects) and config['target'] == 'preview'
                and config['runtime'] in {'node', 'dockerfile', 'image'} and config['memory_mb'] >= 256
                and (not selected or config['runtime'] == 'image')
                and (not config.get('sleep') or selected or project in self.sandbox_sleep_projects)
                and not any(config.get(k) for k in ('database_id', 'volume_id', 'migration_command')))

    @classmethod
    def from_env(cls):
        # The unprivileged API must read this non-secret configuration
        # without access to the separate directory of operator credentials.
        path=Path('/etc/paas-publisher-domains.json')
        options=json.loads(path.read_text()) if path.exists() else {}
        runtime_path = Path('/etc/paas-publisher-runtime.json')
        runtime = json.loads(runtime_path.read_text()) if runtime_path.exists() else {}
        return cls(Path(os.environ.get("PAAS_DATA", "/var/lib/paas-publisher")),
                   os.environ.get("PAAS_DOMAIN", options.get('legacy_domain','w33d.xyz')),
                   new_app_domain=os.environ.get("PAAS_NEW_APP_DOMAIN",options.get('new_app_domain')) or None,
                   static_artifacts=runtime.get('static_artifacts', False),
                   static_source_builds=runtime.get('static_source_builds', False),
                   sandbox_projects=runtime.get('sandbox_projects', ()),
                   sandbox_sleep_projects=runtime.get('sandbox_sleep_projects', ()),
                   prebuilt_image_projects=runtime.get('prebuilt_image_projects', ()),
                   personal_previews=runtime.get('personal_previews', False),
                   sandbox_memory_limit_mib=runtime.get('sandbox_memory_limit_mib', 2048),
                   sandbox_cpu_limit=runtime.get('sandbox_cpu_limit', 1.5))

    @property
    def sandbox_limits(self):
        return {'memory_mib': self.sandbox_memory_limit_mib, 'vcpus': self.sandbox_cpu_limit}

    @property
    def db(self):
        return self.root / "control.sqlite3"

    def hostname(self, project_id, deployment_id=None, *, domain=None):
        return f"{'preview-' + deployment_id if deployment_id else 'app-' + project_id}.{domain or self.domain}"
