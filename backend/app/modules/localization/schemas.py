"""Public contract for POI on-demand localization."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class OnDemandRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    poi_id: str = Field(min_length=1, max_length=200)
    lang: Literal["vi", "en", "zh", "ja", "ko"]


class OnDemandResponse(BaseModel):
    poi_id: str
    lang: str
    title: str
    translated_text: str
    audio_url: str
    audio_status: Literal["completed"] = "completed"
    is_fallback: bool = False
    cache_hit: bool
