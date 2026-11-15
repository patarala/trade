from pydantic import SecretStr

from backend.app.integrations.regimex.config import RegimexSettings


class Settings(RegimexSettings):
    database_url: str

    jwt_secret: SecretStr
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 30

    password_pepper: SecretStr


settings = Settings()
