import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
import app


class GenerationPresetTests(unittest.TestCase):
    def test_crud_and_validation(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(app, 'DB_PATH', Path(directory) / 'test.sqlite'):
            app.init_db()
            client = TestClient(app.app)
            created = client.post('/api/generation-presets', json={'title':'  双图姿势  ', 'content':'图1保留角色，图2提供姿势。'})
            self.assertEqual(created.status_code, 200)
            item = created.json()
            self.assertEqual(item['title'], '双图姿势')
            self.assertEqual(client.put('/api/generation-presets/'+item['id'], json={'title':'双图', 'content':'保持角色外观，只迁移姿势。'}).status_code, 200)
            self.assertEqual(client.get('/api/generation-presets').json()[0]['title'], '双图')
            self.assertEqual(client.delete('/api/generation-presets/'+item['id']).status_code, 200)
            self.assertEqual(client.get('/api/generation-presets').json(), [])
            self.assertEqual(client.post('/api/generation-presets', json={'title':' ', 'content':'x'}).status_code, 422)
            self.assertEqual(client.post('/api/generation-presets', json={'title':'x', 'content':' '}).status_code, 422)
