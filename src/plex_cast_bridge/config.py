from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")
    plex_url: str = "http://plex.local:32400"
    # Optional when using Settings → Connect with Plex (JWT store under DATA_DIR).
    plex_token: SecretStr | None = None
    plex_music_section: str = "Music"
    default_speaker_name: str = ""
    default_speaker_host: str = ""
    default_cast_volume: float = 0.40
    max_cast_volume: float = 0.85
    night_cast_volume: float | None = None
    gemini_api_key: SecretStr = SecretStr("")
    local_api_key: SecretStr | None = None
    # Legacy settings are accepted but ignored; access is controlled at the network layer.
    allow_unauthenticated_local_api: bool = False
    gcp_project_id: str = ""
    gcp_pubsub_subscription_id: str = ""
    enable_pubsub_listener: bool = False
    cloud_gateway_url: str = ""
    catalog_write_key: SecretStr | None = None
    # Optional background Cast discovery. 0 disables; default 15 minutes.
    speaker_discovery_interval_sec: int = 900
    catalog_reconcile_interval_sec: int = 60
