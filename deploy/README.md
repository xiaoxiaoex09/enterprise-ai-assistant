# v1.0.1 单机部署

## 无域名临时公网预览

Windows 本机已安装 cloudflared 时，可在仓库根目录用 `.\.venv\Scripts\python.exe deploy/public_preview.py` 启动 API、独立 Worker 和 Cloudflare Quick Tunnel。进程保持运行期间，`data/public_url.txt` 会记录当前 HTTPS 页面地址；`data/public_access.json` 记录独立的管理员和工程师 API Key，两个文件均被 Git 忽略。该方式使用新的 `data/public-pilot.db` 与 `storage/public`，不会公开旧的 `tickets.db`。停止方式：在 `data/` 下创建 `public.stop` 空文件。Quick Tunnel 仅用于临时演示；地址在重启后变化，本机必须保持在线。长期稳定上线仍需有域名及服务器或支持持久化存储的托管平台。

打开 HTTPS 地址的 `/login`，首次开通时输入 `data/public_access.json` 中的管理员 API Key、企业名称和首个账号密码。之后用企业名称、用户名和密码登录；管理员可在工作台“账号管理”中创建本企业成员。密码以带盐 PBKDF2 哈希保存，登录会话为 12 小时，退出登录会撤销会话。普通成员无需持有 API Key。

前提：Linux 主机已安装 Docker Compose 插件；公开访问还需域名解析到该主机，并开放 80/443 端口。API 与 Worker 共用两个 Docker 数据卷，使用同一个 SQLite 文件和 PDF 存储目录。不要将 `.env` 提交到版本库。

1. 复制 `.env.example` 为 `.env`，生成两个各至少 32 位的随机 API Key，配置 `PILOT_API_KEYS` 的 JSON 映射，以及 `SILICONFLOW_API_KEY`、`DEEPSEEK_API_KEY`。示例占位符不能直接使用。可用 `python3 -c 'import secrets; print(secrets.token_urlsafe(32))'` 生成 Key。不要将真实密钥粘贴到工单或终端记录中。
2. 在仓库根目录运行 `docker compose up -d --build`。本机访问 `http://127.0.0.1:8000/app`，检查 `curl http://127.0.0.1:8000/health` 返回 `healthy`。API 仅绑定主机回环地址。
3. 公开上线时，在 `.env` 中设置 `SITE_DOMAIN=your.domain.example`，完成 DNS 解析后运行 `docker compose -f compose.yaml -f compose.public.yaml up -d --build`。访问 `https://你的域名/login`；Caddy 自动申请并续期 HTTPS 证书。正式环境只通过 HTTPS 输入账号密码或首次开通密钥。
4. 运行 `docker compose ps` 和 `docker compose logs --tail=100 api worker` 检查两个进程。使用管理员 Key 连接页面，初始化企业与型号，上传 PDF；确认任务完成、审核发布后再提问。模型 Key 缺失时只能测试页面和非模型接口。

备份前先停止 API 与 Worker 以取得一致的 SQLite 快照，再备份 `pilot-data` 和 `pilot-storage` 两个卷。升级前保留这两个卷和 `.env` 的私有副本；升级使用同一条 `docker compose ... up -d --build` 命令。回滚时切回先前镜像/代码并恢复对应数据卷快照，避免新旧 schema 混用。此模板面向单机试点，尚无自动备份、监控告警或多机高可用。
