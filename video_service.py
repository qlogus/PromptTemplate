import json
import logging
import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
from fastapi import HTTPException

from example_store import ExampleInput, ExampleStore, MediaInput
from media_store import MediaStore
from video_adapter import VIDEO_ADAPTERS
from video_models import VideoExampleInput, VideoInput, VideoSettings


class VideoService:
    def __init__(self, connect, storage: Path, cache: Path, ffmpeg: str = 'ffmpeg', transport=None):
        self.connect = connect
        self.storage = storage
        self.cache = cache
        self.media = MediaStore(storage, ffmpeg)
        self.examples = ExampleStore(connect, storage)
        self.transport = transport
        self.logger = logging.getLogger('prompt_template')
        self.locks = {}
        self.guard = threading.Lock()
        self.stop_event = threading.Event()
        self.thread = None

    @staticmethod
    def initialize(db) -> None:
        db.execute('''CREATE TABLE IF NOT EXISTS video_jobs (
            id TEXT PRIMARY KEY,
            prompt_id TEXT NOT NULL REFERENCES prompts(id) ON DELETE CASCADE,
            provider_id TEXT NOT NULL REFERENCES providers(id),
            provider_json TEXT NOT NULL,
            request_json TEXT NOT NULL,
            remote_id TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL,
            progress INTEGER,
            error TEXT NOT NULL DEFAULT '',
            result_url TEXT NOT NULL DEFAULT '',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            cache_path TEXT NOT NULL DEFAULT '',
            cache_expires_at REAL,
            next_poll_at REAL NOT NULL DEFAULT 0,
            saved_example_id TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )''')
        db.execute('CREATE INDEX IF NOT EXISTS video_jobs_due ON video_jobs(status,next_poll_at)')
        db.execute('CREATE INDEX IF NOT EXISTS video_jobs_prompt ON video_jobs(prompt_id,created_at)')

    def lock(self, job_id: str):
        with self.guard:
            return self.locks.setdefault(job_id, threading.RLock())

    def create(self, payload: VideoInput) -> dict:
        with self.connect() as db:
            if not db.execute('SELECT 1 FROM prompts WHERE id=?', (payload.prompt_id,)).fetchone():
                raise HTTPException(404, 'Prompt 不存在')
            row = db.execute("SELECT * FROM providers WHERE id=? AND provider_kind='video'", (payload.provider_id,)).fetchone()
            if not row:
                raise HTTPException(400, '请选择视频 Provider')
            provider = dict(row)
            settings = VideoSettings.model_validate_json(provider['video_settings'])
            if payload.mode not in settings.modes:
                raise HTTPException(400, '当前 Provider 不支持该生成模式')
            if len(payload.references) > settings.keyframe_limit:
                raise HTTPException(400, '参考图数量超过 Provider 的关键帧上限')
            snapshot = {key:provider[key] for key in ('name','model','base_url','verify_tls','api_key')}
            snapshot['video_settings'] = settings.model_dump()
            job_id = str(uuid.uuid4())
            now = time.time()
            db.execute('INSERT INTO video_jobs(id,prompt_id,provider_id,provider_json,request_json,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)',
                       (job_id,payload.prompt_id,payload.provider_id,json.dumps(snapshot),payload.model_dump_json(),'pending',now,now))
        return self.get(job_id)

    def get(self, job_id: str) -> dict:
        with self.connect() as db:
            row = db.execute('SELECT * FROM video_jobs WHERE id=?', (job_id,)).fetchone()
            if not row:
                raise HTTPException(404, '视频任务不存在')
        job = dict(row)
        job['provider'] = json.loads(job.pop('provider_json'))
        job['provider'].pop('api_key',None)
        job['request'] = json.loads(job.pop('request_json'))
        job['metadata'] = json.loads(job.pop('metadata_json'))
        job['preview_url'] = '/api/video-jobs/' + job_id + '/content' if job['status'] == 'completed' else ''
        return job

    def list(self, prompt_id: str) -> list[dict]:
        with self.connect() as db:
            ids = [row[0] for row in db.execute('SELECT id FROM video_jobs WHERE prompt_id=? ORDER BY created_at DESC LIMIT 50', (prompt_id,))]
        return [self.get(job_id) for job_id in ids]

    def update(self, job_id: str, **values) -> None:
        values['updated_at'] = time.time()
        with self.connect() as db:
            db.execute('UPDATE video_jobs SET ' + ','.join(key + '=?' for key in values) + ' WHERE id=?', (*values.values(),job_id))

    def advance(self, job_id: str) -> None:
        with self.lock(job_id):
            job = self.get(job_id)
            if job['status'] not in ('pending','queued','running','downloading'):
                return
            provider = job['provider']
            with self.connect() as db:
                private_provider = json.loads(db.execute('SELECT provider_json FROM video_jobs WHERE id=?',(job_id,)).fetchone()[0])
            provider['api_key'] = private_provider['api_key']
            submitting = job['status'] == 'pending'
            downloading = job['status'] == 'downloading'
            try:
                with httpx.Client(verify=bool(provider['verify_tls']), timeout=300, transport=self.transport) as client:
                    adapter = VIDEO_ADAPTERS[provider['video_settings']['protocol']](provider,client)
                    if submitting:
                        self.update(job_id, status='submitting')
                        remote = adapter.submit(VideoInput.model_validate(job['request']))
                        self.update(job_id,remote_id=remote.remote_id,status=remote.status,progress=remote.progress,
                                    result_url=remote.url,metadata_json=json.dumps(remote.metadata),error=remote.error,next_poll_at=time.time()+10)
                        self.logger.info('video submitted job=%s provider=%s model=%s remote_id=%s mode=%s frames=%s fps=%s requested_size=%sx%s',
                                         job_id,provider['name'],provider['model'],remote.remote_id,job['request']['mode'],job['request']['num_frames'],job['request']['frame_rate'],job['request']['width'],job['request']['height'])
                        return
                    if not downloading:
                        remote = adapter.poll(job['remote_id'])
                        self.update(job_id,status=remote.status,progress=remote.progress,result_url=remote.url,
                                    metadata_json=json.dumps(remote.metadata),error=remote.error,next_poll_at=time.time()+10)
                        self.logger.info('video status job=%s remote=%s status=%s progress=%s metadata=%s',job_id,job['remote_id'],remote.status,remote.progress,remote.metadata)
                        if remote.status != 'downloading':
                            return
                        job = self.get(job_id)
                        downloading = True
                    url, headers = adapter.content(job['remote_id'],job['result_url'])
                    target = self.cache / job_id / 'result.mp4'
                    self.media.download(client,url,target,headers)
                    metadata = job['metadata'] | {'actual':self.media.probe(target)}
                    self.update(job_id,status='completed',progress=100,error='',cache_path=str(target),cache_expires_at=time.time()+86400,metadata_json=json.dumps(metadata))
                    self.logger.info('video cached job=%s bytes=%s actual=%s',job_id,target.stat().st_size,metadata['actual'])
            except (httpx.HTTPError, ValueError, OSError) as error:
                if submitting:
                    definitive = isinstance(error,httpx.HTTPStatusError) and 400 <= error.response.status_code < 500 and error.response.status_code != 408
                    status = 'failed' if definitive else 'unknown'
                else:
                    status = 'download_failed' if downloading else job['status']
                detail = str(error)
                if isinstance(error,httpx.HTTPStatusError):
                    detail = 'HTTP ' + str(error.response.status_code) + ' ' + error.response.text[:600]
                self.update(job_id,status=status,error=detail,next_poll_at=time.time()+30)
                self.logger.exception('video request failed job=%s status=%s',job_id,status)

    def refresh(self, job_id: str) -> dict:
        with self.lock(job_id):
            job = self.get(job_id)
            if job['status'] in ('download_failed','expired') and job['remote_id']:
                self.update(job_id,status='downloading',next_poll_at=0,error='')
            elif job['status'] in ('queued','running'):
                self.update(job_id,next_poll_at=0,error='')
        return self.get(job_id)

    def recover(self, job_id: str, remote_id: str) -> dict:
        with self.lock(job_id):
            if self.get(job_id)['status'] != 'unknown':
                raise HTTPException(409, '仅提交状态未知的任务可以补填远程任务 ID')
            self.update(job_id,remote_id=remote_id,status='queued',next_poll_at=0,error='')
        return self.get(job_id)

    def content_path(self, job_id: str) -> Path:
        job = self.get(job_id)
        path = self.cache / job_id / 'result.mp4'
        if job['status'] != 'completed' or not path.is_file():
            raise HTTPException(404, '临时视频不可用，请重新获取结果')
        return path

    def save_example(self, job_id: str, payload: VideoExampleInput) -> dict:
        with self.lock(job_id):
            job = self.get(job_id)
            if job['saved_example_id']:
                with self.connect() as db:
                    return self.examples.get(db,job['saved_example_id'])
            source = self.content_path(job_id)
            example_id = str(uuid.uuid4())
            try:
                base = job['provider']['base_url'].rstrip('/')
                base = base if base.endswith('/v1') else base + '/v1'
                original_path = job['result_url'] or base + '/videos/' + job['remote_id'] + '/content'
                asset = self.media.copy_video(source,job['prompt_id'],example_id,original_path,payload.compress)
                assets = {asset['source']:asset}
                references = []
                with httpx.Client(timeout=60,transport=self.transport) as client:
                    for index, url in enumerate(job['request']['references']):
                        relative = Path(job['prompt_id']) / example_id / ('reference-' + str(index + 1) + '.image')
                        self.media.download(client,url,self.storage/relative,kind='image')
                        reference = {'kind':'image','source':'/media/'+relative.as_posix(),'original_path':url,'relative_path':relative.as_posix(),'role':'keyframe' if job['request']['mode'] == 'keyframes' else 'first_frame','label':'图'+str(index+1)}
                        assets[reference['source']] = reference
                        references.append(MediaInput.model_validate(reference))
                request = job['request']
                example = ExampleInput(title=payload.title,input_text=request['prompt'],references=references,results=[MediaInput.model_validate(asset)],
                                       generator_model=payload.generator_model if payload.generator_model is not None else job['provider']['model'],rating=payload.rating,notes=payload.notes,
                                       seed=str(request['seed']) if request['seed'] is not None else '',
                                       generation_params=json.dumps({'requested':{key:value for key,value in request.items() if key not in ('prompt_id','provider_id','prompt','references')},'returned':job['metadata'],'provider':job['provider']['name'],'remote_id':job['remote_id']},ensure_ascii=False))
                with self.connect() as db:
                    result = self.examples.write(db,job['prompt_id'],example,assets=assets,new_id=example_id)
                    db.execute('UPDATE video_jobs SET saved_example_id=? WHERE id=?',(result['id'],job_id))
                return result
            except Exception as error:
                shutil.rmtree(self.storage / job['prompt_id'] / example_id,ignore_errors=True)
                if isinstance(error,HTTPException):
                    raise
                self.logger.exception('video example save failed job=%s',job_id)
                raise HTTPException(502,'保存视频或参考图失败：'+str(error)) from error

    def delete(self, job_id: str) -> dict:
        with self.lock(job_id):
            job = self.get(job_id)
            if job['status'] in ('pending','submitting','queued','running','downloading'):
                raise HTTPException(409,'任务仍在进行，完成后可以清除本地记录')
            with self.connect() as db:
                db.execute('DELETE FROM video_jobs WHERE id=?',(job_id,))
            shutil.rmtree(self.cache/job_id,ignore_errors=True)
        return {'ok':True}

    def recover_startup(self) -> None:
        with self.connect() as db:
            db.execute("UPDATE video_jobs SET status='unknown',error='应用在提交期间退出，请确认上游任务并补填任务 ID' WHERE status='submitting'")
            db.execute("UPDATE video_jobs SET next_poll_at=0 WHERE status IN ('pending','queued','running','downloading')")
        self.cleanup()

    def cleanup(self) -> None:
        with self.connect() as db:
            ids = [row[0] for row in db.execute("SELECT id FROM video_jobs WHERE cache_expires_at IS NOT NULL AND cache_expires_at<? AND status='completed'",(time.time(),))]
            known = {row[0] for row in db.execute('SELECT id FROM video_jobs')}
        for job_id in ids:
            with self.lock(job_id):
                job = self.get(job_id)
                if job['status'] == 'completed' and job['cache_expires_at'] < time.time():
                    shutil.rmtree(self.cache/job_id,ignore_errors=True)
                    self.update(job_id,status='expired',cache_path='')
        if self.cache.exists():
            for path in self.cache.iterdir():
                if path.is_dir() and path.name not in known:
                    shutil.rmtree(path)

    def start(self) -> None:
        if self.thread:
            return
        self.recover_startup()
        self.stop_event.clear()
        self.thread = threading.Thread(target=self.run,daemon=True,name='video-jobs')
        self.thread.start()

    def run(self) -> None:
        futures = {}
        last_cleanup = 0
        with ThreadPoolExecutor(max_workers=4,thread_name_prefix='video-request') as executor:
            while not self.stop_event.wait(2):
                try:
                    for job_id, future in list(futures.items()):
                        if future.done():
                            try:
                                future.result()
                            except Exception:
                                self.logger.exception('video worker failed job=%s',job_id)
                            del futures[job_id]
                    with self.connect() as db:
                        ids = [row[0] for row in db.execute("SELECT id FROM video_jobs WHERE status IN ('pending','queued','running','downloading') AND next_poll_at<=? ORDER BY created_at LIMIT 20",(time.time(),))]
                    for job_id in ids:
                        if job_id not in futures and len(futures)<4:
                            futures[job_id] = executor.submit(self.advance,job_id)
                    if time.time()-last_cleanup>60:
                        self.cleanup()
                        last_cleanup=time.time()
                except Exception:
                    self.logger.exception('video scheduler failed')

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=5)
            self.thread = None
