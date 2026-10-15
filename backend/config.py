from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    dgis_api_key: str = ""
    dgis_map_key: str = ""
    map_style_id: str = ""
    demo_mode: bool = False
    default_city: str = "Алматы"
    default_lat: float = 43.2380
    default_lon: float = 76.9286
    groq_api_key: str = ""
    dgis_region_id: str = ""

    class Config:
        env_file = ".env"


settings = Settings()
