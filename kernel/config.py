"""Application configuration via pydantic-settings (environment variables)."""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Database
    mongodb_uri: str = "mongodb://localhost:27017"
    database_name: str = "indemn_os"
    # Connection pool sizes per service type
    # API: 50, Queue Processor: 10, Temporal Worker: 30
    mongodb_max_pool_size: int = 50

    # Auth
    jwt_signing_key: str = "dev-signing-key-not-for-production"
    jwt_access_token_expire_minutes: int = 15
    jwt_algorithm: str = "HS256"

    # Temporal
    temporal_address: str = "localhost:7233"
    temporal_namespace: str = "default"
    temporal_api_key: str = ""

    # AWS
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    aws_region: str = "us-east-1"

    # OTEL
    otel_exporter_endpoint: str = ""
    otel_service_name: str = "indemn-os"

    # API
    api_url: str = "http://localhost:8000"

    # Environment
    environment: str = "local"  # local, dev, prod

    model_config = {"env_file": ".env"}


settings = Settings()
