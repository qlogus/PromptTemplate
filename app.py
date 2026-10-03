from __future__ import annotations

import json
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
from pydantic import BaseModel, Field, field_validator

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
STORAGE_DIR = ROOT / "storage"
DB_PATH = DATA_DIR / "prompt_templates.sqlite3"
STATIC_DIR = ROOT / "static"
FFMPEG = os.environ.get("PROMPT_TEMPLATE_FFMPEG", "ffmpeg")
LOG_DIR = ROOT / "logs"

DATA_DIR.mkdir(exist_ok=True)
STORAGE_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)

logger = logging.getLogger("prompt_template")
if not logger.handlers:
    logger.setLevel(logging.INFO)
    handler = RotatingFileHandler(LOG_DIR / "app.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)


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
                references_json TEXT NOT NULL DEFAULT '[]',
                result_urls TEXT NOT NULL DEFAULT '[]',
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
        example_columns = {row[1] for row in db.execute("PRAGMA table_info(examples)")}
        for name, definition in (("references_json", "TEXT NOT NULL DEFAULT '[]'"), ("result_urls", "TEXT NOT NULL DEFAULT '[]'"), ("rating", "INTEGER NOT NULL DEFAULT 0"), ("generator_model", "TEXT NOT NULL DEFAULT ''"), ("seed", "TEXT NOT NULL DEFAULT ''"), ("generation_params", "TEXT NOT NULL DEFAULT ''")):
            if name not in example_columns:
                db.execute(f"ALTER TABLE examples ADD COLUMN {name} {definition}")
        if "content" in columns:
            db.execute("UPDATE prompts SET content_zh=content WHERE content_zh='' AND content<>''")


def tags_for(db: sqlite3.Connection, prompt_id: str) -> list[str]:
    return [row[0] for row in db.execute("SELECT tag FROM prompt_tags WHERE prompt_id = ? ORDER BY tag", (prompt_id,))]


def attachment_dict(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["compressed"] = bool(item["compressed"])
    item["url"] = f"/media/{item['relative_path'].replace(os.sep, '/')}"
    return item


def prompt_dict(db: sqlite3.Connection, row: sqlite3.Row, detailed: bool = False) -> dict[str, Any]:
    item = dict(row)
    item["tags"] = tags_for(db, item["id"])
    if detailed:
        examples = []
        for example in db.execute("SELECT * FROM examples WHERE prompt_id = ? ORDER BY position, id", (item["id"],)):
            value = dict(example)
            value["references"] = json.loads(value.pop("references_json", "[]"))
            value["result_urls"] = json.loads(value.pop("result_urls", "[]"))
            value["attachments"] = [attachment_dict(a) for a in db.execute("SELECT * FROM attachments WHERE example_id = ? ORDER BY created_at", (value["id"],))]
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


class ExampleInput(BaseModel):
    title: str = "Example"
    input_text: str = ""
    output_text: str = ""
    notes: str = ""
    position: int = 0
    references: list[str] = []
    result_urls: list[str] = []
    rating: int = Field(default=0, ge=0, le=5)
    generator_model: str = ""
    seed: str = ""
    generation_params: str = ""


class ProviderInput(BaseModel):
    name: str = Field(min_length=1)
    base_url: str = Field(min_length=1)
    model: str = Field(min_length=1)
    api_key: str = ""
    capabilities: list[str] = ["text"]
    provider_kind: str = Field(default="text", pattern="^(text|multimodal|image)$")
    verify_tls: bool = True
    image_input: str = Field(default="both", pattern="^(url|base64|both)$")
    is_default: bool = False
    reference_protocol: Literal['multipart_edit', 'json_url', 'none'] = 'multipart_edit'
    reference_endpoint: str = Field(default='/images/edits', pattern=r'^/[A-Za-z0-9_/-]+$')
    reference_field: str = Field(default='image', pattern=r'^[A-Za-z_][A-Za-z0-9_]*$')

    @field_validator('reference_field')
    @classmethod
    def validate_reference_field(cls, value: str) -> str:
        if value in {'model', 'prompt', 'n', 'size'}:
            raise ValueError('参考图参数名不能覆盖 model、prompt、n 或 size')
        return value


class ProviderModelsInput(BaseModel):
    provider_id: str | None = None
    base_url: str = Field(min_length=1)
    api_key: str = ""
    verify_tls: bool = True


class ReviseInput(BaseModel):
    prompt_id: str
    request: str = Field(min_length=1)
    provider_id: str | None = None
    images: list[str] = []
    image_urls: list[str] = []


class GenerateImageInput(BaseModel):
    prompt_id: str
    provider_id: str
    prompt: str = Field(min_length=1)
    reference_urls: list[str] = []
    reference_images: list[str] = []
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


@app.on_event("startup")
def startup() -> None:
    init_db()


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
        attachments = db.execute("SELECT relative_path FROM attachments WHERE prompt_id=? OR example_id IN (SELECT id FROM examples WHERE prompt_id=?)", (prompt_id, prompt_id)).fetchall()
        if not db.execute("SELECT 1 FROM prompts WHERE id=?", (prompt_id,)).fetchone():
            raise HTTPException(404, "Prompt 不存在")
        db.execute("DELETE FROM prompts WHERE id=?", (prompt_id,))
        for row in attachments:
            path = STORAGE_DIR / row[0]
            if path.is_file():
                path.unlink()
        return {"ok": True}


@app.post("/api/prompts/{prompt_id}/examples")
def add_example(prompt_id: str, payload: ExampleInput) -> dict[str, Any]:
    example_id = str(uuid.uuid4())
    with connect() as db:
        if not db.execute("SELECT 1 FROM prompts WHERE id=?", (prompt_id,)).fetchone():
            raise HTTPException(404, "Prompt 不存在")
        db.execute("INSERT INTO examples(id,prompt_id,title,input_text,output_text,notes,references_json,result_urls,rating,generator_model,seed,generation_params,position) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (example_id, prompt_id, payload.title, payload.input_text, payload.output_text, payload.notes, json.dumps(payload.references), json.dumps(payload.result_urls), payload.rating, payload.generator_model, payload.seed, payload.generation_params, payload.position))
        return dict(db.execute("SELECT * FROM examples WHERE id=?", (example_id,)).fetchone()) | {"attachments": []}


@app.put("/api/examples/{example_id}")
def update_example(example_id: str, payload: ExampleInput) -> dict[str, Any]:
    with connect() as db:
        db.execute("UPDATE examples SET title=?,input_text=?,output_text=?,notes=?,references_json=?,result_urls=?,rating=?,generator_model=?,seed=?,generation_params=?,position=? WHERE id=?", (payload.title, payload.input_text, payload.output_text, payload.notes, json.dumps(payload.references), json.dumps(payload.result_urls), payload.rating, payload.generator_model, payload.seed, payload.generation_params, payload.position, example_id))
        row = db.execute("SELECT * FROM examples WHERE id=?", (example_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Example 不存在")
        value = dict(row)
        value["references"] = json.loads(value.pop("references_json", "[]"))
        value["result_urls"] = json.loads(value.pop("result_urls", "[]"))
        return value | {"attachments": [attachment_dict(a) for a in db.execute("SELECT * FROM attachments WHERE example_id=?", (example_id,))]}


@app.delete("/api/examples/{example_id}")
def delete_example(example_id: str) -> dict[str, bool]:
    with connect() as db:
        paths = db.execute("SELECT relative_path FROM attachments WHERE example_id=?", (example_id,)).fetchall()
        if not db.execute("SELECT 1 FROM examples WHERE id=?", (example_id,)).fetchone():
            raise HTTPException(404, "Example 不存在")
        db.execute("DELETE FROM examples WHERE id=?", (example_id,))
        for row in paths:
            path = STORAGE_DIR / row[0]
            if path.is_file():
                path.unlink()
        return {"ok": True}


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
            compressed_path = destination.with_name(destination.stem + ".compressed" + destination.suffix)
            try:
                subprocess.run([FFMPEG, "-y", "-i", str(destination), "-c:v", "libx264", "-c:a", "aac", str(compressed_path)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=600)
                destination.unlink()
                compressed_path.rename(destination)
                compressed = True
                status = "completed"
            except (OSError, subprocess.SubprocessError):
                status = "failed"
        elif compress and media_type.startswith("image/"):
            status = "unsupported_without_image_encoder"
        else:
            status = "not_requested"
        db.execute("INSERT INTO attachments(id,prompt_id,example_id,relative_path,original_path,media_type,size_bytes,compressed,compression_status) VALUES(?,?,?,?,?,?,?,?,?)", (attachment_id, prompt_id, example_id or None, relative.as_posix(), original_path, media_type, destination.stat().st_size, compressed, status))
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
        row = db.execute("SELECT relative_path FROM attachments WHERE id=?", (attachment_id,)).fetchone()
        if not row:
            raise HTTPException(404, "附件不存在")
        db.execute("DELETE FROM attachments WHERE id=?", (attachment_id,))
        path = STORAGE_DIR / row[0]
        if path.is_file():
            path.unlink()
        return {"ok": True}


@app.get("/api/providers")
def list_providers() -> list[dict[str, Any]]:
    with connect() as db:
        return [{**dict(row), "api_key": "••••••••" if row["api_key"] else "", "capabilities": json.loads(row["capabilities"]), "verify_tls": bool(row["verify_tls"])} for row in db.execute("SELECT * FROM providers ORDER BY provider_kind, is_default DESC, name")]


@app.post("/api/providers")
def create_provider(payload: ProviderInput) -> dict[str, Any]:
    provider_id = str(uuid.uuid4())
    with connect() as db:
        if payload.is_default:
            db.execute("UPDATE providers SET is_default=0 WHERE provider_kind=?", (payload.provider_kind,))
        image_input = payload.image_input if payload.provider_kind == "multimodal" else "both"
        db.execute("INSERT INTO providers(id,name,base_url,model,api_key,capabilities,provider_kind,image_input,verify_tls,is_default) VALUES(?,?,?,?,?,?,?,?,?,?)", (provider_id, payload.name, payload.base_url.rstrip("/"), payload.model, payload.api_key, json.dumps(payload.capabilities), payload.provider_kind, image_input, payload.verify_tls, payload.is_default))
        db.execute("UPDATE providers SET reference_protocol=?,reference_endpoint=?,reference_field=? WHERE id=?", (payload.reference_protocol, payload.reference_endpoint, payload.reference_field, provider_id))
        row = dict(db.execute("SELECT * FROM providers WHERE id=?", (provider_id,)).fetchone())
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
        db.execute("UPDATE providers SET reference_protocol=?,reference_endpoint=?,reference_field=? WHERE id=?", (payload.reference_protocol, payload.reference_endpoint, payload.reference_field, provider_id))
        row = dict(db.execute("SELECT * FROM providers WHERE id=?", (provider_id,)).fetchone())
        row["capabilities"] = payload.capabilities
        row["api_key"] = "••••••••" if row["api_key"] else ""
        return row


@app.delete("/api/providers/{provider_id}")
def delete_provider(provider_id: str) -> dict[str, bool]:
    with connect() as db:
        if not db.execute("SELECT 1 FROM providers WHERE id=?", (provider_id,)).fetchone():
            raise HTTPException(404, "Provider 不存在")
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
    references = [url.strip() for url in payload.reference_urls if url.strip()]
    reference_count = len(references) + len(payload.reference_images)
    if payload.edit and not reference_count:
        raise HTTPException(400, "Edit 需要一张参考图")
    if reference_count > 1:
        raise HTTPException(400, "当前图片编辑协议仅支持一张参考图，请选择一个文件或一个 URL")
    if any(httpx.URL(url).scheme not in ("http", "https") for url in references):
        raise HTTPException(400, "参考图必须使用 HTTP 或 HTTPS URL")
    with connect() as db:
        provider = db.execute("SELECT * FROM providers WHERE id=?", (payload.provider_id,)).fetchone()
        if not provider or provider["provider_kind"] != "image":
            raise HTTPException(400, "请选择图片生成 provider")
        protocol = provider['reference_protocol']
        if reference_count and protocol == 'none':
            raise HTTPException(400, '当前 provider 未启用参考图')
        if payload.reference_images and protocol == 'json_url':
            raise HTTPException(400, '当前 provider 仅接受公网图片 URL')
        body = {"model": provider["model"], "prompt": payload.prompt, "n": 1}
        if payload.size:
            body['size'] = payload.size
        route = (provider['reference_endpoint'] if protocol == 'json_url' else '/images/edits') if reference_count else '/images/generations'
        endpoint = provider_endpoint(provider["base_url"]).removesuffix("/chat/completions") + route
        headers = {"Authorization": f"Bearer {provider['api_key']}", "User-Agent": "Mozilla/5.0"}
        try:
            with httpx.Client(verify=bool(provider["verify_tls"]), timeout=180) as client:
                if reference_count and protocol == 'multipart_edit':
                    if references:
                        with client.stream('GET', references[0], follow_redirects=True) as image:
                            image.raise_for_status()
                            media_type = image.headers.get('content-type', '').split(';')[0]
                            image_bytes = bytearray()
                            for chunk in image.iter_bytes():
                                image_bytes.extend(chunk)
                                if len(image_bytes) > 20 * 1024 * 1024:
                                    raise HTTPException(400, '参考图片不能超过 20 MB')
                    else:
                        try:
                            metadata, encoded = payload.reference_images[0].split(',', 1)
                            if not metadata.startswith('data:image/') or not metadata.endswith(';base64'):
                                raise ValueError('invalid image data URI')
                            if len(encoded) > 28 * 1024 * 1024:
                                raise ValueError('image too large')
                            image_bytes = base64.b64decode(encoded, validate=True)
                            media_type = metadata[5:-7]
                        except (ValueError, binascii.Error) as exc:
                            raise HTTPException(400, '上传图片格式错误或超过 20 MB') from exc
                    if len(image_bytes) > 20 * 1024 * 1024:
                        raise HTTPException(400, '参考图片不能超过 20 MB')
                    if not media_type.startswith("image/"):
                        raise HTTPException(400, "参考 URL 没有返回图片，请提供图片直链")
                    extension = mimetypes.guess_extension(media_type) or ".png"
                    response = client.post(endpoint, headers=headers, data={key: str(value) for key, value in body.items()}, files={"image": ("reference" + extension, bytes(image_bytes), media_type)})
                else:
                    if references:
                        body[provider['reference_field']] = references[0]
                    response = client.post(endpoint, headers=headers, json=body)
                response.raise_for_status()
                raw = response.json()
            logger.info("image request completed provider=%s model=%s route=%s protocol=%s references=%s", provider["name"], provider["model"], route, protocol, reference_count)
        except httpx.HTTPStatusError as exc:
            detail = exc.response.text[:800]
            logger.error("image request HTTP %s route=%s detail=%s", exc.response.status_code, route, detail)
            raise HTTPException(exc.response.status_code, f"生图或参考图读取失败：HTTP {exc.response.status_code} {detail}") from exc
        except (httpx.RequestError, ValueError) as exc:
            logger.exception("image request failed route=%s", route)
            raise HTTPException(502, "生图请求失败，请查看日志") from exc
        items = raw.get("data", []) if isinstance(raw, dict) else []
        return {"results": [{"url": item.get("url", ""), "b64_json": item.get("b64_json", "")} for item in items if isinstance(item, dict)]}
