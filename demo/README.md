# V1.0 演示：原创 PDF → Worker → 审核 → 引用问答

演示材料：[AsterEdge X100 虚构设备指南](assets/asteredge_x100_guide.pdf)。内容由本项目原创编写，可公开再分发；它不是实际设备维修说明。生成脚本为 `demo/create_demo_pdf.py`。不要上传仓库根目录的私人 `manual.pdf`。

## 先跑无需密钥的自动化演示

```powershell
.\.venv\Scripts\python.exe -m unittest -v test_demo_flow
```

该测试在临时 SQLite 中执行真实 HTTP ASGI 路由、PDF 提取/分块、Worker、发布及双企业隔离；Embedding 和 DeepSeek 被模拟。它还验证发布前问答没有来源、发布后引用包含原文和页码。测试不触碰本地真实数据库。

## 交互式演示（会调用真实模型，可能产生费用）

先按根 [README](../README.md) 创建虚拟环境，并准备自己的 SiliconFlow、DeepSeek 密钥。以下在 PowerShell 的 **API 终端**执行，所有文件保存在被 `.gitignore` 排除的本地目录：

```powershell
New-Item -ItemType Directory -Force -Path .\data, .\storage | Out-Null
$adminA = & .\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(32))"
$engineerA = & .\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(32))"
$adminB = & .\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(32))"
$keys = @{}
$keys[$adminA] = @{ organization_id = 1; role = 'admin' }
$keys[$engineerA] = @{ organization_id = 1; role = 'engineer' }
$keys[$adminB] = @{ organization_id = 2; role = 'admin' }
$env:PILOT_API_KEYS = ConvertTo-Json -Compress -Depth 4 $keys
$env:DATABASE_PATH = Join-Path (Get-Location).Path 'data\pilot.db'
$env:DOCUMENT_STORAGE_PATH = Join-Path (Get-Location).Path 'storage'
$env:PDF_INGESTION_MODE = 'worker'
$env:SILICONFLOW_API_KEY = '<your-private-key>'
$env:DEEPSEEK_API_KEY = '<your-private-key>'
.\.venv\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8000
```

不要照抄密钥占位符。**Worker 终端**在同一仓库目录运行，设置相同数据库与存储路径，再持续启动；不能同时用不同数据库或默认 background 模式演示任务恢复。

```powershell
$env:DATABASE_PATH = Join-Path (Get-Location).Path 'data\pilot.db'
$env:DOCUMENT_STORAGE_PATH = Join-Path (Get-Location).Path 'storage'
$env:PDF_INGESTION_MODE = 'worker'
$env:SILICONFLOW_API_KEY = '<your-private-key>'
.\.venv\Scripts\python.exe pdf_rag_service.py work --poll-seconds 3
```

第三个 **请求终端**需要把 `$adminA`、`$engineerA`、`$adminB` 安全地传入本终端（不要写入仓库文件）。下面命令中的变量须使用 API 终端生成的值：

```powershell
$base = 'http://127.0.0.1:8000'
Invoke-RestMethod "$base/health"
Invoke-RestMethod "$base/v1/organization" -Method Post -Headers @{ 'X-API-Key' = $adminA } -ContentType 'application/json' -Body '{"name":"Demo Enterprise A"}'
Invoke-RestMethod "$base/v1/organization" -Method Post -Headers @{ 'X-API-Key' = $adminB } -ContentType 'application/json' -Body '{"name":"Demo Enterprise B"}'
$modelA = Invoke-RestMethod "$base/v1/device-models" -Method Post -Headers @{ 'X-API-Key' = $adminA } -ContentType 'application/json' -Body '{"model_code":"X100","name":"Demo Sensor"}'
$modelB = Invoke-RestMethod "$base/v1/device-models" -Method Post -Headers @{ 'X-API-Key' = $adminB } -ContentType 'application/json' -Body '{"model_code":"X100","name":"Other Sensor"}'
$pdf = (Resolve-Path '.\demo\assets\asteredge_x100_guide.pdf').Path
$uploadUrl = "$base/v1/documents?family_key=asteredge-x100&version=1.0&title=AsterEdge-X100&device_model_ids=$($modelA.id)"
$upload = Invoke-RestMethod $uploadUrl -Method Post -Headers @{ 'X-API-Key' = $adminA } -ContentType 'application/pdf' -InFile $pdf
$upload
```

上传应返回 `202`、`document_id`、`job_id`、`queued`；此接口接收**原始 PDF 字节**，不能用 `multipart/form-data`。接着在请求终端执行：

```powershell
$question = @{ device_model_id = $modelA.id; question = 'What should I check for fault code E-17?' } | ConvertTo-Json -Compress
Invoke-RestMethod "$base/v1/answers" -Method Post -Headers @{ 'X-API-Key' = $engineerA } -ContentType 'application/json' -Body $question
Invoke-RestMethod "$base/v1/ingestion-jobs/$($upload.job_id)" -Headers @{ 'X-API-Key' = $adminA }
Invoke-RestMethod "$base/v1/documents/$($upload.document_id)" -Headers @{ 'X-API-Key' = $engineerA }
```

第一次问答应为 `insufficient_evidence`。等 Worker 把任务置为 `succeeded`、文档置为 `review` 后，再问一次仍应没有来源。管理员审核原文后发布：

```powershell
Invoke-RestMethod "$base/v1/documents/$($upload.document_id)/publish" -Method Post -Headers @{ 'X-API-Key' = $adminA }
$answer = Invoke-RestMethod "$base/v1/answers" -Method Post -Headers @{ 'X-API-Key' = $engineerA } -ContentType 'application/json' -Body $question
$answer | ConvertTo-Json -Depth 6
```

真实模型措辞不固定；检查 `status` 是 `answered` 还是安全退回 `needs_review` / `insufficient_evidence`。只有 `answered` 时才应有与答案编号相符的 `references`，包含文档 ID、页码和原文。模型没有给出有效引用时不要假装问答成功。

最后验证隔离：以 `$adminB` 查询 A 的文档与任务应返回 404；以 B 的设备型号提问不应引用 A 的资料。重复运行时数据库会保留旧记录，请换新的 `family_key`/`version` 或使用新的私有演示数据库；不要删除真实数据来清理演示。

```powershell
Invoke-RestMethod "$base/v1/documents/$($upload.document_id)" -Headers @{ 'X-API-Key' = $adminB }
Invoke-RestMethod "$base/v1/ingestion-jobs/$($upload.job_id)" -Headers @{ 'X-API-Key' = $adminB }
$otherQuestion = @{ device_model_id = $modelB.id; question = 'What should I check for fault code E-17?' } | ConvertTo-Json -Compress
Invoke-RestMethod "$base/v1/answers" -Method Post -Headers @{ 'X-API-Key' = $adminB } -ContentType 'application/json' -Body $otherQuestion
```

本项目尚未验证真实外部 API 在你的网络和账户上的可用性、费用、引用质量或生产 Worker 进程监管。
