# 常用修改建议

全局文字建议保存在本机数据库，不绑定 Prompt 条目或参考图片。

- 数据契约与接口：[app.py](../app.py) 的 `RevisionPresetInput` 及 `/api/revision-presets`。
- 保存、管理、快捷填入：[revision-presets.js](../static/revision-presets.js)。
- 修改与生图区的颜色、操作层级：[workflows.css](../static/workflows.css)。
- 数据及接口验收：[test_revision_presets.py](../tests/test_revision_presets.py)。
