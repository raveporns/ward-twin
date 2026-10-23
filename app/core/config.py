from typing import Literal
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_env: Literal["local", "production"] = "local"

    jwt_issuer: str = "ward-twin-local"
    jwt_audience: str = "ward-twin-api"
    jwt_secret: str

    gateway_api_key: str

    # Gateway configuration
    gateway_producer_id: str = "local-gateway"
    gateway_allowed_devices: str = "MON-01"

    database_url: str

    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
    )


settings = Settings()