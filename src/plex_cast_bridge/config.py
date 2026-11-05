from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")
    plex_url: str = "http://192.168.88.166:32400"
    plex_token: SecretStr
    plex_music_section: str = "Music"
    default_speaker_name: str = ""
    default_speaker_host: str = ""
    default_cast_volume: float = 0.20
    gemini_api_key: SecretStr = SecretStr("")
    local_api_key: SecretStr | None = None
    gcp_project_id: str = ""
    gcp_pubsub_subscription_id: str = ""
    enable_pubsub_listener: bool = False
