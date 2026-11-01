from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', extra='ignore')
    bot_token: SecretStr
    database_url: str
    admin_ids: list[int] = []
    max_age_hours: int = Field(0, ge=0)
