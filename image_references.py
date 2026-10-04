import base64
import binascii
import mimetypes

import httpx
from fastapi import HTTPException


MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_TOTAL_BYTES = 80 * 1024 * 1024


def reference_files(client: httpx.Client, references: list[str], field: str) -> list[tuple[str, tuple[str, bytes, str]]]:
    files = []
    total = 0
    for index, reference in enumerate(references, 1):
        try:
            if reference.startswith('data:'):
                metadata, encoded = reference.split(',', 1)
                if not metadata.startswith('data:image/') or not metadata.endswith(';base64'):
                    raise ValueError('图片需要使用 base64 data URL')
                if len(encoded) > (MAX_IMAGE_BYTES + 2) // 3 * 4:
                    raise ValueError('单张图片不能超过 20 MB')
                image = base64.b64decode(encoded, validate=True)
                media_type = metadata[5:-7]
            else:
                with client.stream('GET', reference, follow_redirects=True) as response:
                    response.raise_for_status()
                    media_type = response.headers.get('content-type', '').split(';')[0].strip().lower()
                    image = bytearray()
                    for chunk in response.iter_bytes():
                        image.extend(chunk)
                        if len(image) > MAX_IMAGE_BYTES:
                            raise ValueError('单张图片不能超过 20 MB')
            if not image or not media_type.startswith('image/'):
                raise ValueError('未读取到有效图片，请检查图片直链或上传文件')
            if len(image) > MAX_IMAGE_BYTES:
                raise ValueError('单张图片不能超过 20 MB')
            total += len(image)
            if total > MAX_TOTAL_BYTES:
                raise ValueError('参考图片总大小不能超过 80 MB')
            extension = mimetypes.guess_extension(media_type) or '.png'
            files.append((field, (f'reference-{index}{extension}', bytes(image), media_type)))
        except (ValueError, binascii.Error) as exc:
            raise HTTPException(400, f'图{index}：{exc}') from exc
        except httpx.HTTPStatusError as exc:
            raise HTTPException(400, f'图{index}读取失败：HTTP {exc.response.status_code}') from exc
        except httpx.RequestError as exc:
            raise HTTPException(400, f'图{index}读取失败，请检查 URL 或网络连接') from exc
    return files
