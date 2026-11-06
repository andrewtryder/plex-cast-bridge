from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")
    plex_url: str = "http://plex.local:32400"
    plex_token: SecretStr
    plex_music_section: str = "Music"
    default_speaker_name: str = ""
    default_speaker_host: str = ""
    default_cast_volume: float = 0.20
    gemini_api_key: SecretStr = SecretStr("")
    local_api_key: SecretStr | None = None
    # Explicit opt-in for trusted LAN / local development without an API key.
    allow_unauthenticated_local_api: bool = False
    gcp_project_id: str = ""
    gcp_pubsub_subscription_id: str = ""
    enable_pubsub_listener: bool = False

    @model_validator(mode="after")
    def require_local_api_key_unless_opted_in(self):
        key = self.local_api_key.get_secret_value() if self.local_api_key else ""
        if not key and not self.allow_unauthenticated_local_api:
            raise ValueError(
                "LOCAL_API_KEY is required unless ALLOW_UNAUTHENTICATED_LOCAL_API=true "
                "(explicit trusted-LAN / development opt-in)."
            )
        return self
