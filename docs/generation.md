# 图片生成

请求入口与配置契约：[app.py](../app.py)。设置界面：[providers.js](../static/providers.js)。生成与 Example 保存界面：[generation.js](../static/generation.js)。验收：[test_generation.py](../tests/test_generation.py)。

Provider 的参考图配置见 `ProviderInput`：数量上限是能力的唯一来源（0 不支持、1 单图、大于 1 多图），与传输方式、参数名、单值或列表格式分开。原 Provider 保留原有单图或禁用行为，不依据模型名称推断能力。

有序参考图控件：[generation-references.js](../static/generation-references.js)。上传准备与资源限制：[image_references.py](../image_references.py)。上传型支持文件与 URL 混排，URL 图片在内存中读取；URL 型支持字符串或字符串数组，不支持嵌套对象。请求的 `references` 顺序即界面编号，超限和失败不得截断或跳过。

参考图和结果仅驻留内存，保存 Example 是独立操作。保存时使用生成成功时的 Prompt 与参考图快照，生成模型默认为实际请求模型并允许修改。

Base64 媒体预览与打开链接使用 [media-urls.js](../static/media-urls.js) 管理的临时 Blob 地址；持久化仍使用原始结果数据，不保存 Blob 地址。Example 的结构与媒体节点装配入口为 `exampleHtml` / `hydrateExampleMedia`，见 [index.html](../static/index.html)。

协议依据：[OpenAI 图片文档](https://developers.openai.com/api/docs/guides/image-generation)、[服务公开 OpenAPI](https://agnes.dockerspeeds.asia/openapi.json)。按实际服务配置，能力声明不代表服务端支持已经验证。多图验收：[test_multi_reference.py](../tests/test_multi_reference.py)。

尺寸契约见 `GenerateImageInput.size`，提交与快照逻辑见上述生成模块。Example 的 `generation_params.size` 记录请求尺寸，null 表示未指定，不代表实际输出尺寸。尺寸支持范围由所选服务和模型决定：[OpenAI 图片文档](https://developers.openai.com/api/docs/guides/image-generation)。

生图日志记录请求尺寸、Provider、模型、路由、参考图数量、HTTP 状态、返回数量和响应字段；Base64 返回还会解析 PNG/JPEG/WebP 的实际尺寸。仅返回 URL 时不下载结果，实际尺寸显示为 `not_available`，应在浏览器通过图片的 intrinsic size 或下载文件确认。

图片生成 / Edit 的本地上游请求等待上限由 `app.py` 的 `IMAGE_REQUEST_TIMEOUT` 统一控制，当前为 300 秒。该设置不改变上游网关（例如 Cloudflare）的独立超时限制。

生图 Prompt 模板使用独立的 `generation_presets` 与 `/api/generation-presets`，界面见 `generation-presets.js`。它与 AI 修改建议模板分开管理，快捷填入不会自动请求或生成。
