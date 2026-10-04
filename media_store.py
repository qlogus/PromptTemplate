import json
import logging
import shutil
import subprocess
import uuid
from pathlib import Path

import httpx


class MediaStore:
    def __init__(self, root: Path, ffmpeg: str = 'ffmpeg'):
        self.root = root
        self.ffmpeg = ffmpeg
        self.logger = logging.getLogger('prompt_template')

    def probe(self, path: Path) -> dict:
        executable = Path(self.ffmpeg).with_name('ffprobe' + Path(self.ffmpeg).suffix) if Path(self.ffmpeg).parent != Path('.') else Path('ffprobe')
        try:
            result = subprocess.run([str(executable), '-v', 'error', '-show_entries', 'format=duration:stream=codec_type,width,height,r_frame_rate', '-of', 'json', str(path)], capture_output=True, text=True, timeout=30, check=True)
            raw = json.loads(result.stdout)
            stream = next((item for item in raw.get('streams', []) if item.get('codec_type') == 'video'), {})
            return {key:value for key,value in {'width':stream.get('width'), 'height':stream.get('height'), 'frame_rate':stream.get('r_frame_rate'), 'duration':raw.get('format', {}).get('duration')}.items() if value is not None}
        except (OSError, subprocess.SubprocessError, ValueError):
            return {}

    def compress(self, source: Path) -> dict:
        target = source.with_name(source.stem + '.compressed.mp4')
        partial = target.with_name(target.stem + '.partial.mp4')
        try:
            subprocess.run([self.ffmpeg, '-y', '-i', str(source), '-c:v', 'libx264', '-crf', '23', '-c:a', 'aac', '-movflags', '+faststart', str(partial)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=600)
            partial.replace(target)
            return {'compressed_relative_path':target.relative_to(self.root).as_posix(), 'compression_status':'completed'}
        except (OSError, subprocess.SubprocessError):
            self.logger.exception('video compression failed source=%s', source)
            partial.unlink(missing_ok=True)
            return {'compressed_relative_path':'', 'compression_status':'failed'}

    def download(self, client: httpx.Client, url: str, destination: Path, headers: dict | None = None, kind: str = 'video') -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = destination.with_suffix(destination.suffix + '.partial')
        try:
            with client.stream('GET', url, headers=headers, follow_redirects=True) as response:
                response.raise_for_status()
                content_type = response.headers.get('content-type', '').split(';')[0]
                if content_type and not (content_type.startswith(kind + '/') or content_type == 'application/octet-stream'):
                    raise ValueError('返回内容不是' + ('视频' if kind == 'video' else '图片'))
                with partial.open('wb') as output:
                    for chunk in response.iter_bytes():
                        output.write(chunk)
                        if kind == 'image' and output.tell() > 20 * 1024 * 1024:
                            raise ValueError('参考图超过 20 MB')
            if not partial.stat().st_size:
                raise ValueError('返回内容为空')
            partial.replace(destination)
        finally:
            partial.unlink(missing_ok=True)

    def copy_video(self, source: Path, prompt_id: str, example_id: str, original_path: str, compress: bool) -> dict:
        relative = Path(prompt_id) / example_id / (str(uuid.uuid4()) + '.mp4')
        destination = self.root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        asset = {'kind':'video', 'source':'/media/' + relative.as_posix(), 'original_path':original_path, 'relative_path':relative.as_posix(), 'compressed_relative_path':'', 'compression_status':'not_requested', 'metadata':self.probe(destination)}
        if compress:
            asset.update(self.compress(destination))
        return asset
