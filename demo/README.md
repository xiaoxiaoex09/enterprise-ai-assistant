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

本项目已有一次受控的真实 SiliconFlow、DeepSeek 与原创 PDF 闭环成功记录，见 [验证记录](../docs/real-model-validation.md)。这只证明当次请求成功；不保证服务商持续可用，也不代表生产 Worker 进程监管、主机故障恢复或监控已验收。仅验证记录中的那一次完整回答经过人工逐项引用核对，其他回答仍需复核。

## 可选：真实模型最小联调

仓库根目录的 `live_model_smoke.py` **默认只做本地预检，不发起模型请求**：

```powershell
.\.venv\Scripts\python.exe live_model_smoke.py
```

程序不会自动加载 `.env`。需要真实联调时，在同一个 PowerShell 窗口交互输入密钥；以下命令不会把密钥文字写进命令历史，也不会把密钥写进仓库文件：

```powershell
$sfSecret = Read-Host 'SiliconFlow API Key' -AsSecureString
$dsSecret = Read-Host 'DeepSeek API Key' -AsSecureString
$env:SILICONFLOW_API_KEY = [System.Net.NetworkCredential]::new('', $sfSecret).Password
$env:DEEPSEEK_API_KEY = [System.Net.NetworkCredential]::new('', $dsSecret).Password
Remove-Variable sfSecret, dsSecret
.\.venv\Scripts\python.exe live_model_smoke.py --live --e2e
```

只有同时传入 `--live` 和已配置的两个环境变量，脚本才会调用真实模型；`--e2e` 另启用一次隔离的完整业务流。当前公开 PDF 为 1 页、2 个 chunk。上面的完整命令计划最多调用 4 次 SiliconFlow Embedding 和 2 次 DeepSeek Chat：各一次单接口检查、2 次 chunk 向量化、1 次已发布知识检索，以及 1 次带来源问答。现有模型客户端没有自动 HTTP 重试；实际费用、额度及服务可用性以服务商账户为准。不要反复运行该命令。

脚本核对 `BAAI/bge-m3` 返回 1024 维有限数值向量（[模型卡](https://huggingface.co/BAAI/bge-m3)），并区分认证、限流、服务端、网络和响应解析错误。仅在显式 `--live --e2e` 时输出原创公开 PDF 对应的完整问答与引用供人工核对；不打印认证头或密钥。不要将脚本改用于私人文档。

脚本只复用现有 `get_embedding()`、`chat_completion()`、`main:app` 对应的 ASGI 应用和 Worker 处理函数，不替换模型返回值。数据库、上传目录、测试 API Key 均在系统临时目录或内存中，完成后清理；**不会使用本地私人数据库或根目录 `manual.pdf`**。脚本运行的是单次 Worker 处理函数，不能代替对独立、持续运行 Worker 进程监管的验收。失败时只输出安全的错误类别/摘要，不输出认证头或密钥。

## 面试现场演示顺序与离线备用方案

按前面的启动命令分别运行 `main:app` 与独立 Worker，使用原创 PDF 和演示企业身份：管理员上传原始 `application/pdf`，展示 `document_id` / `job_id`、任务从 `queued` 到 `succeeded`、文档从 `processing` 到 `review`；管理员发布后由工程师提问，现场核对回答引用的文档 ID、页码和原文，再展示另一个企业查询该文档返回 404。解释 `lease_token` 防止过期 Worker 覆盖新执行者、租约过期恢复及尝试次数上限；说明实际主机故障恢复和生产进程监管尚未验收。不要把原创虚构指南当作真实维修建议。

若现场没有模型密钥、网络或额度，不要重复付费调用；改运行 `python -m unittest -v test_demo_flow test_worker_lease_integration` 展示隔离数据库中的流程和租约测试，并明确其中的 Embedding、DeepSeek 是 Mock。真实模型的历史证据与人工引用核对展示在验证记录中，离线备用演示不等于当场真实模型调用。
