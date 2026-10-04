import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app


class ExampleMediaTests(unittest.TestCase):
    def test_old_examples_migrate_without_losing_images(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(app, 'DB_PATH', Path(directory) / 'db.sqlite'):
            app.init_db()
            prompt = app.create_prompt(app.PromptInput(title='pose',category='pose'))
            with app.connect() as db:
                db.execute("ALTER TABLE examples ADD COLUMN references_json TEXT NOT NULL DEFAULT '[]'")
                db.execute("ALTER TABLE examples ADD COLUMN result_urls TEXT NOT NULL DEFAULT '[]'")
                db.execute("INSERT INTO examples(id,prompt_id,title,references_json,result_urls) VALUES('old',?,?,?,?)",(prompt['id'],'old',json.dumps(['https://ref.test/a.png']),json.dumps(['data:image/png;base64,YQ=='])))
            app.init_db()
            example = app.get_prompt(prompt['id'])['examples'][0]
            self.assertEqual(example['references'][0]['source'], 'https://ref.test/a.png')
            self.assertEqual(example['results'][0]['kind'], 'image')
            self.assertEqual(example['results'][0]['source'], 'data:image/png;base64,YQ==')
            app.init_db()
            self.assertEqual(len(app.get_prompt(prompt['id'])['examples'][0]['results']),1)
