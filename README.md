# 向前 · 个人求职工作台

本地运行的中文个人求职应用。FastAPI 后端、SQLite WAL 持久化、无构建依赖的响应式前端。Windows 可用本机 OCR，无需将截图发给外部服务。初始画像在 `config/default_profile.json`，首次启动写入数据库，此后由「我的资料」编辑。

## 启动

已在当前机器建立 `.venv` 并安装依赖。PowerShell 在项目目录运行：

```powershell
.\start.ps1 -Background
```

访问 <http://127.0.0.1:8765>。首次随机登录密码保存在 **`data/access-password.txt`**，可用本机编辑器打开。密码不会通过网页接口公开。也可在 `.env` 设置至少12字符的 `APP_PASSWORD`。不要在服务已运行时重复启动。

前台运行方便看日志：

```powershell
.\start.ps1
```

跨平台手动启动（Python 3.11+，推荐3.13）：

```sh
python -m venv .venv
# macOS/Linux:
source .venv/bin/activate
python -m pip install -r requirements.lock
python run.py
```

Windows直接使用上面的PowerShell启动脚本，或用 `.venv\Scripts\python.exe` 替代最后两步的 `python`。Windows 截图识别需要已安装 Windows 中文/英文 OCR 语言组件；其他平台可用系统 Tesseract。缺少识别引擎时页面会保留原文件并提示补充文字。

关闭网页后，后台进程仍会运行任务。电脑关机、休眠或后端停止时不能获取岗位；重启后当天超过设定时间且尚未运行，会补跑一次。默认北京时间 **09:00**，可在设置修改。采用一个应用进程/一个后台工作线程，不要使用多个 Uvicorn workers。长期无人值守可把 `run.py` 配置为操作系统启动服务；本次没有擅自安装开机服务。

## 第一次使用

1. 在「我的资料」上传 PDF/DOCX。系统保留原件并提取待核对草稿，不覆盖已保存基础履历。确认姓名、经历、技能层次和项目边界后保存。每项技能和项目有独立确认标记；“学习过”和“尚未接触”不会变成实践能力。
2. 「导入岗位」支持合并文字、链接、1–2张截图、PDF/DOCX/TXT/Markdown。解析后直接修改高亮的不确定字段。原始提交和文件保留，可下载核对。
3. 打开岗位工作区，查看逐条“岗位原文 → 经历证据 → 匹配/未知”的分析，再标记「可投」。五项准备任务独立运行、保存，失败/缺资料可单项重试。已完成任务也可按需重新生成版本。
4. 有已确认姓名、画像和具体项目后，生成中文简历；项目与技能根据岗位相关性重排，真实背景和措辞不被伪造。预览、DOCX、PDF使用同一组内容块。英文翻译需要AI并须逐项核对后导出。手动编辑另存版本，不覆盖基础履历或旧版本。
5. 文字面试每次一问，可中英、HR/技术/项目深挖/综合，保留记录。无密钥时明确使用本地分支练习，有密钥并开启增强后可由模型结合上一回答追问。

仅生成准备材料不会标记「已投递」。实际投递日期、状态、下一步行动、备注由用户维护。

## 可选配置

复制 `.env.example` 为 `.env`，修改后重启。不要提交 `.env`、数据库、上传文件或密码文件到 Git。

| 配置 | 用途 |
|---|---|
| `APP_PASSWORD` | 本地访问密码；不设时生成随机密码并保存在私有数据目录 |
| `APP_HOST` / `APP_PORT` | 默认 `127.0.0.1` / `8765` |
| `DATA_DIR` | 可选独立数据目录，默认项目内 `data/` |
| `AI_API_KEY` | 服务端 AI 密钥，前端无密钥输入/读取接口 |
| `AI_MODEL` / `AI_BASE_URL` | 可留空，使用设置页面的模型和HTTPS兼容接口；环境变量优先 |
| `AI_RESPONSE_FORMAT` | 默认 `json_schema`；仅不支持严格模式的兼容服务用 `json_object`，仍经过Pydantic校验 |
| `SMTP_HOST` / `SMTP_PORT` | 邮件主机和端口，默认587 |
| `SMTP_FROM` / `SMTP_USER` / `SMTP_PASSWORD` | 发件人和认证信息 |
| `SMTP_SECURITY` | `starttls`（默认）或 `ssl` |
| `COOKIE_SECURE` | 远程部署在HTTPS反向代理后设 `true` |

