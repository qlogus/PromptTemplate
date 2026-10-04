import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
import app


class RevisionPresetTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        patcher = patch.object(app, 'DB_PATH', Path(self.directory.name) / 'test.sqlite')
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = self.enterContext(TestClient(app.app))

    def test_crud_persists_independently_of_prompts(self):
        response = self.client.post('/api/revision-presets', json={'title':'  保持侧面  ', 'content':'保持角色侧面，参考图1。'})
        self.assertEqual(response.status_code, 200)
        preset = response.json()
        self.assertEqual(preset['title'], '保持侧面')
        app.init_db()
        self.assertEqual(self.client.get('/api/revision-presets').json(), [preset])
        updated = self.client.put('/api/revision-presets/' + preset['id'], json={'title':'侧面姿势', 'content':'参考 image1，保持侧面。'})
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(self.client.get('/api/revision-presets').json()[0]['content'], '参考 image1，保持侧面。')
        self.assertEqual(self.client.delete('/api/revision-presets/' + preset['id']).status_code, 200)
        self.assertEqual(self.client.get('/api/revision-presets').json(), [])

    def test_invalid_and_missing_presets(self):
        for payload in ({'title':' ', 'content':'text'}, {'title':'title', 'content':'\n '}, {'title':'x' * 121, 'content':'text'}):
            self.assertEqual(self.client.post('/api/revision-presets', json=payload).status_code, 422)
        self.assertEqual(self.client.put('/api/revision-presets/missing', json={'title':'title', 'content':'text'}).status_code, 404)
        self.assertEqual(self.client.delete('/api/revision-presets/missing').status_code, 404)
