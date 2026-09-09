from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


def _default_env_file(config_file: Path) -> Path:
    """Repo-root ``.env``, robust to how deep this file sits on disk.

    Four chained ``.parent`` hops from ``config_file`` never raise --
    unlike the ``.parents[3]`` indexing this replaces, which raised
    ``IndexError`` and crashed the API container at import time
    (SPEC-M3 §6 addendum): ``apps/api/Dockerfile``'s ``WORKDIR /app`` +
    ``COPY apps/api /app`` puts this file at ``/app/app/config.py``, only 3
    directories below the container's filesystem root, one short of the 4
    the local dev checkout layout (``.../apps/api/app/config.py``) always
    has. At that shallow a depth, chained ``.parent`` simply stops at the
    filesystem root (whose own ``.parent`` is itself) instead of raising --
    the resulting ``.env`` path won't exist there, which pydantic-settings
    already treats as "no file to load", the correct behaviour for a
    container that gets its configuration from real environment variables.
    """
    root = config_file.resolve()
    for _ in range(4):
        root = root.parent
    return root / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_default_env_file(Path(__file__)),
        extra="ignore",
    )

    app_env: str = "development"
    data_dir: Path = Path("./demo_data")
    ifc_file: str = "armie_demo.ifc"
    pdf_file: str = "armie_demo_schedule.pdf"

    llm_provider: str = "ollama"
    openai_api_key: str | None = None
    openai_text_model: str = "gpt-4.1-mini"
    openai_vision_model: str = "gpt-4.1-mini"
    # Azure OpenAI (SPEC-M3): Managed Identity only (OD-23), no API-key path.
    # ``azure_openai_endpoint`` is required only when llm_provider="azure".
    azure_openai_endpoint: str | None = None
    azure_openai_api_version: str = "2024-10-21"
    azure_openai_text_deployment: str = "gpt-4o-mini"
    azure_openai_vision_deployment: str = "gpt-4o-mini"
    # Empty by default: OpenTelemetry exports nowhere unless explicitly set,
    # the same opt-in pattern as ollama_escalation_model below.
    otel_exporter_connection_string: str | None = None
    # ``localhost`` makes the native macOS development path work. Docker users
    # override this with host.docker.internal in their local .env.
    ollama_base_url: str = "http://127.0.0.1:11434"
    ollama_text_model: str = "qwen3:8b"
    ollama_vision_model: str = "qwen3-vl:8b"
    # Bounded semantic-repair escalation (SPEC-M1 §4.2/9, OD-2) is opt-in and
    # disabled by default: no developer or CI environment is silently
    # required to hold a 30B model. Set this to escalate persistent semantic
    # validation failures to a larger local model before falling back to
    # clarification.
    ollama_escalation_model: str | None = None

    audit_store_path: Path = Path("./runtime/audit.jsonl")
    # Postgres-backed conversation/audit persistence (SPEC-M4, OD-20/24) is
    # opt-in, the same pattern as otel_exporter_connection_string above:
    # unset keeps today's in-memory dict + local JSONL file behaviour, so no
    # developer or CI environment is required to run Postgres. Setting it
    # switches app.state.container's ConversationStore/AuditStore (see
    # app/persistence/factory.py) to the Postgres-backed implementations.
    database_url: str | None = None
    # Authenticate to Postgres with an Entra ID access token obtained via
    # DefaultAzureCredential instead of a password embedded in database_url
    # -- continuing OD-23's zero-stored-secret posture onto this project's
    # second Azure-managed data resource (OD-26). Only meaningful in Azure;
    # local development against a local Postgres uses a plain password.
    database_use_managed_identity: bool = False
    evidence_dir: Path = Path("./runtime/evidence")
    route_confidence_threshold: float = 0.70
    pdf_confidence_threshold: float = 0.75
    max_verification_retries: int = 1
    # Vision models may spend longer on a cold load than text planning. Keep
    # the request bounded, but do not turn a legitimate first browser call
    # into a false PDF failure at the provider's 30-second boundary.
    model_call_timeout_seconds: float = 90.0
    request_timeout_seconds: float = 180.0

    @property
    def ifc_path(self) -> Path:
        return self.data_dir / self.ifc_file

    @property
    def pdf_path(self) -> Path:
        return self.data_dir / self.pdf_file

    def ensure_runtime_directories(self) -> None:
        self.audit_store_path.parent.mkdir(parents=True, exist_ok=True)
        self.evidence_dir.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_runtime_directories()
    return settings
