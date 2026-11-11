from dataclasses import dataclass
import json
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

    def __post_init__(self):
        if not valid_domain(self.domain):
            raise ValueError('PAAS_DOMAIN must be a lowercase DNS name')
        if self.new_app_domain and (not valid_domain(self.new_app_domain) or
                self.new_app_domain==self.domain or self.new_app_domain.endswith('.'+self.domain) or
                self.domain.endswith('.'+self.new_app_domain)):
            raise ValueError('PAAS_NEW_APP_DOMAIN must be a separate DNS domain from the legacy/business domain')

    @classmethod
    def from_env(cls):
        # The unprivileged API must read this non-secret configuration
        # without access to the separate directory of operator credentials.
        path=Path('/etc/paas-publisher-domains.json')
        options=json.loads(path.read_text()) if path.exists() else {}
        return cls(Path(os.environ.get("PAAS_DATA", "/var/lib/paas-publisher")),
                   os.environ.get("PAAS_DOMAIN", options.get('legacy_domain','w33d.xyz')),
                   new_app_domain=os.environ.get("PAAS_NEW_APP_DOMAIN",options.get('new_app_domain')) or None)

    @property
    def db(self):
        return self.root / "control.sqlite3"

    def hostname(self, project_id, deployment_id=None, *, domain=None):
        return f"{'preview-' + deployment_id if deployment_id else 'app-' + project_id}.{domain or self.domain}"
