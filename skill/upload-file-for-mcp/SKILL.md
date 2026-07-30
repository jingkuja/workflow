---
name: upload-file-for-mcp
description: 使用随附的 Python 脚本把本地文件上传到后台并取得未绑定的 file_key，再把 file_key 交给 omniq 或 jieshi MCP 的业务工具；首次消费时才绑定该 MCP 调用人和公司。用于任何需要将本地 Word、PDF、Markdown、文本、图片、音视频或其他文件导入、提交、处理的任务；MCP 本身不接收文件、Base64 或本地路径，HTML/JavaScript 页面仅作为人工上传备用方式。
---

# Upload File for MCP

先执行 Python 脚本上传文件，再调用业务 MCP。不要在 MCP 参数中放入文件内容、
Base64 或本地路径。

## 上传文件

1. 确认本机可执行 `python` 或 `python3`。脚本只使用 Python 标准库，不安装
   第三方包。
2. 以本 `SKILL.md` 所在目录为基准，解析脚本的绝对路径。对用户给出的每个本地
   文件，执行：

   ```bash
   python3 "/absolute/path/to/skill/scripts/upload_file.py" "/absolute/path/to/file"
   ```

   Windows 可执行：

   ```powershell
   py "C:\absolute\path\to\skill\scripts\upload_file.py" "C:\absolute\path\to\file"
   ```

3. 默认上传端点为
   `https://feishu.todoucloud.com/api/files/upload`，默认固定上传 Token 为
   `dev-file-upload-token-change-me`。不同部署使用 `--endpoint` 和 `--token`
   覆盖，或通过 `FILE_UPLOAD_TOKEN` 环境变量提供 Token。
4. 检查脚本退出码必须为 `0`，并从其 JSON 输出读取 `file_key` 和
   `original_filename`。上传失败时停止，不得臆造 `file_key`。

必须实际执行 [scripts/upload_file.py](scripts/upload_file.py)，不能只向用户展示
命令或要求用户自行上传。仅当 Python 不可用或脚本执行失败且无法修复时，才使用
浏览器备用方式。

## 调用业务 MCP

上传成功后，根据当前环境和用户指定的服务调用 `omniq` 或 `jieshi` MCP：

- 将 `file_key` 原样传给导入、提交或处理工具。
- 将脚本返回的文件名传给 `original_filename`（若业务工具有该字段）。
- 确认第一次使用此 key 的业务 MCP 身份就是最终归属人；第一次消费会原子绑定
  当前调用人和公司。
- 绑定后只允许同一人员和公司继续使用；其他人员不能接管或复用。
- 不调用名为 `upload_file` 的 MCP 工具。
- 不向 MCP 传固定上传 Token、人员 Token、文件内容、Base64 或本地路径。
- 如果业务 MCP 没有接收 `file_key` 的合适工具，停止并说明缺少的业务能力。

`file_key` 在首次消费前未绑定身份，但会过期。出现已绑定他人或已过期错误时，
重新上传生成新的 key，并由正确人员首先调用业务 MCP。

## 浏览器备用方式

当 Python 方式不可用时，打开
`https://feishu.todoucloud.com/file-upload`，或用现代浏览器打开
[scripts/upload-file.html](scripts/upload-file.html) 并填写完整上传端点。由
用户在文件选择框中选中文件并上传。页面不保存上传 Token，也不需要 Node.js、
Python、npm 或第三方包。
