import json
import uuid
from pathlib import Path
from typing import Any, Literal

from fastapi import HTTPException
from pydantic import BaseModel, Field


class MediaInput(BaseModel):
    kind: Literal['image', 'video', 'audio', 'file']
    source: str = Field(min_length=1)
    original_path: str = ''
    role: str = ''
    label: str = ''
    metadata: dict[str, Any] = Field(default_factory=dict)


class ExampleInput(BaseModel):
    title: str = 'Example'
    input_text: str = ''
    output_text: str = ''
    notes: str = ''
    position: int = 0
    references: list[MediaInput] = Field(default_factory=list)
    results: list[MediaInput] = Field(default_factory=list)
    rating: int = Field(default=0, ge=0, le=5)
    generator_model: str = ''
    seed: str = ''
    generation_params: str = ''


class ExampleStore:
    def __init__(self, connect, storage: Path):
        self.connect = connect
        self.storage = storage

    @staticmethod
    def initialize(db) -> None:
        db.execute('''CREATE TABLE IF NOT EXISTS example_media (
            id TEXT PRIMARY KEY,
            example_id TEXT NOT NULL REFERENCES examples(id) ON DELETE CASCADE,
            purpose TEXT NOT NULL CHECK(purpose IN ('reference','result')),
            kind TEXT NOT NULL CHECK(kind IN ('image','video','audio','file')),
            source TEXT NOT NULL,
            original_path TEXT NOT NULL DEFAULT '',
            relative_path TEXT NOT NULL DEFAULT '',
            compressed_relative_path TEXT NOT NULL DEFAULT '',
            compression_status TEXT NOT NULL DEFAULT 'not_requested',
            role TEXT NOT NULL DEFAULT '',
            label TEXT NOT NULL DEFAULT '',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            position INTEGER NOT NULL,
            UNIQUE(example_id,purpose,position)
        )''')
        columns = {row[1] for row in db.execute('PRAGMA table_info(examples)')}
        for column, purpose in [('references_json','reference'),('result_urls','result')]:
            if column not in columns:
                continue
            for row in db.execute(f'SELECT id,{column} FROM examples').fetchall():
                for position, source in enumerate(json.loads(row[column])):
                    db.execute('INSERT INTO example_media(id,example_id,purpose,kind,source,original_path,position) VALUES(?,?,?,?,?,?,?)', (str(uuid.uuid4()),row['id'],purpose,'image',source,source if not source.startswith('data:') else '',position))
            db.execute(f'ALTER TABLE examples DROP COLUMN {column}')

    @staticmethod
    def get(db, example_id: str) -> dict:
        row = db.execute('SELECT * FROM examples WHERE id=?', (example_id,)).fetchone()
        if not row:
            raise HTTPException(404, 'Example 不存在')
        value = dict(row)
        value.update(references=[], results=[], attachments=[])
        for row in db.execute('SELECT * FROM example_media WHERE example_id=? ORDER BY position', (example_id,)):
            media = dict(row)
            media['metadata'] = json.loads(media.pop('metadata_json'))
            purpose = media.pop('purpose')
            media['compressed_source'] = '/media/' + media['compressed_relative_path'] if media['compressed_relative_path'] else ''
            value['references' if purpose == 'reference' else 'results'].append(media)
        for row in db.execute('SELECT * FROM attachments WHERE example_id=? ORDER BY created_at', (example_id,)):
            attachment = dict(row)
            attachment['url'] = '/media/' + attachment['relative_path']
            attachment['compressed'] = bool(attachment['compressed'])
            value['attachments'].append(attachment)
        return value

    def write(self, db, prompt_id: str, payload: ExampleInput, example_id: str | None = None, assets: dict[str,dict] | None = None, new_id: str | None = None) -> dict:
        existing = example_id is not None
        example_id = example_id or new_id or str(uuid.uuid4())
        if not db.execute('SELECT 1 FROM prompts WHERE id=?', (prompt_id,)).fetchone():
            raise HTTPException(404, 'Prompt 不存在')
        if existing and not db.execute('SELECT 1 FROM examples WHERE id=? AND prompt_id=?', (example_id,prompt_id)).fetchone():
            raise HTTPException(404, 'Example 不存在')
        values = payload.model_dump(exclude={'references','results'})
        values.update(id=example_id, prompt_id=prompt_id)
        db.execute('''INSERT INTO examples(id,prompt_id,title,input_text,output_text,notes,position,rating,generator_model,seed,generation_params)
            VALUES(:id,:prompt_id,:title,:input_text,:output_text,:notes,:position,:rating,:generator_model,:seed,:generation_params)
            ON CONFLICT(id) DO UPDATE SET title=excluded.title,input_text=excluded.input_text,output_text=excluded.output_text,
            notes=excluded.notes,position=excluded.position,rating=excluded.rating,generator_model=excluded.generator_model,
            seed=excluded.seed,generation_params=excluded.generation_params''', values)
        managed = {row['source']:dict(row) for row in db.execute('SELECT * FROM example_media WHERE example_id=?', (example_id,))}
        managed.update(assets or {})
        db.execute('DELETE FROM example_media WHERE example_id=?', (example_id,))
        for purpose, items in [('reference',payload.references),('result',payload.results)]:
            for position, item in enumerate(items):
                asset = managed.get(item.source, {})
                db.execute('''INSERT INTO example_media(id,example_id,purpose,kind,source,original_path,relative_path,compressed_relative_path,compression_status,role,label,metadata_json,position)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''', (str(uuid.uuid4()),example_id,purpose,item.kind,item.source,item.original_path,
                    asset.get('relative_path',''),asset.get('compressed_relative_path',''),asset.get('compression_status','not_requested'),item.role,item.label,json.dumps(item.metadata),position))
        return self.get(db, example_id)

    def delete(self, example_id: str) -> dict:
        with self.connect() as db:
            self.get(db, example_id)
            paths = {row[0] for row in db.execute("SELECT relative_path FROM example_media WHERE example_id=? UNION SELECT compressed_relative_path FROM example_media WHERE example_id=? UNION SELECT relative_path FROM attachments WHERE example_id=? UNION SELECT compressed_relative_path FROM attachments WHERE example_id=?", (example_id,)*4) if row[0]}
            db.execute('DELETE FROM examples WHERE id=?', (example_id,))
            db.execute('UPDATE video_jobs SET saved_example_id=NULL WHERE saved_example_id=?', (example_id,))
        for relative in paths:
            (self.storage / relative).unlink(missing_ok=True)
        return {'ok':True}
