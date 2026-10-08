"""Runtime configuration, read once from environment variables."""
import os


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


class Settings:
    # SQLite by default so the project runs with zero external services.
    database_url: str = os.getenv("DATABASE_URL", "sqlite:///./data/app.db")

    # Upload guard rails.
    max_upload_bytes: int = _int_env("MAX_UPLOAD_MB", 50) * 1024 * 1024
    # Protects against zip bombs: total declared size of all archive members.
    max_uncompressed_bytes: int = _int_env("MAX_UNCOMPRESSED_MB", 200) * 1024 * 1024
    max_features: int = _int_env("MAX_FEATURES", 100_000)


settings = Settings()
