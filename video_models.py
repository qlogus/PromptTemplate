from typing import Literal

from pydantic import BaseModel, Field, HttpUrl, field_validator, model_validator


VideoMode = Literal['text', 'image', 'keyframes']
VideoStatus = Literal['pending', 'submitting', 'queued', 'running', 'downloading', 'completed', 'failed', 'unknown', 'download_failed', 'expired', 'cancelled']


class VideoSettings(BaseModel):
    model_config = {'extra': 'forbid'}
    protocol: Literal['agnes_v2'] = 'agnes_v2'
    api_style: Literal['gateway', 'direct'] = 'gateway'
    modes: list[VideoMode] = Field(default_factory=lambda: ['text', 'image', 'keyframes'], min_length=1)
    keyframe_limit: int = Field(default=4, ge=2, le=32)


class VideoInput(BaseModel):
    model_config = {'extra': 'forbid'}
    prompt_id: str
    provider_id: str
    prompt: str = Field(min_length=1)
    mode: VideoMode = 'text'
    references: list[str] = Field(default_factory=list, max_length=32)
    width: int = Field(default=1152, ge=64, le=7680, multiple_of=8)
    height: int = Field(default=768, ge=64, le=7680, multiple_of=8)
    num_frames: int = Field(default=121, ge=9, le=441)
    frame_rate: int = Field(default=24, ge=1, le=60)
    seed: int | None = None
    num_inference_steps: int | None = Field(default=None, ge=1, le=200)
    negative_prompt: str = ''

    @field_validator('references')
    @classmethod
    def public_urls(cls, values: list[str]) -> list[str]:
        return [str(HttpUrl(value)) for value in values]

    @field_validator('prompt')
    @classmethod
    def nonempty_prompt(cls, value: str) -> str:
        if not value.strip():
            raise ValueError('请填写 Prompt')
        return value.strip()

    @model_validator(mode='after')
    def validate_mode(self):
        if self.num_frames % 8 != 1:
            raise ValueError('帧数必须满足 8n+1')
        if (self.mode == 'text' and self.references) or (self.mode == 'image' and len(self.references) != 1) or (self.mode == 'keyframes' and len(self.references) < 2):
            raise ValueError('文生视频不引用图片；单图模式需要一张；关键帧模式至少两张')
        return self


class VideoExampleInput(BaseModel):
    title: str = '视频 Example'
    generator_model: str | None = None
    rating: int = Field(default=0, ge=0, le=5)
    notes: str = ''
    compress: bool = False


class RecoverVideoInput(BaseModel):
    remote_id: str = Field(min_length=1, max_length=128, pattern=r'^[A-Za-z0-9_-]+$')
