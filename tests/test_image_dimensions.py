import base64
import unittest

import app


class ImageDimensionTests(unittest.TestCase):
    def test_png_dimensions(self):
        data = b'\x89PNG\r\n\x1a\n' + b'\x00' * 8 + (3840).to_bytes(4, 'big') + (2160).to_bytes(4, 'big')
        self.assertEqual(app.image_dimensions(base64.b64encode(data).decode()), (3840, 2160))

    def test_invalid_data_is_unknown(self):
        self.assertIsNone(app.image_dimensions('not-an-image'))
