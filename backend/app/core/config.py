import re
from typing import Self

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from backend.app.integrations.regimex.config import (
    IDENTITY_PATTERN,
    Connection,
    validate_connection,
)


class Settings(BaseSettings):
    database_url: str

    jwt_secret: SecretStr
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 30

    password_pepper: SecretStr

    regimex_url: str | None = None
    regimex_token: SecretStr | None = None
    regimex_source_id: str = "trade"
    regimex_allow_loopback_http: bool = False

    @model_validator(mode="after")
    def validate_regimex(self) -> Self:
        if not re.fullmatch(IDENTITY_PATTERN, self.regimex_source_id):
            raise ValueError("REGIMEX_SOURCE_ID must be a stable ASCII source identifier")
        validate_connection(self.regimex_url, self.regimex_token, self.regimex_allow_loopback_http)
        return self

    def regimex_connection(self) -> Connection | None:
        if self.regimex_url is None or self.regimex_token is None:
            return None
        return Connection(self.regimex_url, self.regimex_token, self.regimex_source_id)

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
    )


settings = Settings()
