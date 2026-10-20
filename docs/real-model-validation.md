# V1.0.1 真实模型联调记录

- 检查日期：2026-10-20（Asia/Shanghai）
- 基线：公开版本 `v1.0.0`；以下为 V1.0.1 发布前的受控验证记录，不代表生产部署验收。
- 结论：**真实模型冒烟测试及隔离环境中的 PDF RAG 业务链路通过**。此前的 BLOCKED 源于 Codex 进程未继承已设置的 Windows 用户环境变量；重启后，联调脚本的 Python 子进程均可读取两个变量。本记录不包含密钥值。

## 当前配置与请求范围

| 服务 | 项目现有配置 | 真实验证结果 |
| --- | --- | --- |
| SiliconFlow Embedding | `BAAI/bge-m3`，`https://api.siliconflow.cn/v1/embeddings`，60 秒超时 | 真实 HTTP 200；非空、有效数值向量，实测 1024 维 |
| DeepSeek Chat | `deepseek-chat`，`https://api.deepseek.com/chat/completions`，默认 60 秒超时，RAG 调用为 90 秒 | 真实 HTTP 200；返回非空文本 |
| PDF RAG | 公开的 `demo/assets/asteredge_x100_guide.pdf` | 隔离环境中上传、单次 Worker 处理、发布、真实检索及问答成功；引用原文位置核对通过 |

程序不会自动加载 `.env`。密钥必须由操作者在运行联调脚本的进程环境中设置；不要将密钥写入仓库、命令行参数、终端历史或报告。安全的交互式 PowerShell 配置方法见 `demo/README.md` 的“可选：真实模型最小联调”。

## 实际执行与证据边界

- ` .\.venv\Scripts\python.exe live_model_smoke.py `：退出码 0；公开 PDF 大小 3,191 字节，1 页、2 个 chunk；两个变量在子进程中均可见。预检未发起模型请求。
- ` .\.venv\Scripts\python.exe live_model_smoke.py --live `：退出码 0；SiliconFlow `BAAI/bge-m3` HTTP 200、1024 维、0.474 秒；DeepSeek `deepseek-chat` HTTP 200、非空文本、1.12 秒。此命令分别发起一次真实 Embedding 和 Chat 请求。
- ` .\.venv\Scripts\python.exe live_model_smoke.py --live --e2e `：退出码 0；直接模型检查再次通过，Embedding HTTP 200 / 1024 维 / 0.699 秒，Chat HTTP 200 / 0.698 秒；完整隔离流程耗时 3.051 秒。该次运行总计 4 次真实 Embedding、2 次真实 Chat 请求（包含直接模型检查），未超过预检预算。
- 端到端记录：`job_status=succeeded`，文档先进入 `review` 后由管理员发布为 `published`，2 个 chunk，`answer_status=answered`，2 条引用，跨企业隔离检查通过。未发布文档的知识不可由普通问答检索。引用指向公开 PDF 第 1 页，所引片段经规范化后可在原文中找到。
- 联调脚本对网络调用使用透传记录和请求预算限制，未替换真实模型返回。它使用临时 SQLite、临时上传目录和内存测试身份，不使用现有私人数据库或根目录 `manual.pdf`；仅直接执行一次 `process_next_queued_job()`，**未验证独立持续轮询 Worker、崩溃恢复、外部进程监管或真实主机故障**。
- 默认 `unittest` 中的模型测试使用 Mock / Patch，不应计为真实服务验证；只有上述 `--live` 命令构成真实请求证据。

## 2026-10-20 受控真实回答与人工引用核对

本次仅运行一次 `live_model_smoke.py --live --e2e`，退出码 0；执行时间 `2026-10-20T13:13:20Z`。该次运行总计 **4 次 Embedding、2 次 Chat**，均受脚本端点白名单和调用次数限制。独立 Embedding 检查为 HTTP 200、1024 维、0.494 秒；独立 DeepSeek 检查为 HTTP 200、0.707 秒。隔离业务流耗时 2.957 秒，任务 `succeeded`、文档 `review` 后发布为 `published`、2 个 chunk、`answered`、2 条引用；企业隔离通过。这不是持续 Worker 或生产监管验收。

