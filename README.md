# Prompt Template Manager

本地单用户网页应用，用于管理 pose 与 action prompt、examples、附件和 OpenAI-compatible providers。

## 启动

在本目录执行：

```powershell
python -m pip install -r requirements.txt
python -m uvicorn app:app --host 127.0.0.1 --port 9001 --reload
```

浏览器打开 `http://127.0.0.1:9001`。

FFmpeg 可通过 `PROMPT_TEMPLATE_FFMPEG` 指定路径。视频压缩失败时仍保留原视频，条目中会显示压缩状态。
