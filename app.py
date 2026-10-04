from __future__ import annotations

import json
import struct
import base64
import binascii
import httpx
import logging
from logging.handlers import RotatingFileHandler
import mimetypes
import os
import shutil
import sqlite3
import ssl
import subprocess
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator, model_validator
from image_references import reference_files
from example_store import ExampleInput, ExampleStore
from media_store import MediaStore
from video_models import VideoSettings, VideoInput, VideoExampleInput, RecoverVideoInput
from video_service import VideoService

ROOT = Path(__file__).resolve().parent
STATE_DIR = Path(os.environ.get('PROMPT_TEMPLATE_STATE_DIR', ROOT))
DATA_DIR = STATE_DIR / "data"
STORAGE_DIR = STATE_DIR / "storage"
DB_PATH = DATA_DIR / "prompt_templates.sqlite3"
STATIC_DIR = ROOT / "static"
FFMPEG = os.environ.get("PROMPT_TEMPLATE_FFMPEG", "ffmpeg")
IMAGE_REQUEST_TIMEOUT = 300
LOG_DIR = STATE_DIR / "logs"

DATA_DIR.mkdir(parents=True,exist_ok=True)
STORAGE_DIR.mkdir(parents=True,exist_ok=True)
LOG_DIR.mkdir(parents=True,exist_ok=True)

