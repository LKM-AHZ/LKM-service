from pydantic import BaseModel


class Settings(BaseModel):
    app_name: str = "LKM-API"
    app_version: str = "0.0.1"
    api_prefix: str = "/api/v1"
    database_url: str = "sqlite:///./lkm.db"


settings = Settings()
