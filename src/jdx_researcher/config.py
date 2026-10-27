import os
import secrets
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import yaml
from platformdirs import user_data_path
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    data_dir: Path = Field(default_factory=lambda: user_data_path("jdx-researcher"))
    mode: Literal["local", "retrieval", "demo"] = "local"
    bind: Literal["127.0.0.1", "::1"] = "127.0.0.1"
    port: int = Field(8787, ge=1024, le=65535)
    llm_base_url: str = "http://127.0.0.1:8080/v1"
    llm_model: str = "research-worker"
    llm_context_tokens: int = Field(16384, ge=4096, le=32768)
    llm_timeout_seconds: float = Field(120, gt=0, le=600)
    llm_max_tokens: int = Field(2048, ge=256, le=4096)
    searxng_url: str = "http://127.0.0.1:8888"
    http_timeout_seconds: float = Field(25, gt=0, le=120)
    download_concurrency: int = Field(4, ge=1, le=4)
    max_html_bytes: int = Field(5 * 1024**2, ge=1024, le=5 * 1024**2)
    max_pdf_bytes: int = Field(20 * 1024**2, ge=1024, le=20 * 1024**2)
    max_pdf_pages: int = Field(50, ge=1, le=50)
    max_text_chars: int = Field(500000, ge=5000, le=500000)
    ranking: Literal["lexical", "qwen3"] = "lexical"
    reranker_revision: str = "main"
    reranker_device: Literal["auto", "cpu", "mps"] = "auto"
    model_revision: str = "UNVERIFIED"
    model_sha256: str = "UNVERIFIED"
    chat_template_revision: str = "UNVERIFIED"
    lease_seconds: float = Field(60, ge=3, le=300)
    poll_seconds: float = Field(0.5, gt=0, le=10)
    api_token: SecretStr | None = None
    # Raises the GitHub search rate limit (10/min anonymous); each github query uses 4 views.
    github_token: SecretStr | None = None

    @model_validator(mode="after")
    def trusted_service_urls(self):
        for value in (self.llm_base_url, self.searxng_url):
            parsed = urlsplit(value)
            if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError("Service URL must be HTTP(S) without embedded credentials")
            if parsed.query or parsed.fragment:
                raise ValueError("Service URL cannot contain a query or fragment")
        return self

    def token(self) -> str:
        if self.api_token:
            return self.api_token.get_secret_value()
        token_path = self.data_dir / "api-token"
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            fd = os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return token_path.read_text().strip()
        value = secrets.token_urlsafe(32)
        with os.fdopen(fd, "w") as stream:
            stream.write(value)
        return value

    def public_manifest(self) -> dict:
        data = self.model_dump(mode="json", exclude={"api_token", "github_token"})
        data.pop("data_dir", None)
        return data


def load_settings(path: Path | None = None) -> Settings:
    values = yaml.safe_load(path.read_text()) if path else {}
    values = values or {}
    for env, key in {
        "JDX_DATA_DIR": "data_dir",
        "JDX_MODE": "mode",
        "JDX_LLM_BASE_URL": "llm_base_url",
        "JDX_LLM_MODEL": "llm_model",
        "JDX_SEARXNG_URL": "searxng_url",
        "JDX_API_TOKEN": "api_token",
        "JDX_GITHUB_TOKEN": "github_token",
    }.items():
        if env in os.environ:
            values[key] = os.environ[env]
    return Settings.model_validate(values)
