import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
import app


class GenerationTests(unittest.TestCase):
    def test_size_forwarding_for_all_protocols(self):
        for protocol, references in [('none', {}), ('json_url', {'reference_urls':['https://reference.test/a.png']}), ('multipart_edit', {'reference_images':['data:image/png;base64,aW1hZ2U=']})]:
            with self.subTest(protocol=protocol), tempfile.TemporaryDirectory() as directory, patch.object(app, 'DB_PATH', Path(directory) / 'db.sqlite'):
                app.init_db()
                provider = app.create_provider(app.ProviderInput(name='image', base_url='https://provider.test/v1', model='image', provider_kind='image', reference_protocol=protocol))
                def upstream(request):
                    if protocol == 'multipart_edit':
                        self.assertIn(b'name="size"\r\n\r\n1536x1024', request.content)
                    else:
                        self.assertEqual(json.loads(request.content)['size'], '1536x1024')
                    return httpx.Response(200, json={'data': []})
                with patch.object(app.httpx, 'Client', return_value=httpx.Client(transport=httpx.MockTransport(upstream))):
                    app.generate_image(app.GenerateImageInput(prompt_id='draft', provider_id=provider['id'], prompt='pose', size='1536x1024', **references))

    def test_size_validation(self):
        for size in ('16:9', '0x1024', '-1x1024', '1024', 'huge'):
            with self.subTest(size=size), self.assertRaises(ValueError):
                app.GenerateImageInput(prompt_id='draft', provider_id='image', prompt='pose', size=size)
        for size in (None, '', 'auto', '1536x864'):
            app.GenerateImageInput(prompt_id='draft', provider_id='image', prompt='pose', size=size)

    def test_reference_field_cannot_overwrite_generation_parameters(self):
        for field in ('model', 'prompt', 'n', 'size'):
            with self.subTest(field=field), self.assertRaises(ValueError):
                app.ProviderInput(name='image', base_url='https://provider.test/v1', model='image', reference_field=field)

    def test_provider_reference_settings_round_trip(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(app, 'DB_PATH', None):
            app.DB_PATH = Path(directory) / 'db.sqlite'
            app.init_db()
            settings = app.ProviderInput(name='images', base_url='https://provider.test/v1', model='image', provider_kind='image', reference_protocol='json_url', reference_endpoint='/images/generations', reference_field='image_url')
            provider = app.create_provider(settings)
            self.assertEqual(provider['reference_protocol'], 'json_url')
            settings.reference_protocol = 'multipart_edit'
            app.update_provider(provider['id'], settings)
            self.assertEqual(app.list_providers()[0]['reference_protocol'], 'multipart_edit')

    def test_url_protocol_forwards_url_without_downloading(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(app, 'DB_PATH', None):
            app.DB_PATH = Path(directory) / 'db.sqlite'
            app.init_db()
            provider = app.create_provider(app.ProviderInput(name='url', base_url='https://provider.test/v1', model='image', provider_kind='image', reference_protocol='json_url', reference_endpoint='/images/generations', reference_field='image_url'))
            def upstream(request):
                self.assertEqual(request.method, 'POST')
                self.assertEqual(request.url.path, '/v1/images/generations')
                self.assertEqual(json.loads(request.content)['image_url'], 'https://reference.test/a.png')
                return httpx.Response(200, json={'data': [{'url': 'https://result.test/a.png'}]})
            with patch.object(app.httpx, 'Client', return_value=httpx.Client(transport=httpx.MockTransport(upstream))):
                app.generate_image(app.GenerateImageInput(prompt_id='draft', provider_id=provider['id'], prompt='edit', reference_urls=['https://reference.test/a.png']))

    def test_uploaded_image_is_sent_as_file(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(app, 'DB_PATH', None):
            app.DB_PATH = Path(directory) / 'db.sqlite'
            app.init_db()
            provider = app.create_provider(app.ProviderInput(name='upload', base_url='https://provider.test/v1', model='image', provider_kind='image'))
            def upstream(request):
                self.assertEqual(request.method, 'POST')
                self.assertIn(b'image-bytes', request.content)
                return httpx.Response(200, json={'data': [{'b64_json': 'aW1hZ2U='}]})
            with patch.object(app.httpx, 'Client', return_value=httpx.Client(transport=httpx.MockTransport(upstream))):
                app.generate_image(app.GenerateImageInput(prompt_id='draft', provider_id=provider['id'], prompt='edit', reference_images=['data:image/png;base64,aW1hZ2UtYnl0ZXM=']))

    def test_reference_is_sent_as_file_without_persistence(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(app, 'DB_PATH', Path(directory) / 'db.sqlite'):
                app.init_db()
                provider = app.create_provider(app.ProviderInput(name='image', base_url='https://provider.test/v1', model='image-model', provider_kind='image'))
                requests = []
                def upstream(request):
                    requests.append(request)
                    if request.method == 'GET':
                        self.assertNotIn('authorization', request.headers)
                        return httpx.Response(200, content=b'image-bytes', headers={'content-type': 'image/png'})
                    self.assertEqual(request.url.path, '/v1/images/edits')
                    self.assertIn(b'image-bytes', request.content)
                    self.assertIn(b'name="image"', request.content)
                    self.assertIn(b'image-model', request.content)
                    return httpx.Response(200, json={'data': [{'url': 'https://result.test/image.png'}]})
                client = httpx.Client(transport=httpx.MockTransport(upstream))
                with patch.object(app.httpx, 'Client', return_value=client):
                    result = app.generate_image(app.GenerateImageInput(prompt_id='draft', provider_id=provider['id'], prompt='edit pose', reference_urls=['https://reference.test/image.png']))
                self.assertEqual(len(requests), 2)
                self.assertEqual(len(result['results']), 1)
                with app.connect() as db:
                    self.assertEqual(db.execute('SELECT count(*) FROM examples').fetchone()[0], 0)

    def test_edit_without_reference_rejected(self):
        with self.assertRaises(app.HTTPException) as error:
            app.generate_image(app.GenerateImageInput(prompt_id='draft', provider_id='unused', prompt='edit', edit=True))
        self.assertEqual(error.exception.status_code, 400)

    def test_no_reference_uses_generation_and_rejects_incompatible_inputs(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(app, 'DB_PATH', None):
            app.DB_PATH = Path(directory) / 'db.sqlite'
            app.init_db()
            provider = app.create_provider(app.ProviderInput(name='url', base_url='https://provider.test/v1', model='image', provider_kind='image', reference_protocol='json_url'))
            def upstream(request):
                self.assertEqual(request.url.path, '/v1/images/generations')
                self.assertEqual(json.loads(request.content), {'model':'image', 'prompt':'generate', 'n':1})
                return httpx.Response(200, json={'data': [{'url': 'https://result.test/a.png'}]})
            with patch.object(app.httpx, 'Client', return_value=httpx.Client(transport=httpx.MockTransport(upstream))):
                app.generate_image(app.GenerateImageInput(prompt_id='draft', provider_id=provider['id'], prompt='generate'))
            for values in ({'reference_images':['data:image/png;base64,aW1hZ2U=']}, {'reference_urls':['https://a.test/x','https://b.test/y']}):
                with self.assertRaises(app.HTTPException) as error:
                    app.generate_image(app.GenerateImageInput(prompt_id='draft', provider_id=provider['id'], prompt='edit', **values))
                self.assertEqual(error.exception.status_code, 400)
