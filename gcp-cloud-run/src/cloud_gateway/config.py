from pathlib import Path
from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "gcp-cloud-run/.env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Required OAuth Secrets (Fail fast if missing or empty)
    google_client_id: str = Field(..., min_length=1, alias="GOOGLE_CLIENT_ID")
    google_client_secret: SecretStr = Field(..., alias="GOOGLE_CLIENT_SECRET")
    admin_passphrase: SecretStr = Field(..., alias="ADMIN_PASSPHRASE")
    oauth_signing_secret: SecretStr = Field(..., alias="OAUTH_SIGNING_SECRET")

    # Webhook Settings
    webhook_api_key: SecretStr | None = Field(default=None, alias="WEBHOOK_API_KEY")

    # Pub/Sub Settings
    gcp_project_id: str = Field(default="plex-cast-bridge-home", alias="GCP_PROJECT_ID")
    gcp_pubsub_topic_id: str = Field(default="plex-commands", alias="GCP_PUBSUB_TOPIC_ID")
    mock_pubsub: bool = Field(default=False, alias="MOCK_PUBSUB")

    # Default Target Speaker
    default_speaker_name: str | None = Field(default=None, alias="DEFAULT_SPEAKER_NAME")

    # Catalog & Tokens
    catalog_path: str = Field(default="catalog.json", alias="CATALOG_PATH")
    token_expire_days: int = Field(default=30, alias="TOKEN_EXPIRE_DAYS")
    auth_code_expire_minutes: int = Field(default=10, alias="AUTH_CODE_EXPIRE_MINUTES")

    @field_validator("google_client_secret", "admin_passphrase", "oauth_signing_secret")
    @classmethod
    def validate_non_empty_secret(cls, v: SecretStr) -> SecretStr:
        if not v or not v.get_secret_value().strip():
            raise ValueError("Secret value must not be empty")
        return v

    def get_catalog_file(self) -> Path:
        p = Path(self.catalog_path)
        if not p.is_absolute() and not p.exists():
            # Check relative to service directory or repository root
            service_root = Path(__file__).resolve().parent.parent.parent
            for candidate in (service_root / "catalog.json", service_root.parent / "catalog.json"):
                if candidate.exists():
                    return candidate
        return p

    def get_webhook_key(self) -> str:
        if self.webhook_api_key and self.webhook_api_key.get_secret_value():
            return self.webhook_api_key.get_secret_value()
        return self.admin_passphrase.get_secret_value()