实际问题：`What should I check for fault code E-17?`

完整模型回答（只涉及原创虚构设备，无私有资料）：

> 可能原因：传感器未获得稳定的 12 V DC 供电。[资料1]
>
> 按顺序检查建议：
> 1. 接触端子排前先断开电源。[资料1]
> 2. 确认 X100 输入端的供电电压在 11.5 至 12.5 V DC 范围内。[资料1]
> 3. 检查电源线缆和端子螺钉是否松动。[资料1][资料2]
> 4. 恢复供电并等待 30 秒后再检查 LED；若 E-17 仍存在，记录实测电压并升级至支持。[资料2]

真实返回的引用（文档 ID 1 仅属于本次临时数据库，两条均为原创 PDF 第 1 页）：

| 引用 | 原文片段 |
| --- | --- |
| 资料1 | `AsterEdge X100 - Demonstration Field Guide Original fictional sample for this open-source portfolio project. Model: X100 Version: 1.0 Document type: service guide Fault code E-17: amber status LED flashes twice every five seconds. Meaning: the sensor is not receiving a stable 12 V DC supply. Check 1: disconnect power before touching the terminal block. Check 2: verify the supply at the X100 input is 11.5 to 12.5 V DC. Check 3: inspect the power cable and terminal screws for looseness. Check 4: r` |
| 资料2 | `. Check 3: inspect the power cable and terminal screws for looseness. Check 4: restore power and wait 30 seconds before checking the LED. If E-17 remains, record the measured voltage and escalate to support. Safety: this fictional guide is a software demo, not real repair advice.` |

以上片段由脚本逐条确认存在于 `demo/assets/asteredge_x100_guide.pdf` 第 1 页；以下是**人工**逐项语义核对，不是通用自动语义判定器：

| 模型的独立事实或建议 | 支持原文（第 1 页） | 判定 |
| --- | --- | --- |
| E-17 表示供电不稳定，额定 12 V DC | `Meaning: the sensor is not receiving a stable 12 V DC supply.`（资料1） | SUPPORTED |
| 接触端子排前断电 | `Check 1: disconnect power before touching the terminal block.`（资料1） | SUPPORTED |
| 测量 X100 输入端，范围 11.5–12.5 V DC | `Check 2: verify the supply at the X100 input is 11.5 to 12.5 V DC.`（资料1） | SUPPORTED |
| 检查电源线缆与端子螺钉是否松动 | `Check 3: inspect the power cable and terminal screws for looseness.`（资料1、资料2） | SUPPORTED |
| 恢复供电，等 30 秒再检查 LED | `Check 4: restore power and wait 30 seconds before checking the LED.`（资料2） | SUPPORTED |
| E-17 仍存在时记录实测电压并升级支持 | `If E-17 remains, record the measured voltage and escalate to support.`（资料2） | SUPPORTED |

**结论仅限本次回答：**上述六项论断均可由所标注的第 1 页原文支持。程序本身只自动核验引用编号、文档、页码和片段存在；其他问题或后续模型回答仍需人工复核。该指南是虚构的软件演示，不是真实设备维修指令。

## 本次回归

| 实际命令 | 结果 |
| --- | --- |
| `.\.venv\Scripts\python.exe -m unittest discover -v` | 75 项：74 通过、0 失败、1 跳过；跳过项为本机无法创建目录符号链接 |
| `.\.venv\Scripts\python.exe -m compileall -q app main.py database.py pdf_rag_service.py demo test_demo_flow.py` | 通过 |
| `.\.venv\Scripts\python.exe -m pip check` | 通过 |
| 本地 ASGI 请求 `GET /health`、`GET /openapi.json` | 均为 200；OpenAPI 含 19 个操作及演示所需路由 |

## 尚未覆盖与下一步

本次证明真实模型可调用，且小型公开 PDF 的业务链路在临时数据库中闭环；不等于服务商持续可用或生产部署验收。持续 Worker 的进程监管、真实主机故障恢复和生产监控仍未验证。任何额外真实请求都应另行预算、明确启动，不能由默认测试自动触发。
