"""Application configuration.

Loads settings from environment variables (via a local `.env` file when
present) using `pydantic-settings`. All values have safe defaults so the
backend can start out of the box, but they should be overridden for real
deployments.

Secrets (API keys, OAuth tokens, etc.) are **never** stored in this file.
They are read from the environment at runtime only.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """SiloVault backend configuration."""

    app_name: str = "SiloVault"
    app_version: str = "0.1.0-skeleton"

    # Local server binding. Defaults to loopback for privacy.
    host: str = "127.0.0.1"
    port: int = 8000
    debug: bool = False

    # Local database path (excluded from Git via .gitignore).
    database_path: str = "silo_vault.db"

    # API version prefix.
    api_v1_prefix: str = "/api/v1"

    # Placeholder for future cloud / AI configuration.
    # These are read from the environment but never committed as defaults.
    google_drive_client_id: str | None = None
    google_drive_client_secret: str | None = None
    supabase_url: str | None = None
    supabase_key: str | None = None

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def database_url(self) -> str:
        """Return the SQLAlchemy-compatible database URL."""
        return f"sqlite:///{self.database_path}"


settings = Settings()