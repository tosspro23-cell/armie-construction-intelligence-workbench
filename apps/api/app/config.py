from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parents[3] / ".env",
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
    # ``localhost`` makes the native macOS development path work. Docker users
    # override this with host.docker.internal in their local .env.
    ollama_base_url: str = "http://127.0.0.1:11434"
    ollama_text_model: str = "qwen3:8b"
    ollama_vision_model: str = "qwen3-vl:8b"

    checkpoint_db_path: Path = Path("./runtime/checkpoints.sqlite")
    audit_store_path: Path = Path("./runtime/audit.jsonl")
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
