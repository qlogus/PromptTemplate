import json
import tempfile
import unittest
from email.parser import BytesParser
from email.policy import default
from pathlib import Path
from unittest.mock import patch

import httpx
import app


class MultiReferenceTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.object(app, 'DB_PATH', Path(directory) / 'test.sqlite'))
        app.init_db()

    def test_mixed_references_preserve_order_and_do_not_persist(self):
        provider = app.create_provider(app.ProviderInput(name='images', base_url='https://provider.test/v1', model='test', provider_kind='image', reference_limit=3, reference_field='image[]', reference_format='array'))
        settings = app.list_providers()[0]
        self.assertEqual(settings['reference_limit'], 3)
        calls = []
        def upstream(request):
            calls.append(request.method)
            if request.method == 'GET':
                self.assertNotIn('authorization', request.headers)
                return httpx.Response(200, content=b'url-image', headers={'content-type':'image/png'})
            message = BytesParser(policy=default).parsebytes(b'Content-Type: ' + request.headers['content-type'].encode() + b'\r\n\r\n' + request.content)
            images = [part for part in message.iter_parts() if part.get_filename()]
            self.assertEqual([part.get_param('name', header='content-disposition') for part in images], ['image[]', 'image[]'])
            self.assertEqual([part.get_payload(decode=True) for part in images], [b'url-image', b'local-image'])
            return httpx.Response(200, json={'data':[]})
        with patch.object(app.httpx, 'Client', return_value=httpx.Client(transport=httpx.MockTransport(upstream))):
            app.generate_image(app.GenerateImageInput(prompt_id='draft', provider_id=provider['id'], prompt='pose', references=['https://ref.test/a.png','data:image/png;base64,bG9jYWwtaW1hZ2U=']))
        self.assertEqual(calls, ['GET', 'POST'])
        with app.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM examples').fetchone()[0], 0)

    def test_json_array_even_with_one_reference(self):
        provider = app.create_provider(app.ProviderInput(name='url', base_url='https://provider.test/v1', model='test', provider_kind='image', reference_protocol='json_url', reference_limit=4, reference_format='array'))
        for refs in (['https://a.test/1'], ['https://a.test/2','https://a.test/1']):
            def upstream(request):
                self.assertEqual(request.method, 'POST')
                self.assertEqual(json.loads(request.content)['image'], refs)
                return httpx.Response(200, json={'data':[]})
            with patch.object(app.httpx, 'Client', return_value=httpx.Client(transport=httpx.MockTransport(upstream))):
                app.generate_image(app.GenerateImageInput(prompt_id='draft', provider_id=provider['id'], prompt='pose', references=refs))

    def test_limits_and_invalid_configuration(self):
        for limit in (0, 1, 2):
            provider = app.create_provider(app.ProviderInput(name='test', base_url='https://provider.test/v1', model='test', provider_kind='image', reference_limit=limit, reference_format='array' if limit > 1 else 'single'))
            with patch.object(app.httpx, 'Client') as client, self.assertRaises(app.HTTPException):
                app.generate_image(app.GenerateImageInput(prompt_id='draft', provider_id=provider['id'], prompt='pose', references=['https://a.test/1'] * (limit + 1)))
            client.assert_not_called()
        with self.assertRaises(ValueError):
            app.ProviderInput(name='bad', base_url='https://provider.test', model='test', reference_limit=2, reference_format='single')

    def test_bad_reference_stops_generation(self):
        provider = app.create_provider(app.ProviderInput(name='test', base_url='https://provider.test/v1', model='test', provider_kind='image', reference_limit=2, reference_format='array'))
        def upstream(request):
            self.assertEqual(request.method, 'GET')
            return httpx.Response(200, content=b'not an image', headers={'content-type':'text/html'})
        with patch.object(app.httpx, 'Client', return_value=httpx.Client(transport=httpx.MockTransport(upstream))), self.assertRaises(app.HTTPException):
            app.generate_image(app.GenerateImageInput(prompt_id='draft', provider_id=provider['id'], prompt='pose', references=['data:image/png;base64,bG9jYWwtaW1hZ2U=','https://ref.test/invalid']))

    def test_existing_provider_migration_and_update(self):
        with app.connect() as db:
            db.execute('ALTER TABLE providers DROP COLUMN reference_limit')
            db.execute('ALTER TABLE providers DROP COLUMN reference_format')
            db.execute("INSERT INTO providers(id,name,base_url,model,reference_protocol) VALUES('single','single','https://p.test','model','multipart_edit'),('none','none','https://p.test','model','none')")
            db.execute("UPDATE providers SET capabilities='[]'")
        app.init_db()
        with app.connect() as db:
            self.assertEqual(dict(db.execute('SELECT id,reference_limit FROM providers')), {'single':1, 'none':0})
        updated = app.update_provider('single', app.ProviderInput(name='single', base_url='https://p.test', model='model', provider_kind='image', reference_limit=16, reference_format='array', reference_field='image[]'))
        self.assertEqual(updated['reference_limit'], 16)
        app.init_db()
        self.assertEqual(next(item for item in app.list_providers() if item['id'] == 'single')['reference_limit'], 16)
