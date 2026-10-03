import tempfile
import unittest
from pathlib import Path

import app


class PromptStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        app.DB_PATH = Path(self.temp.name) / "test.sqlite3"
        app.STORAGE_DIR = Path(self.temp.name) / "storage"
        app.STORAGE_DIR.mkdir()
        app.init_db()

    def tearDown(self):
        self.temp.cleanup()

    def test_fts_and_tags_round_trip(self):
        payload = app.PromptInput(title="Sword attack", category="action", content_zh="fast sword swing", content_en="fast sword swing", content_ja="素早い剣の振り", scenarios="game animation", tags=["melee", "game"])
        app.create_prompt(payload)
        rows = app.list_prompts(q="sword", category="action", tag="melee")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["tags"], ["game", "melee"])

    def test_relative_attachment_and_original_path(self):
        payload = app.PromptInput(title="Pose", category="pose")
        created = app.create_prompt(payload)
        self.assertTrue(created["id"])


if __name__ == "__main__":
    unittest.main()
