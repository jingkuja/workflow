---
name: js
description: 使用随附的 Python 脚本把员工本地演播稿或其他任务文件上传到工作流后台并取得 file_key，再交给 jieshi 员工 MCP 提交。用于员工上传 Word、PDF、Markdown、文本或其他任务文件；MCP 不接收文件内容、Base64 或本地路径。
---

# JS 文件上传

先执行脚本上传文件，再调用 jieshi MCP。不要把文件内容、Base64 或本地路径放入
MCP 参数。

## 上传文件

固定配置如下：

- 上传接口：`https://feishu.todoucloud.com/api/files/upload`
- 上传 Token：`dev-file-upload-token-change-me`

以本 `SKILL.md` 所在目录为基准解析脚本绝对路径，并实际执行：

```bash
python3 "/absolute/path/to/js/scripts/upload_file.py" "/absolute/path/to/file"
```

Windows 执行：

```powershell
py "C:\absolute\path\to\js\scripts\upload_file.py" "C:\absolute\path\to\file"
```

脚本只使用 Python 标准库。检查退出码必须为 `0`，并从 JSON 输出读取 `file_key`
和 `original_filename`。失败时停止，不得臆造 `file_key`。

## 调用业务 MCP

上传成功后调用 jieshi MCP 的合适员工工具：

- 将 `file_key` 原样传给导入、提交或处理工具。
- 将脚本返回的文件名传给 `original_filename`（若业务工具有该字段）。
- 提交演播稿时调用 `submit_script_file`，同时传任务编号和新的幂等键。
- 不调用名为 `upload_file` 的 MCP 工具。
- 不向 MCP 传上传 Token、人员 Token、文件内容、Base64 或本地路径。
- 如果业务 MCP 没有接收 `file_key` 的合适工具，停止并说明缺少的业务能力。

`file_key` 首次消费时绑定当前员工和公司。出现已绑定他人或已过期错误时，重新
上传，并由正确员工首先调用 MCP。

## 浏览器备用方式

当 Python 方式不可用时，打开
`https://feishu.todoucloud.com/file-upload`，或用现代浏览器打开
[scripts/upload-file.html](scripts/upload-file.html) 并填写完整上传端点。由
用户选择文件并上传。