logger = logging.getLogger("prompt_template")
if not logger.handlers:
    logger.setLevel(logging.INFO)
    handler = RotatingFileHandler(LOG_DIR / "app.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
logger.addHandler(handler)


def image_dimensions(encoded: str) -> tuple[int, int] | None:
    try:
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        return None
    if data.startswith(b'\x89PNG\r\n\x1a\n') and len(data) >= 24:
        return struct.unpack('>II', data[16:24])
    if data.startswith(b'RIFF') and data[8:12] == b'WEBP' and data[12:16] == b'VP8X' and len(data) >= 30:
        return (1 + int.from_bytes(data[24:27], 'little'), 1 + int.from_bytes(data[27:30], 'little'))
    if data[:2] == b'\xff\xd8':
        index = 2
        while index + 9 < len(data):
            if data[index] != 0xFF:
                index += 1
                continue
            marker = data[index + 1]
            index += 2
            if marker in (0xD8, 0xD9):
                continue
            length = int.from_bytes(data[index:index + 2], 'big')
            if marker in range(0xC0, 0xC4) and index + 7 < len(data):
                return (int.from_bytes(data[index + 5:index + 7], 'big'), int.from_bytes(data[index + 3:index + 5], 'big'))
            index += length
    return None


@contextmanager
def connect():
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        yield connection
        connection.commit()
    finally:
        connection.close()


def init_db() -> None:
    with connect() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS prompts (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                category TEXT NOT NULL,
                content TEXT NOT NULL,
                content_zh TEXT NOT NULL DEFAULT '',
                content_en TEXT NOT NULL DEFAULT '',
                content_ja TEXT NOT NULL DEFAULT '',
                language_sync TEXT NOT NULL DEFAULT 'manual',
                scenarios TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS prompt_tags (
                prompt_id TEXT NOT NULL REFERENCES prompts(id) ON DELETE CASCADE,
                tag TEXT NOT NULL,
                PRIMARY KEY(prompt_id, tag)
            );
            CREATE TABLE IF NOT EXISTS examples (
                id TEXT PRIMARY KEY,
                prompt_id TEXT NOT NULL REFERENCES prompts(id) ON DELETE CASCADE,
                title TEXT NOT NULL,
                input_text TEXT NOT NULL DEFAULT '',
                output_text TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT '',
                rating INTEGER NOT NULL DEFAULT 0,
                generator_model TEXT NOT NULL DEFAULT '',
                seed TEXT NOT NULL DEFAULT '',
                generation_params TEXT NOT NULL DEFAULT '',
                position INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS attachments (
                id TEXT PRIMARY KEY,
                prompt_id TEXT REFERENCES prompts(id) ON DELETE CASCADE,
                example_id TEXT REFERENCES examples(id) ON DELETE CASCADE,
                relative_path TEXT NOT NULL,
                original_path TEXT NOT NULL DEFAULT '',
                media_type TEXT NOT NULL,
                size_bytes INTEGER NOT NULL DEFAULT 0,
                compressed INTEGER NOT NULL DEFAULT 0,
                compression_status TEXT NOT NULL DEFAULT 'not_requested',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS providers (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                base_url TEXT NOT NULL,
                model TEXT NOT NULL,
                api_key TEXT NOT NULL DEFAULT '',
                capabilities TEXT NOT NULL DEFAULT 'text',
                provider_kind TEXT NOT NULL DEFAULT 'text',
                image_input TEXT NOT NULL DEFAULT 'both',
                verify_tls INTEGER NOT NULL DEFAULT 1,
                is_default INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS revision_presets (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS generation_presets (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS prompts_fts USING fts5(
                prompt_id UNINDEXED, title, content, scenarios, notes, tags
            );
            """
        )
        columns = {row[1] for row in db.execute("PRAGMA table_info(prompts)")}
        for name, definition in (("content_zh", "TEXT NOT NULL DEFAULT ''"), ("content_en", "TEXT NOT NULL DEFAULT ''"), ("content_ja", "TEXT NOT NULL DEFAULT ''"), ("language_sync", "TEXT NOT NULL DEFAULT 'manual'")):
            if name not in columns:
                db.execute(f"ALTER TABLE prompts ADD COLUMN {name} {definition}")
        provider_columns = {row[1] for row in db.execute("PRAGMA table_info(providers)")}
        if "provider_kind" not in provider_columns:
            db.execute("ALTER TABLE providers ADD COLUMN provider_kind TEXT NOT NULL DEFAULT 'text'")
        if "verify_tls" not in provider_columns:
            db.execute("ALTER TABLE providers ADD COLUMN verify_tls INTEGER NOT NULL DEFAULT 1")
        if "image_input" not in provider_columns:
            db.execute("ALTER TABLE providers ADD COLUMN image_input TEXT NOT NULL DEFAULT 'both'")
        for name, definition in (("reference_protocol", "TEXT NOT NULL DEFAULT 'multipart_edit'"), ("reference_endpoint", "TEXT NOT NULL DEFAULT '/images/edits'"), ("reference_field", "TEXT NOT NULL DEFAULT 'image'")):
            if name not in provider_columns:
                db.execute(f"ALTER TABLE providers ADD COLUMN {name} {definition}")
        if 'reference_limit' not in provider_columns:
            db.execute('ALTER TABLE providers ADD COLUMN reference_limit INTEGER NOT NULL DEFAULT 1')
            db.execute("UPDATE providers SET reference_limit=0,reference_protocol='multipart_edit' WHERE reference_protocol='none'")
        if 'reference_format' not in provider_columns:
            db.execute("ALTER TABLE providers ADD COLUMN reference_format TEXT NOT NULL DEFAULT 'single'")
        example_columns = {row[1] for row in db.execute("PRAGMA table_info(examples)")}
        for name, definition in (("rating", "INTEGER NOT NULL DEFAULT 0"), ("generator_model", "TEXT NOT NULL DEFAULT ''"), ("seed", "TEXT NOT NULL DEFAULT ''"), ("generation_params", "TEXT NOT NULL DEFAULT ''")):
            if name not in example_columns:
                db.execute(f"ALTER TABLE examples ADD COLUMN {name} {definition}")
        if 'video_settings' not in provider_columns:
            db.execute("ALTER TABLE providers ADD COLUMN video_settings TEXT NOT NULL DEFAULT '{}'")
        attachment_columns = {row[1] for row in db.execute('PRAGMA table_info(attachments)')}
        if 'compressed_relative_path' not in attachment_columns:
            db.execute("ALTER TABLE attachments ADD COLUMN compressed_relative_path TEXT NOT NULL DEFAULT ''")
        ExampleStore.initialize(db)
        VideoService.initialize(db)
        if "content" in columns:
            db.execute("UPDATE prompts SET content_zh=content WHERE content_zh='' AND content<>''")


def tags_for(db: sqlite3.Connection, prompt_id: str) -> list[str]:
    return [row[0] for row in db.execute("SELECT tag FROM prompt_tags WHERE prompt_id = ? ORDER BY tag", (prompt_id,))]


def attachment_dict(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["compressed"] = bool(item["compressed"])
    item["url"] = f"/media/{item['relative_path'].replace(os.sep, '/')}"
    item['compressed_url'] = '/media/' + item['compressed_relative_path'] if item['compressed_relative_path'] else ''
    return item


def prompt_dict(db: sqlite3.Connection, row: sqlite3.Row, detailed: bool = False) -> dict[str, Any]:
    item = dict(row)
    item["tags"] = tags_for(db, item["id"])
    if detailed:
        examples = []
        for example in db.execute("SELECT * FROM examples WHERE prompt_id = ? ORDER BY position, id", (item["id"],)):
            value = ExampleStore.get(db,example['id'])
            examples.append(value)
        item["examples"] = examples
        item["attachments"] = [attachment_dict(a) for a in db.execute("SELECT * FROM attachments WHERE prompt_id = ? ORDER BY created_at", (item["id"],))]
    return item


def refresh_fts(db: sqlite3.Connection, prompt_id: str) -> None:
    row = db.execute("SELECT * FROM prompts WHERE id = ?", (prompt_id,)).fetchone()
    if not row:
        db.execute("DELETE FROM prompts_fts WHERE prompt_id = ?", (prompt_id,))
        return
    db.execute("DELETE FROM prompts_fts WHERE prompt_id = ?", (prompt_id,))
    content = " ".join(row[name] for name in ("content", "content_zh", "content_en", "content_ja"))
    db.execute("INSERT INTO prompts_fts(prompt_id,title,content,scenarios,notes,tags) VALUES(?,?,?,?,?,?)", (prompt_id, row["title"], content, row["scenarios"], row["notes"], " ".join(tags_for(db, prompt_id))))


class PromptInput(BaseModel):
    title: str = Field(min_length=1)
    category: str = Field(pattern="^[a-zA-Z0-9_-]+$")
    content_zh: str = ""
    content_en: str = ""
    content_ja: str = ""
    language_sync: str = Field(default="manual", pattern="^(sync|manual|ai_pending)$")
    scenarios: str = ""
    notes: str = ""
    tags: list[str] = []


class ProviderInput(BaseModel):
    name: str = Field(min_length=1)
    base_url: str = Field(min_length=1)
    model: str = Field(min_length=1)
    api_key: str = ""
    capabilities: list[str] = ["text"]
    provider_kind: Literal['text','multimodal','image','video'] = 'text'
    video_settings: VideoSettings | None = None
    verify_tls: bool = True
    image_input: str = Field(default="both", pattern="^(url|base64|both)$")
    is_default: bool = False
    reference_protocol: Literal['multipart_edit', 'json_url'] = 'multipart_edit'
    reference_endpoint: str = Field(default='/images/edits', pattern=r'^/[A-Za-z0-9_/-]+$')
    reference_field: str = Field(default='image', pattern=r'^[A-Za-z_][A-Za-z0-9_]*(\[\])?$')
    reference_limit: int = Field(default=1, ge=0, le=32)
    reference_format: Literal['single', 'array'] = 'single'

    @model_validator(mode='after')
    def validate_reference_settings(self):
        if self.provider_kind == 'video':
            self.video_settings = self.video_settings or VideoSettings()
            return self
        self.video_settings = None
        if self.reference_limit > 1 and self.reference_format != 'array':
            raise ValueError('多图必须选择列表提交格式')
        if self.reference_protocol == 'json_url' and self.reference_field.endswith('[]'):
            raise ValueError('JSON 参数名不使用 [] 后缀，请选择数组格式')
        return self

    @field_validator('reference_field')
    @classmethod
    def validate_reference_field(cls, value: str) -> str:
        if value.removesuffix('[]') in {'model', 'prompt', 'n', 'size'}:
            raise ValueError('参考图参数名不能覆盖 model、prompt、n 或 size')
        return value


class ProviderModelsInput(BaseModel):
    provider_id: str | None = None
    base_url: str = Field(min_length=1)
    api_key: str = ""
    verify_tls: bool = True


class RevisionPresetInput(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    content: str = Field(min_length=1)

    @field_validator('title', 'content', mode='before')
    @classmethod
    def strip_text(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value


GenerationPresetInput = RevisionPresetInput


class ReviseInput(BaseModel):
    prompt_id: str
    request: str = Field(min_length=1)
    provider_id: str | None = None
    images: list[str] = []
    image_urls: list[str] = []


class GenerateImageInput(BaseModel):
    model_config = {'extra': 'forbid'}
    prompt_id: str
    provider_id: str
    prompt: str = Field(min_length=1)
    references: list[str] = Field(default_factory=list, max_length=32)
    edit: bool = False
    size: str | None = Field(default=None, pattern=r'^(auto|[1-9][0-9]*x[1-9][0-9]*|)$', max_length=32)


def normalize_tags(values: list[str]) -> list[str]:
    return sorted({value.strip() for value in values if value.strip()})


def save_prompt(db: sqlite3.Connection, prompt_id: str, payload: PromptInput) -> None:
    now = "CURRENT_TIMESTAMP"
    db.execute(f"UPDATE prompts SET title=?, category=?, content_zh=?, content_en=?, content_ja=?, language_sync=?, scenarios=?, notes=?, updated_at={now} WHERE id=?", (payload.title.strip(), payload.category, payload.content_zh, payload.content_en, payload.content_ja, payload.language_sync, payload.scenarios, payload.notes, prompt_id))
    db.execute("DELETE FROM prompt_tags WHERE prompt_id = ?", (prompt_id,))
    db.executemany("INSERT INTO prompt_tags(prompt_id, tag) VALUES(?, ?)", [(prompt_id, tag) for tag in normalize_tags(payload.tags)])
    refresh_fts(db, prompt_id)


app = FastAPI(title="Prompt Template Manager")
app.mount("/media", StaticFiles(directory=STORAGE_DIR), name="media")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
video_service = VideoService(connect, STORAGE_DIR, DATA_DIR / 'video-cache', FFMPEG)


@app.on_event("startup")
def startup() -> None:
    init_db()
    video_service.start()


@app.on_event('shutdown')
def shutdown() -> None:
    video_service.stop()


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/prompts")
def list_prompts(q: str = "", category: str = "", tag: str = "", offset: int = 0, limit: int = 30) -> list[dict[str, Any]]:
    with connect() as db:
        clauses: list[str] = []
        params: list[Any] = []
        if q.strip():
            clauses.append("p.id IN (SELECT prompt_id FROM prompts_fts WHERE prompts_fts MATCH ?)")
            params.append(" ".join(f'"{part.replace(chr(34), "")}"*' for part in q.split() if part.strip()))
        if category:
            clauses.append("p.category = ?")
            params.append(category)
        if tag:
            clauses.append("EXISTS (SELECT 1 FROM prompt_tags pt WHERE pt.prompt_id=p.id AND pt.tag=?)")
            params.append(tag)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        safe_offset = max(0, offset)
        safe_limit = max(1, min(100, limit))
        rows = db.execute(f"SELECT p.* FROM prompts p{where} ORDER BY p.updated_at DESC LIMIT ? OFFSET ?", [*params, safe_limit, safe_offset]).fetchall()
        return [prompt_dict(db, row) for row in rows]


@app.get('/api/revision-presets')
def list_revision_presets() -> list[dict[str, Any]]:
    with connect() as db:
        return [dict(row) for row in db.execute('SELECT id,title,content FROM revision_presets ORDER BY created_at,id')]


@app.post('/api/revision-presets')
def create_revision_preset(payload: RevisionPresetInput) -> dict[str, Any]:
    preset = {'id': str(uuid.uuid4()), **payload.model_dump()}
    with connect() as db:
        db.execute('INSERT INTO revision_presets(id,title,content) VALUES(:id,:title,:content)', preset)
    return preset


@app.put('/api/revision-presets/{preset_id}')
def update_revision_preset(preset_id: str, payload: RevisionPresetInput) -> dict[str, Any]:
    with connect() as db:
        cursor = db.execute('UPDATE revision_presets SET title=?,content=? WHERE id=?', (payload.title, payload.content, preset_id))
        if not cursor.rowcount:
            raise HTTPException(404, '常用建议不存在')
    return {'id': preset_id, **payload.model_dump()}


@app.delete('/api/revision-presets/{preset_id}')
def delete_revision_preset(preset_id: str) -> dict[str, bool]:
    with connect() as db:
        if not db.execute('DELETE FROM revision_presets WHERE id=?', (preset_id,)).rowcount:
            raise HTTPException(404, '常用建议不存在')
    return {'ok': True}


@app.get('/api/generation-presets')
def list_generation_presets() -> list[dict[str, Any]]:
    with connect() as db:
        return [dict(row) for row in db.execute('SELECT id,title,content FROM generation_presets ORDER BY created_at,id')]


@app.post('/api/generation-presets')
def create_generation_preset(payload: GenerationPresetInput) -> dict[str, Any]:
    preset = {'id': str(uuid.uuid4()), **payload.model_dump()}
    with connect() as db:
        db.execute('INSERT INTO generation_presets(id,title,content) VALUES(:id,:title,:content)', preset)
    return preset


@app.put('/api/generation-presets/{preset_id}')
def update_generation_preset(preset_id: str, payload: GenerationPresetInput) -> dict[str, Any]:
    with connect() as db:
        cursor = db.execute('UPDATE generation_presets SET title=?,content=? WHERE id=?', (payload.title, payload.content, preset_id))
        if not cursor.rowcount:
            raise HTTPException(404, '生图模板不存在')
    return {'id': preset_id, **payload.model_dump()}


@app.delete('/api/generation-presets/{preset_id}')
def delete_generation_preset(preset_id: str) -> dict[str, bool]:
    with connect() as db:
        if not db.execute('DELETE FROM generation_presets WHERE id=?', (preset_id,)).rowcount:
            raise HTTPException(404, '生图模板不存在')
    return {'ok': True}


@app.post("/api/prompts")
def create_prompt(payload: PromptInput) -> dict[str, Any]:
    prompt_id = str(uuid.uuid4())
    with connect() as db:
        db.execute("INSERT INTO prompts(id,title,category,content,content_zh,content_en,content_ja,language_sync,scenarios,notes) VALUES(?,?,?,?,?,?,?,?,?,?)", (prompt_id, payload.title.strip(), payload.category, payload.content_zh, payload.content_zh, payload.content_en, payload.content_ja, payload.language_sync, payload.scenarios, payload.notes))
        db.executemany("INSERT INTO prompt_tags(prompt_id,tag) VALUES(?,?)", [(prompt_id, tag) for tag in normalize_tags(payload.tags)])
        refresh_fts(db, prompt_id)
        return prompt_dict(db, db.execute("SELECT * FROM prompts WHERE id=?", (prompt_id,)).fetchone(), True)


@app.get("/api/prompts/{prompt_id}")
def get_prompt(prompt_id: str) -> dict[str, Any]:
    with connect() as db:
        row = db.execute("SELECT * FROM prompts WHERE id=?", (prompt_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Prompt 不存在")
        return prompt_dict(db, row, True)


@app.put("/api/prompts/{prompt_id}")
def update_prompt(prompt_id: str, payload: PromptInput) -> dict[str, Any]:
    with connect() as db:
        if not db.execute("SELECT 1 FROM prompts WHERE id=?", (prompt_id,)).fetchone():
            raise HTTPException(404, "Prompt 不存在")
        save_prompt(db, prompt_id, payload)
        return prompt_dict(db, db.execute("SELECT * FROM prompts WHERE id=?", (prompt_id,)).fetchone(), True)


@app.delete("/api/prompts/{prompt_id}")
def delete_prompt(prompt_id: str) -> dict[str, bool]:
    with connect() as db:
        if db.execute("SELECT 1 FROM video_jobs WHERE prompt_id=? AND status IN ('pending','submitting','queued','running','downloading')",(prompt_id,)).fetchone():
            raise HTTPException(409,'该条目仍有视频任务进行中，请等待任务结束')
        attachments = db.execute("SELECT relative_path FROM attachments WHERE prompt_id=? UNION SELECT compressed_relative_path FROM attachments WHERE prompt_id=? UNION SELECT relative_path FROM example_media WHERE example_id IN (SELECT id FROM examples WHERE prompt_id=?) UNION SELECT compressed_relative_path FROM example_media WHERE example_id IN (SELECT id FROM examples WHERE prompt_id=?)", (prompt_id,)*4).fetchall()
        jobs = [row[0] for row in db.execute('SELECT id FROM video_jobs WHERE prompt_id=?',(prompt_id,))]
        if not db.execute("SELECT 1 FROM prompts WHERE id=?", (prompt_id,)).fetchone():
            raise HTTPException(404, "Prompt 不存在")
        db.execute("DELETE FROM prompts WHERE id=?", (prompt_id,))
        for row in attachments:
            if not row[0]:
                continue
            path = STORAGE_DIR / row[0]
            if path.is_file():
                path.unlink()
        for job_id in jobs:
            shutil.rmtree(video_service.cache/job_id,ignore_errors=True)
        return {"ok": True}


@app.post("/api/prompts/{prompt_id}/examples")
def add_example(prompt_id: str, payload: ExampleInput) -> dict[str, Any]:
    with connect() as db:
        return ExampleStore(connect,STORAGE_DIR).write(db,prompt_id,payload)


@app.put("/api/examples/{example_id}")
def update_example(example_id: str, payload: ExampleInput) -> dict[str, Any]:
    with connect() as db:
        current = ExampleStore.get(db,example_id)
        return ExampleStore(connect,STORAGE_DIR).write(db,current['prompt_id'],payload,example_id)


@app.delete("/api/examples/{example_id}")
def delete_example(example_id: str) -> dict[str, bool]:
    return ExampleStore(connect,STORAGE_DIR).delete(example_id)


@app.post("/api/prompts/{prompt_id}/attachments")
async def upload_attachment(prompt_id: str, file: UploadFile = File(...), original_path: str = Form(""), compress: bool = Form(False), example_id: str = Form("")) -> dict[str, Any]:
    with connect() as db:
        if not db.execute("SELECT 1 FROM prompts WHERE id=?", (prompt_id,)).fetchone():
            raise HTTPException(404, "Prompt 不存在")
        if example_id and not db.execute("SELECT 1 FROM examples WHERE id=? AND prompt_id=?", (example_id, prompt_id)).fetchone():
            raise HTTPException(400, "Example 不属于当前 Prompt")
        attachment_id = str(uuid.uuid4())
        suffix = Path(file.filename or "asset").suffix.lower()
        relative = Path(prompt_id) / (attachment_id + suffix)
        destination = STORAGE_DIR / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("wb") as output:
            shutil.copyfileobj(file.file, output)
        media_type = file.content_type or mimetypes.guess_type(file.filename or "")[0] or "application/octet-stream"
        status = "requested"
        compressed = False
        if compress and media_type.startswith("video/"):
            result = MediaStore(STORAGE_DIR,FFMPEG).compress(destination)
            status = result['compression_status']
            compressed = status == 'completed'
        elif compress and media_type.startswith("image/"):
            status = "unsupported_without_image_encoder"
        else:
            status = "not_requested"
        db.execute("INSERT INTO attachments(id,prompt_id,example_id,relative_path,original_path,media_type,size_bytes,compressed,compression_status) VALUES(?,?,?,?,?,?,?,?,?)", (attachment_id, prompt_id, example_id or None, relative.as_posix(), original_path, media_type, destination.stat().st_size, compressed, status))
        if compress and media_type.startswith('video/'):
            db.execute('UPDATE attachments SET compressed_relative_path=? WHERE id=?',(result['compressed_relative_path'],attachment_id))
        row = db.execute("SELECT * FROM attachments WHERE id=?", (attachment_id,)).fetchone()
        return attachment_dict(row)


@app.patch("/api/attachments/{attachment_id}")
def update_attachment_path(attachment_id: str, original_path: str = Form(...)) -> dict[str, Any]:
    with connect() as db:
        db.execute("UPDATE attachments SET original_path=? WHERE id=?", (original_path, attachment_id))
        row = db.execute("SELECT * FROM attachments WHERE id=?", (attachment_id,)).fetchone()
        if not row:
            raise HTTPException(404, "附件不存在")
        return attachment_dict(row)


@app.post("/api/attachments/{attachment_id}/open-original")
def open_original_attachment(attachment_id: str) -> dict[str, bool]:
    with connect() as db:
        row = db.execute("SELECT original_path FROM attachments WHERE id=?", (attachment_id,)).fetchone()
        if not row:
            raise HTTPException(404, "附件不存在")
        path = row["original_path"]
        if not path or "://" in path:
            raise HTTPException(400, "该附件没有可打开的本地原始路径")
        target = Path(path)
        if not target.exists():
            raise HTTPException(404, "原始路径不存在")
        if os.name == "nt":
            if target.is_file():
                subprocess.Popen(["explorer", "/select,", str(target)])
            else:
                subprocess.Popen(["explorer", str(target)])
        else:
            subprocess.Popen(["xdg-open", str(target)])
        return {"ok": True}


@app.delete("/api/attachments/{attachment_id}")
def delete_attachment(attachment_id: str) -> dict[str, bool]:
    with connect() as db:
        row = db.execute("SELECT relative_path,compressed_relative_path FROM attachments WHERE id=?", (attachment_id,)).fetchone()
        if not row:
            raise HTTPException(404, "附件不存在")
        db.execute("DELETE FROM attachments WHERE id=?", (attachment_id,))
        path = STORAGE_DIR / row[0]
        if path.is_file():
            path.unlink()
        if row['compressed_relative_path']:
            (STORAGE_DIR / row['compressed_relative_path']).unlink(missing_ok=True)
        return {"ok": True}


@app.get("/api/providers")
def list_providers() -> list[dict[str, Any]]:
    with connect() as db:
        return [{**dict(row), "api_key": "••••••••" if row["api_key"] else "", "capabilities": json.loads(row["capabilities"]), "verify_tls": bool(row["verify_tls"]), 'video_settings':json.loads(row['video_settings'])} for row in db.execute("SELECT * FROM providers ORDER BY provider_kind, is_default DESC, name")]


@app.post("/api/providers")
def create_provider(payload: ProviderInput) -> dict[str, Any]:
    provider_id = str(uuid.uuid4())
    with connect() as db:
        if payload.is_default:
            db.execute("UPDATE providers SET is_default=0 WHERE provider_kind=?", (payload.provider_kind,))
        image_input = payload.image_input if payload.provider_kind == "multimodal" else "both"
        db.execute("INSERT INTO providers(id,name,base_url,model,api_key,capabilities,provider_kind,image_input,verify_tls,is_default) VALUES(?,?,?,?,?,?,?,?,?,?)", (provider_id, payload.name, payload.base_url.rstrip("/"), payload.model, payload.api_key, json.dumps(payload.capabilities), payload.provider_kind, image_input, payload.verify_tls, payload.is_default))
        db.execute("UPDATE providers SET reference_protocol=?,reference_endpoint=?,reference_field=?,reference_limit=?,reference_format=? WHERE id=?", (payload.reference_protocol, payload.reference_endpoint, payload.reference_field, payload.reference_limit, payload.reference_format, provider_id))
        db.execute('UPDATE providers SET video_settings=? WHERE id=?',(payload.video_settings.model_dump_json() if payload.video_settings else '{}',provider_id))
        row = dict(db.execute("SELECT * FROM providers WHERE id=?", (provider_id,)).fetchone())
        row['video_settings'] = json.loads(row['video_settings'])
        row["capabilities"] = payload.capabilities
        row["api_key"] = "••••••••" if row["api_key"] else ""
        return row


@app.put("/api/providers/{provider_id}")
def update_provider(provider_id: str, payload: ProviderInput) -> dict[str, Any]:
    with connect() as db:
        current = db.execute("SELECT * FROM providers WHERE id=?", (provider_id,)).fetchone()
        if not current:
            raise HTTPException(404, "Provider 不存在")
        api_key = current["api_key"] if payload.api_key == "••••••••" else payload.api_key
        if payload.is_default:
            db.execute("UPDATE providers SET is_default=0 WHERE provider_kind=?", (payload.provider_kind,))
        image_input = payload.image_input if payload.provider_kind == "multimodal" else "both"
        db.execute("UPDATE providers SET name=?,base_url=?,model=?,api_key=?,capabilities=?,provider_kind=?,image_input=?,verify_tls=?,is_default=? WHERE id=?", (payload.name, payload.base_url.rstrip("/"), payload.model, api_key, json.dumps(payload.capabilities), payload.provider_kind, image_input, payload.verify_tls, payload.is_default, provider_id))
        db.execute("UPDATE providers SET reference_protocol=?,reference_endpoint=?,reference_field=?,reference_limit=?,reference_format=? WHERE id=?", (payload.reference_protocol, payload.reference_endpoint, payload.reference_field, payload.reference_limit, payload.reference_format, provider_id))
        db.execute('UPDATE providers SET video_settings=? WHERE id=?',(payload.video_settings.model_dump_json() if payload.video_settings else '{}',provider_id))
        row = dict(db.execute("SELECT * FROM providers WHERE id=?", (provider_id,)).fetchone())
        row['video_settings'] = json.loads(row['video_settings'])
        row["capabilities"] = payload.capabilities
        row["api_key"] = "••••••••" if row["api_key"] else ""
        return row


@app.delete("/api/providers/{provider_id}")
def delete_provider(provider_id: str) -> dict[str, bool]:
    with connect() as db:
        if not db.execute("SELECT 1 FROM providers WHERE id=?", (provider_id,)).fetchone():
            raise HTTPException(404, "Provider 不存在")
        if db.execute('SELECT 1 FROM video_jobs WHERE provider_id=?',(provider_id,)).fetchone():
            raise HTTPException(409,'此 Provider 仍有视频任务记录，请先清除已结束的任务记录')
        db.execute("DELETE FROM providers WHERE id=?", (provider_id,))
        return {"ok": True}


def provider_endpoint(base_url: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    return base + "/chat/completions" if base.endswith("/v1") else base + "/v1/chat/completions"


@app.post("/api/providers/models")
def provider_models(payload: ProviderModelsInput) -> dict[str, list[str]]:
    import urllib.request
    base = payload.base_url.rstrip("/")
    endpoint = base if base.endswith("/models") else (base + "/models" if base.endswith("/v1") else base + "/v1/models")
    headers = {"Accept": "application/json", "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0 Safari/537.36"}
    api_key = payload.api_key
    if api_key == "••••••••":
        with connect() as db:
            stored = db.execute('SELECT api_key,base_url FROM providers WHERE id=?', (payload.provider_id,)).fetchone()
        if not stored or stored['base_url'].rstrip('/') != payload.base_url.rstrip('/'):
            raise HTTPException(400, '更换地址后请重新填写 API Key 再加载模型')
        api_key = stored['api_key']
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(endpoint, headers=headers, method="GET")
    try:
        context = ssl.create_default_context() if payload.verify_tls else ssl._create_unverified_context()
        with urllib.request.urlopen(request, timeout=30, context=context) as response:
            raw = json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        logger.error("provider models HTTP %s endpoint=%s detail=%s", exc.code, endpoint, detail)
        raise HTTPException(exc.code, f"加载模型失败：HTTP {exc.code} {detail}") from exc
    except Exception as exc:
        logger.exception("provider models request failed endpoint=%s", endpoint)
        raise HTTPException(502, f"加载模型失败：{exc}") from exc
    models = raw.get("data", []) if isinstance(raw, dict) else []
    return {"models": [str(item.get("id")) for item in models if isinstance(item, dict) and item.get("id")]}


@app.post("/api/ai/revise")
def revise_prompt(payload: ReviseInput) -> dict[str, str]:
    with connect() as db:
        prompt = db.execute("SELECT * FROM prompts WHERE id=?", (payload.prompt_id,)).fetchone()
        provider = db.execute("SELECT * FROM providers WHERE id=?", (payload.provider_id,)).fetchone() if payload.provider_id else db.execute("SELECT * FROM providers WHERE is_default=1 AND provider_kind IN ('text','multimodal') LIMIT 1").fetchone()
        if not prompt:
            raise HTTPException(404, "Prompt 不存在")
        if not provider:
            raise HTTPException(400, "请先配置 provider")
        if payload.images and provider["image_input"] == "url":
            raise HTTPException(400, "当前 provider 只支持公网图片 URL")
        if payload.image_urls and provider["image_input"] == "base64":
            raise HTTPException(400, "当前 provider 只支持图片上传")
        image_inputs = [*payload.images, *payload.image_urls]
        if image_inputs and provider["provider_kind"] != "multimodal":
            raise HTTPException(400, "上传图片需要选择多模态 provider")
        text_content = f"当前中文 prompt：\n{prompt['content_zh']}\n\n当前英文 prompt：\n{prompt['content_en']}\n\n当前日文 prompt：\n{prompt['content_ja']}\n\n修改要求：\n{payload.request}"
        user_content: str | list[dict[str, Any]] = text_content
        if image_inputs:
            user_content = [{"type": "text", "text": text_content}]
            user_content.extend({"type": "image_url", "image_url": {"url": image}} for image in image_inputs)
        image_instruction = "本次提供了图片，请保留用户对图片的引用标记：中文使用图1、图2，英文使用image1、image2，日文使用画像1、画像2。" if image_inputs else "本次没有提供图片，请不要提及图片、图片引用或未引用图片。"
        body = {"model": provider["model"], "messages": [{"role": "system", "content": f"你是 prompt 编辑助手。请返回 JSON 对象，不要 Markdown，格式为 {{\"zh\":\"中文修改建议\",\"en\":\"English revision suggestion\",\"ja\":\"日本語の修正提案\"}}。{image_instruction}"}, {"role": "user", "content": user_content}], "temperature": 0.2}
        import urllib.request
        request = urllib.request.Request(provider_endpoint(provider["base_url"]), data=json.dumps(body).encode(), headers={"Content-Type": "application/json", "Accept": "application/json", "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0 Safari/537.36", "Authorization": f"Bearer {provider['api_key']}"}, method="POST")
        context = ssl.create_default_context() if provider["verify_tls"] else ssl._create_unverified_context()
        try:
            with urllib.request.urlopen(request, timeout=120, context=context) as response:
                raw = json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:800]
            logger.error("provider revise HTTP %s provider=%s model=%s detail=%s", exc.code, provider["name"], provider["model"], detail)
            raise HTTPException(exc.code, f"Provider 调用失败：HTTP {exc.code} {detail}") from exc
        except Exception as exc:
            logger.exception("provider revise request failed provider=%s model=%s", provider["name"], provider["model"])
            raise HTTPException(502, f"Provider 调用失败：{exc}") from exc
        content = raw.get("choices", [{}])[0].get("message", {}).get("content", "") if isinstance(raw, dict) else ""
        try:
            cleaned = content.strip().removeprefix("```json").removesuffix("```").strip()
            result = json.loads(cleaned)
        except json.JSONDecodeError:
            result = {"zh": content, "en": "未返回英文修改建议。", "ja": "日本語の修正提案は返されませんでした。"}
        return {"content_zh": str(result.get("zh", "")), "content_en": str(result.get("en", "")), "content_ja": str(result.get("ja", ""))}


@app.post("/api/generate/image")
def generate_image(payload: GenerateImageInput) -> dict[str, Any]:
    references = [value.strip() for value in payload.references]
    reference_count = len(references)
    if payload.edit and not reference_count:
        raise HTTPException(400, "Edit 需要至少一张参考图")
    for index, reference in enumerate(references, 1):
        if reference.startswith('data:image/'):
            continue
        try:
            url = httpx.URL(reference)
            if url.scheme not in ('http', 'https') or not url.host:
                raise ValueError('invalid URL')
        except (httpx.InvalidURL, ValueError) as exc:
            raise HTTPException(400, f'图{index}：请使用 HTTP(S) 图片 URL 或上传图片') from exc
    with connect() as db:
        provider = db.execute("SELECT * FROM providers WHERE id=?", (payload.provider_id,)).fetchone()
        if not provider or provider["provider_kind"] != "image":
            raise HTTPException(400, "请选择图片生成 provider")
        protocol = provider['reference_protocol']
        if reference_count > provider['reference_limit']:
            raise HTTPException(400, f"当前 provider 最多支持 {provider['reference_limit']} 张参考图，已提交 {reference_count} 张")
        if protocol == 'json_url' and any(value.startswith('data:') for value in references):
            raise HTTPException(400, '当前 provider 仅接受公网图片 URL')
        body = {"model": provider["model"], "prompt": payload.prompt, "n": 1}
        if payload.size:
            body['size'] = payload.size
        route = provider['reference_endpoint'] if reference_count else '/images/generations'
        endpoint = provider_endpoint(provider["base_url"]).removesuffix("/chat/completions") + route
        headers = {"Authorization": f"Bearer {provider['api_key']}", "User-Agent": "Mozilla/5.0"}
        try:
            with httpx.Client(verify=bool(provider["verify_tls"]), timeout=IMAGE_REQUEST_TIMEOUT) as client:
                if reference_count and protocol == 'multipart_edit':
                    files = reference_files(client, references, provider['reference_field'])
                    response = client.post(endpoint, headers=headers, data={key: str(value) for key, value in body.items()}, files=files)
                else:
                    if references:
                        body[provider['reference_field']] = references if provider['reference_format'] == 'array' else references[0]
                    response = client.post(endpoint, headers=headers, json=body)
                response.raise_for_status()
                raw = response.json()
            logger.info("image request completed provider=%s model=%s route=%s protocol=%s references=%s requested_size=%s response_status=%s response_count=%s response_keys=%s", provider["name"], provider["model"], route, protocol, reference_count, payload.size or 'provider_default', response.status_code, len(raw.get('data', [])) if isinstance(raw, dict) and isinstance(raw.get('data'), list) else 0, sorted(raw.keys()) if isinstance(raw, dict) else type(raw).__name__)
            if isinstance(raw, dict) and isinstance(raw.get('data'), list):
                for index, item in enumerate(raw['data'], 1):
                    if not isinstance(item, dict):
                        continue
                    encoded = item.get('b64_json', '')
                    dimensions = image_dimensions(encoded) if encoded else None
                    logger.info("image response item=%s output=%s actual_size=%s", index, 'base64' if encoded else 'url' if item.get('url') else 'unknown', f'{dimensions[0]}x{dimensions[1]}' if dimensions else 'not_available')
        except httpx.HTTPStatusError as exc:
            detail = exc.response.text[:800]
            logger.error("image request HTTP %s route=%s detail=%s", exc.response.status_code, route, detail)
            raise HTTPException(exc.response.status_code, f"生图或参考图读取失败：HTTP {exc.response.status_code} {detail}") from exc
        except (httpx.RequestError, ValueError) as exc:
            logger.exception("image request failed route=%s", route)
            raise HTTPException(502, "生图请求失败，请查看日志") from exc
        items = raw.get("data", []) if isinstance(raw, dict) else []
        return {"results": [{"url": item.get("url", ""), "b64_json": item.get("b64_json", "")} for item in items if isinstance(item, dict)]}


@app.post('/api/video-jobs', status_code=202)
def create_video_job(payload: VideoInput) -> dict:
    return video_service.create(payload)


@app.get('/api/video-jobs')
def list_video_jobs(prompt_id: str) -> list[dict]:
    return video_service.list(prompt_id)


@app.get('/api/video-jobs/{job_id}')
def get_video_job(job_id: str) -> dict:
    return video_service.get(job_id)


@app.post('/api/video-jobs/{job_id}/refresh')
def refresh_video_job(job_id: str) -> dict:
    return video_service.refresh(job_id)


@app.post('/api/video-jobs/{job_id}/recover')
def recover_video_job(job_id: str, payload: RecoverVideoInput) -> dict:
    return video_service.recover(job_id,payload.remote_id)


@app.get('/api/video-jobs/{job_id}/content')
def video_job_content(job_id: str) -> FileResponse:
    return FileResponse(video_service.content_path(job_id), media_type='video/mp4', filename='video.mp4', content_disposition_type='inline')


@app.post('/api/video-jobs/{job_id}/example')
def save_video_example(job_id: str, payload: VideoExampleInput) -> dict:
    return video_service.save_example(job_id,payload)


@app.delete('/api/video-jobs/{job_id}')
def delete_video_job(job_id: str) -> dict:
    return video_service.delete(job_id)
