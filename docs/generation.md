# 图片生成

请求入口与配置契约：[app.py](../app.py)。设置界面：[providers.js](../static/providers.js)。生成与 Example 保存界面：[generation.js](../static/generation.js)。验收：[test_generation.py](../tests/test_generation.py)。

Provider 的参考图提交方式是唯一配置源，不依据名称判断。上传型允许本地图片或公网 URL，URL 图片仅在内存中读取；URL 型按服务文档配置接口相对路径和 JSON 字符串参数名。暂支持单张参考图，不支持嵌套或数组 URL 协议。无参考图为普通生图。

参考图和结果仅驻留内存，保存 Example 是独立操作。保存时使用生成成功时的 Prompt 与参考图快照，生成模型默认为实际请求模型并允许修改。

接口依据：[服务公开 OpenAPI](https://agnes.dockerspeeds.asia/openapi.json)。当前编辑协议声明单个 image 文件，不支持多图时必须报错，禁止静默忽略。

尺寸契约见 `GenerateImageInput.size`，提交与快照逻辑见上述生成模块。Example 的 `generation_params.size` 记录请求尺寸，null 表示未指定，不代表实际输出尺寸。尺寸支持范围由所选服务和模型决定：[OpenAI 图片文档](https://developers.openai.com/api/docs/guides/image-generation)。
