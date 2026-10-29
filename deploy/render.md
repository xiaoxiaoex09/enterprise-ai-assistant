# 通过 GitHub 部署到 Render

仓库根目录的 `render.yaml` 定义一个 Python Web Service：API 和持续入库 Worker 在同一实例运行，SQLite 与 PDF 共用 `/var/data` 持久盘。该配置使用 `1c-2g` 付费计算方案和 1 GB 持久盘；请在 Render 创建服务前核对账单。Render 免费 Web Service 无法挂载持久盘，因此不适合保存本系统的账号、文档和 PDF。

## 首次部署

1. 在 Render 用 GitHub 登录，并授权访问 `xiaoxiaoex09/enterprise-ai-assistant` 仓库。选择 **New → Blueprint**，选中该仓库的 `main` 分支并检查 `render.yaml` 中的资源和费用。
2. 在 Render 提示时填写 `PILOT_API_KEYS`、`SILICONFLOW_API_KEY` 和 `DEEPSEEK_API_KEY`。`PILOT_API_KEYS` 是 JSON 字符串，格式参见仓库根目录 `.env.example`；不要使用其中的占位符，也不要把真实值提交到 GitHub。Render 会自动配置数据库路径、PDF 目录、Worker 模式及健康检查。
3. 部署完成后打开 Render 提供的 `https://<service>.onrender.com/login`。用管理员 API Key 首次开通企业和管理员账号，再登录工作台。管理员可创建本企业成员。
4. 检查 `/health`、登录、型号创建、PDF 上传、Worker 入库、审核发布及引用问答。`render.yaml` 设置为 GitHub CI 通过后才自动部署后续提交。

## 迁移当前临时站点的数据

当前临时站点的 `data/public-pilot.db` 和 `storage/public/` 在本机，均被 Git 忽略。若要保留现有“知序演示企业”和管理员账号，部署时不要在 Render 上重新开通同名企业。先在维护窗口停止本机 API 与 Worker，使用 SQLite backup API 生成一致性数据库快照，同时复制 PDF 目录，再通过 Render 的 SSH/文件传输把数据库和 PDF 放入 Render 持久盘的 `/var/data/pilot.db` 与 `/var/data/storage/`，然后启动服务并验证登录及文档。不要把数据库、初始密码或模型密钥上传到 GitHub。

## 运行边界

Render 持久盘只能挂载到一个服务实例；因此这里将 API 和 Worker 合并监督，不能横向扩容。持久盘使数据跨重启和部署保留，但仍需独立备份与恢复演练。发布新版本前先保存数据库和 PDF 的私有备份；如果新版本不兼容，恢复代码与对应数据快照。真实设备维修建议仍需人工核对文档原文。
