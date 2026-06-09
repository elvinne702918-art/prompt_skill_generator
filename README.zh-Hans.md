# Dify 提示词技能生成器

[English README](README.md)

Prompt Skill Generator 是一个 Dify 插件，用于运行可复用的 Prompt Skill。它支持内置模板、自定义 Skill 内容、上传 Skill ZIP 包、持久化注册 Skill 预设，以及带文本、图片、文档附件的 Skill 执行。

## 功能

- 直接执行选中的 Skill，并返回最终结果。
- 将选中的 Skill 转换为可复用提示词。
- 注册一次 Skill ZIP 包，在后续节点中复用。
- 支持专用附件节点，处理文本文件、图片和文档。
- 支持 Dify 平台模型、OpenAI 兼容外部 API 和 Google Gemini。
- 默认隐藏 `<think>...</think>` 等常见思考块。
- 在调用模型前拦截超限图片。

## 工具

### Skill 管理

上传一个 Skill ZIP 包，并保存为 Dify 插件存储中的可复用 Skill 预设。

ZIP 包必须包含 `SKILL.md`。如果包内有 `references/` 目录，其中的文本文件会作为 Skill 的参考资料一起使用。

### 生成/执行 Skill

在不需要附件时，运行内置 Skill、自定义 Skill、上传 Skill 或已注册 Skill。

### 带附件执行 Skill

使用附件或 Base64 图片输入运行 Skill。上传文件使用 `附件 1` 到 `附件 5` 字段。旧工作流中已有的 `attachment_6` 到 `attachment_10` 后端仍可读取。

支持的输入包括：

- 文本文件
- 一张或多张图片
- 文档
- Base64 图片 data URL 或原始 Base64 图片

图片限制会在调用模型前检查：

- 最大文件大小：`10MB`
- 最大宽度或高度：`2048px`

Dify 平台模型调用和外部 HTTP API 调用都会执行同样的校验。

## 外部 API 模式

填写 `External API Key` 后，插件会使用 `External API Type` 指定的外部 API 类型。

`OpenAI Compatible` 会调用：

```text
POST {api_base}/chat/completions
Authorization: Bearer <api_key>
```

如果 `External API Base URL` 留空，默认使用：

```text
https://api.openai.com/v1
```

`Google Gemini` 会调用：

```text
POST {api_base}/models/{model}:generateContent
x-goog-api-key: <api_key>
```

如果 `External API Base URL` 留空，Google Gemini 默认使用：

```text
https://generativelanguage.googleapis.com/v1beta
```

插件默认复用 Dify 模型选择器中的模型名作为外部 `model` 字段。自定义供应商需要确保模型名和远端 API 匹配。

如果要覆盖实际发送给外部 API 的模型名，可以填写 `External Model Name`。这对 Google Gemini 很有用，因为 Dify 模型选择器里不一定有 `gemini-*` 模型名。

Gemini 图片输入也可以不用 Dify 文件附件，而是填写 `Base64 Images` 字段。每行粘贴一个 data URL 或原始 Base64：

```text
data:image/png;base64,iVBORw0KGgo...
```

也支持 JSON 数组：

```json
[
  {
    "filename": "product.png",
    "mime_type": "image/png",
    "base64": "iVBORw0KGgo..."
  }
]
```

## 本地开发

安装开发依赖：

```bash
pip install -r requirements-dev.txt
```

`requirements-dev.txt` 会从 Python 包索引安装测试依赖。`requirements.txt` 用于 Dify 运行时打包，期望本地存在 `wheels/` 目录。

运行测试：

```bash
python -m unittest tests.test_generate_prompt
```

## 离线运行依赖

发布包可以在 `wheels/` 下包含 Linux x86_64 Python 3.12 wheel 文件。`requirements.txt` 会从本地 `wheels/` 安装依赖，这样 Dify 启动插件时不需要联网下载运行依赖。

源码仓库不提交 wheel 文件。如果本地缺少 `wheels/`，打包前重新生成：

```bash
python -m pip download -d wheels -i https://pypi.tuna.tsinghua.edu.cn/simple --only-binary=:all: --platform manylinux2014_x86_64 --python-version 312 --implementation cp --abi cp312 dify_plugin==0.5.1 httpx==0.28.1
```

Marketplace 或签名分发应使用官方 Dify 插件工具打包。本工作区里的本地辅助打包脚本只适合私有测试，不会生成可信 Marketplace 签名。

## 私有安装说明

自托管 Dify 如果启用了强制签名校验，可能会拒绝本地打包的 `.difypkg`。可选处理方式：

- 走官方 Marketplace 发布流程。
- 配置受支持的第三方签名校验。
- 只在可信自托管环境中关闭强制签名校验。

## 发布检查

- 确认 `manifest.yaml`、provider 和 tool YAML 中的 `author` 已设置为发布账号。
- 检查 `PRIVACY.md` 中的联系和所有者信息。
- 运行单元测试。
- 使用官方 Dify 工具打包。
- 不要提交生成的 `.difypkg`、`__pycache__` 或 `wheels/`。

## Logo 资源

仓库展示用品牌资源放在 `logo/`。Dify 插件实际使用的图标放在 `_assets/`。