AI配置后，在设置中打开「AI增强」。调用的是服务端 OpenAI-compatible Chat Completions 接口，结构化输出验证参考 [官方 Structured Outputs 文档](https://developers.openai.com/api/docs/guides/structured-outputs)。导入内容仅作为不可信资料，不执行其中指令。AI报告中引用不存在的JD片段、经历ID，或把未确认经历当明确事实时，会拒绝结果并允许重试。没有真实密钥时不声称AI已验证成功。

不开启AI也能进行导入、OCR、编辑、去重、状态管理、规则匹配、中文事实简历、沟通问题、项目素材、准备计划和本地分支面试。模型未配置时英文翻译/AI增强明确待配置。英文版本核对是事实审查步骤，不能把翻译输出直接当新履历。

邮件收件人及启用开关在页面设置。未配置SMTP仍会生成站内日报。邮件只含岗位与链接，不含简历或个人经历。站内推荐记录和发送账本持久化；重复运行不重复推荐同一岗位。SMTP在极端中断下无法提供事务性的“恰好一次”保证，因此发送状态不明时保留错误并**不自动重发**，避免重复邮件；需人工确认送达情况。

## 来源与事实边界

完整官方依据和2026-09-27实际获取结果见 [docs/SOURCES.md](docs/SOURCES.md)。

- 已实现并实测：Greenhouse公开职位API、Lever公开 postings API、公开网页JSON-LD JobPosting。
- 默认启用 Figure 公开招聘接口，属于海外具身/机器人机会。按岗位名称和画像匹配过滤，不能因为公司做机器人就推荐其电气、线束、财务或安保职位。可配置其他公司；深圳覆盖仍需扩展公司名单或手动导入。
- BOSS直聘、猎聘、LinkedIn：当前没有获授权的自动职位读取连接器，页面明确「暂不支持」。可组合导入截图、文字和文件。没有绕过登录、验证码或robots限制。
- 未知发布时间明确标注；首次发现和最近核验独立。访问失败记录失败原因，不能因此关闭岗位。仅公开有效性信息或用户明确确认用于关闭。
- 薪资按人民币固定月薪判断：`30–60K·14薪`是“可能符合”；年包、外币、含绩效或口径不明为“信息不足”；额外薪数与固定月薪分开。可调整目标，低于目标的岗位不会挤入推荐。
- 合并使用来源URL或已知公司/标题/城市的保守身份；相似但不相同的标题不强行合并。冲突字段保留候选和原始输入，供核对。

## 数据、隐私与维护

`data/workbench.sqlite3` 为数据库；`data/uploads/` 为原件，`data/exports/` 为导出缓存。文件只经登录会话下载，不挂载为静态目录。HttpOnly/SameSite会话、CSRF与同源写入保护、登录限流、受限文件类型/体积、解压炸弹限制、公开抓取SSRF/DNS重绑定防护均已实现。默认仅本机监听；如需跨设备访问，请先配置HTTPS代理和访问边界。文件在磁盘上未额外加密，保管好本机账户和备份。

备份时建议停服务后复制整个 `data/`（包括SQLite可能的WAL文件），或用SQLite backup API在线备份。不能只拷贝正在写入的 `.sqlite3` 而忽略WAL。恢复使用同一目录。

模块边界：

| 文件 | 职责 |
|---|---|
| `app/main.py` | 认证、API、上传、工作区与基础履历写入 |
| `app/store.py` | SQLite、事务合并、队列、版本与恢复 |
| `app/parsing.py` | OCR/文档提取、JD字段、重叠去重、薪资口径 |
| `app/sources.py` | 公开来源适配、安全获取 |
| `app/analysis.py` | 证据匹配、材料、准备计划、简历重排 |
| `app/ai.py` | 服务端模型接口、结构校验 |
| `app/exports.py` | 同源预览块、真实DOCX/PDF导出 |
| `app/worker.py` | 持久任务、每日调度、筛选、日报去重 |
| `app/interview.py` | 保存对话、上一轮追问、反馈 |
| `app/notifications.py` | 加密SMTP与发送记录 |
| `static/` | 中文响应式工作界面 |

关键算法在代码中有注释和例子；没有引入多服务或复杂编排。部署只有一个后端进程和数据库文件。

## 验证

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q
node --check static/app.js
```

所有验收样例使用独立临时库，真实界面不预置虚构岗位。浏览器验收服务器为 `scripts/ui_test_server.py`，专用端口8766、`tmp/ui-acceptance/` 数据目录、明确测试密码；不要把测试服务器开放到公网。

验收记录、已验证能力及仍依赖外部配置的项目见 [docs/VERIFICATION.md](docs/VERIFICATION.md)。
