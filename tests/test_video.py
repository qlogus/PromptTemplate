import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
import app
from video_models import VideoSettings, VideoInput, VideoExampleInput
from video_service import VideoService


class VideoTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(app, 'DB_PATH', self.root / 'test.sqlite'))
        app.init_db()
        self.prompt = app.create_prompt(app.PromptInput(title='动作', category='action'))
        self.provider = app.create_provider(app.ProviderInput(name='video', model='agnes-video-v2.0', base_url='https://upstream.test/v1', api_key='test-key', provider_kind='video', video_settings=VideoSettings()))
        self.calls = []
        def upstream(request):
            self.calls.append(request)
            if request.method == 'POST':
                return httpx.Response(200, json={'id':'task_123', 'video_id':'video_123', 'status':'queued'})
            if request.url.path.endswith('/content'):
                return httpx.Response(200, content=b'video-content', headers={'content-type':'video/mp4'})
            return httpx.Response(200, json={'status':'completed', 'progress':100, 'seconds':'5.04', 'size':'1152x768'})
        self.service = VideoService(app.connect, self.root / 'storage', self.root / 'cache', transport=httpx.MockTransport(upstream))

    def request(self, **values):
        return VideoInput(prompt_id=self.prompt['id'], provider_id=self.provider['id'], prompt='角色挥剑', **values)

    def test_gateway_lifecycle_explicit_save_and_media(self):
        job = self.service.create(self.request())
        self.service.advance(job['id'])
        self.assertEqual(self.service.get(job['id'])['remote_id'], 'task_123')
        self.service.advance(job['id'])
        job = self.service.get(job['id'])
        self.assertEqual(job['status'], 'completed')
        self.assertTrue((self.root / 'cache' / job['id'] / 'result.mp4').is_file())
        self.assertEqual(app.get_prompt(self.prompt['id'])['examples'], [])
        result = self.service.save_example(job['id'], VideoExampleInput(title='测试', rating=4, generator_model='my workflow'))
        example = app.get_prompt(self.prompt['id'])['examples'][0]
        self.assertEqual(example['results'][0]['kind'], 'video')
        self.assertEqual(example['generator_model'], 'my workflow')
        self.assertEqual(example['input_text'], '角色挥剑')
        self.assertTrue((self.root / 'storage' / example['results'][0]['relative_path']).is_file())
        self.assertEqual(self.service.save_example(job['id'], VideoExampleInput())['id'], result['id'])
        self.assertEqual(len(app.get_prompt(self.prompt['id'])['examples']), 1)
        self.assertEqual([r.url.path for r in self.calls], ['/v1/videos','/v1/videos/task_123','/v1/videos/task_123/content'])
        self.service.examples.delete(result['id'])
        self.assertFalse((self.root / 'storage' / example['results'][0]['relative_path']).exists())

    def test_keyframes_parameters_and_snapshot(self):
        request = self.request(mode='keyframes', references=['https://ref.test/1.png','https://ref.test/2.png'], num_frames=161, frame_rate=24, width=1280, height=720, seed=42, negative_prompt='闪烁')
        job = self.service.create(request)
        app.update_provider(self.provider['id'], app.ProviderInput(name='changed', model='different', base_url='https://other.test/v1', api_key='test-key', provider_kind='video'))
        self.service.advance(job['id'])
        body = json.loads(self.calls[0].content)
        self.assertEqual(body['model'], 'agnes-video-v2.0')
        self.assertEqual(body['extra_body'], {'image':request.references, 'mode':'keyframes'})
        self.assertEqual(body['num_frames'], 161)
        self.assertEqual(body['seed'], 42)

    def test_direct_uses_video_id_and_url_without_leaking_key(self):
        settings = app.ProviderInput(name='direct', model='agnes-video-v2.0', base_url='https://upstream.test', api_key='test-key', provider_kind='video', video_settings=VideoSettings(api_style='direct'))
        app.update_provider(self.provider['id'], settings)
        def upstream(request):
            self.calls.append(request)
            if request.method == 'POST':
                return httpx.Response(200, json={'id':'task_123','video_id':'video_123','status':'queued'})
            if request.url.path == '/agnesapi':
                self.assertEqual(request.url.params['video_id'], 'video_123')
                return httpx.Response(200, json={'status':'completed','url':'https://cdn.test/video.mp4'})
            self.assertNotIn('authorization', request.headers)
            return httpx.Response(200, content=b'video', headers={'content-type':'video/mp4'})
        self.service.transport = httpx.MockTransport(upstream)
        job = self.service.create(self.request())
        self.service.advance(job['id'])
        self.service.advance(job['id'])
        self.assertEqual(self.service.get(job['id'])['status'], 'completed')
        self.assertNotIn('test-key', json.dumps(self.service.get(job['id'])))

    def test_timeout_is_unknown_and_never_resubmitted(self):
        def upstream(request):
            self.calls.append(request)
            raise httpx.ReadTimeout('timeout', request=request)
        self.service.transport = httpx.MockTransport(upstream)
        job = self.service.create(self.request())
        self.service.advance(job['id'])
        self.service.advance(job['id'])
        self.assertEqual(self.service.get(job['id'])['status'], 'unknown')
        self.assertEqual(len(self.calls), 1)
        self.service.recover(job['id'], 'task_456')
        self.assertEqual(self.service.get(job['id'])['remote_id'], 'task_456')

    def test_validation_and_limits(self):
        for values in ({'num_frames':120}, {'mode':'image'}, {'mode':'text','references':['https://ref.test/1']}, {'references':['data:image/png;base64,YQ=='],'mode':'image'}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.request(**values)
        with self.assertRaises(ValueError):
            VideoSettings(keyframe_limit=1)

    def test_download_failure_retries_download_without_generation(self):
        job = self.service.create(self.request())
        self.service.advance(job['id'])
        def upstream(request):
            self.calls.append(request)
            if request.url.path.endswith('/content'):
                return httpx.Response(503, text='temporarily unavailable')
            return httpx.Response(200, json={'status':'completed'})
        self.service.transport = httpx.MockTransport(upstream)
        self.service.advance(job['id'])
        self.assertEqual(self.service.get(job['id'])['status'], 'download_failed')
        self.service.refresh(job['id'])
        self.service.advance(job['id'])
        self.assertEqual(sum(r.method == 'POST' for r in self.calls), 1)

    def test_restart_recovers_polling_but_not_ambiguous_submission(self):
        job = self.service.create(self.request())
        with app.connect() as db:
            db.execute("UPDATE video_jobs SET status='submitting' WHERE id=?", (job['id'],))
        self.service.recover_startup()
        self.assertEqual(self.service.get(job['id'])['status'], 'unknown')

    def test_cleanup_expires_only_old_finished_cache(self):
        job = self.service.create(self.request())
        self.service.advance(job['id'])
        self.service.advance(job['id'])
        with app.connect() as db:
            db.execute('UPDATE video_jobs SET cache_expires_at=0 WHERE id=?', (job['id'],))
        self.service.cleanup()
        self.assertEqual(self.service.get(job['id'])['status'], 'expired')
        self.assertFalse((self.root / 'cache' / job['id']).exists())
