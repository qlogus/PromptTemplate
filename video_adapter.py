from dataclasses import dataclass
from urllib.parse import quote

import httpx

from video_models import VideoInput, VideoSettings


@dataclass
class RemoteVideo:
    status: str
    remote_id: str = ''
    progress: int | None = None
    url: str = ''
    error: str = ''
    metadata: dict | None = None


class AgnesVideoAdapter:
    def __init__(self, provider: dict, client: httpx.Client):
        self.provider = provider
        self.client = client
        self.settings = VideoSettings.model_validate(provider['video_settings'])
        base = provider['base_url'].rstrip('/')
        self.base = base if base.endswith('/v1') else base + '/v1'
        self.headers = {'Authorization':'Bearer ' + provider['api_key']} if provider['api_key'] else {}

    def submit(self, payload: VideoInput) -> RemoteVideo:
        body = payload.model_dump(exclude={'prompt_id','provider_id','references','mode'}, exclude_none=True)
        body['model'] = self.provider['model']
        if payload.mode == 'image':
            body['image'] = payload.references[0]
        if payload.mode == 'keyframes':
            body['extra_body'] = {'image':payload.references,'mode':'keyframes'}
        response = self.client.post(self.base + '/videos', headers=self.headers, json=body, timeout=300)
        response.raise_for_status()
        raw = response.json()
        remote_id = raw.get('video_id') if self.settings.api_style == 'direct' else raw.get('id') or raw.get('task_id')
        if not isinstance(remote_id, str) or not remote_id:
            raise ValueError('提交响应缺少远程任务 ID，请在上游确认任务状态')
        result = self.parse(raw)
        result.remote_id = remote_id
        return result

    def poll(self, remote_id: str) -> RemoteVideo:
        if self.settings.api_style == 'direct':
            response = self.client.get(self.base.removesuffix('/v1') + '/agnesapi', params={'video_id':remote_id,'model_name':self.provider['model']}, headers=self.headers, timeout=30)
        else:
            response = self.client.get(self.base + '/videos/' + quote(remote_id, safe=''), headers=self.headers, timeout=30)
        response.raise_for_status()
        return self.parse(response.json())

    def content(self, remote_id: str, url: str) -> tuple[str,dict]:
        if url:
            return url, {}
        if self.settings.api_style == 'gateway':
            return self.base + '/videos/' + quote(remote_id, safe='') + '/content', self.headers
        raise ValueError('任务已完成，但响应缺少视频下载 URL')

    def parse(self, raw: dict) -> RemoteVideo:
        statuses = {'queued':'queued','pending':'queued','in_progress':'running','running':'running','completed':'downloading','succeeded':'downloading','failed':'failed','cancelled':'cancelled','expired':'expired'}
        status = statuses.get(raw.get('status'))
        if status is None:
            raise ValueError('无法识别上游视频任务状态：' + str(raw.get('status')))
        progress = raw.get('progress')
        error = raw.get('error')
        if isinstance(error, dict):
            error = error.get('message') or str(error)
        return RemoteVideo(status=status, progress=min(100,max(0,progress)) if isinstance(progress,(int,float)) else None,
                           url=raw.get('url') or '', error=str(error or ''), metadata={key:raw[key] for key in ('model','seconds','size','seed') if key in raw})


VIDEO_ADAPTERS = {'agnes_v2':AgnesVideoAdapter}
