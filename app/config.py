from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    sandbox_api_key: str = Field(default="change-me-please")

    sandbox_image: str = Field(default="agent-sandbox:latest")
    # Map of template name → Docker image. The "default" key is the fallback
    # used when no template is specified. Add entries in .env as a JSON string:
    #   SANDBOX_TEMPLATES={"default":"agent-sandbox:latest","automation":"agent-sandbox:automation"}
    sandbox_templates: dict[str, str] = Field(
        default_factory=lambda: {"default": "agent-sandbox:latest"}
    )
    # Defaults to a path inside $HOME because Colima's Lima VM only mounts
    # /Users by default. Override via WORKSPACE_ROOT for production setups.
    workspace_root: Path = Field(
        default_factory=lambda: Path.home() / ".agent-sandbox" / "ws"
    )
    audit_db_path: Path = Field(default=Path("./data/sandbox_audit.db"))

    mem_limit_mb: int = 1024
    cpu_nanos: int = 2_000_000_000
    pids_limit: int = 256
    file_upload_max_mb: int = 50

    exec_timeout_s: int = 60
    kill_grace_s: int = 5
    idle_ttl_s: int = 1800
    max_age_s: int = 28_800
    max_sessions: int = 32

    egress_allowlist: str = "pypi.org,files.pythonhosted.org,*.pythonhosted.org"
    proxy_image: str = "ubuntu/squid:latest"
    proxy_container_name: str = "sbx-proxy"
    network_name: str = "sbx-net"

    @property
    def mem_limit_bytes(self) -> int:
        return self.mem_limit_mb * 1024 * 1024


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
