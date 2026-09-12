from pydantic import BaseModel, ConfigDict


class UserConfiguration(BaseModel):
    model_config = ConfigDict(slots=True)  # pyright: ignore

    token: str


class VoiceConfiguration(BaseModel):
    model_config = ConfigDict(slots=True)  # pyright: ignore

    channel_id: str
    guild_id: str | None = None
    preferred_region: str | None = None


class Configuration(BaseModel):
    model_config = ConfigDict(slots=True)  # pyright: ignore

    user: UserConfiguration
    voice: VoiceConfiguration
