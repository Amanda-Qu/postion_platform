# 岗位来源：接入范围与实测记录

核验日期：**2026-09-27（北京时间）**。以下为开发时对公开接口、robots.txt 和公开招聘页的实际 HTTP 访问记录；不是对未来可用性的保证，也不是已向招聘方投递。运行时状态与失败原因由应用保存。测试里的固定样例只是离线协议测试，不进入真实岗位库。

## 当前接入

| 来源 | 获取方式 | 真实核验结果 | 默认行为 |
| --- | --- | --- | --- |
| Figure（Greenhouse） | 官方公开 Job Board GET API | HTTP 200，97 个公开职位，包含 Helix AI Engineer, Perception / Robot Learning / Reinforcement Learning，主要地点 San Jose, CA | **启用**，海外岗位须单列，不能当作深圳岗位或薪资已达标 |
| 其他公司 Greenhouse | 配置 board slug | Anthropic 实测 HTTP 200，618 个职位；详情响应约 8.9 MB | 未配置、未启用，不默认导入所有海外公司 |
| Lever | 官方公开 Postings GET API | `palantir?mode=json&limit=2` HTTP 200，返回 2 个公开职位；`lever` 返回 HTTP 200、空列表 | 未配置、未启用；需填写 site slug 与真实公司名 |
| 公司公开 JobPosting 页面 | HTTPS HTML 中的 JSON-LD，抓取前读取 robots | Palantir 的 Lever 托管公开岗位页 HTTP 200，1 个 JobPosting；页面含真实公司名、地址和描述 | 配置后启用，仅解析公开结构化信息 |
| BOSS直聘 | 用户授权上传的截图、文本、文件及保留链接 | robots.txt HTTP 200，对搜索查询、推荐等路径有限制；本项目未取得或接入可用的官方个人求职搜索接口 | 暂不支持自动获取；手动导入可用 |
| 猎聘 | 用户授权手动导入 | robots.txt HTTP 200，限制查询参数等；公开企业合作能力不代表本项目拥有个人职位搜索权限 | 暂不支持自动获取；手动导入可用 |
| LinkedIn | 用户授权手动导入 | robots.txt HTTP 200，明示自动访问须获得许可；官方 Talent API 要申请批准，不能视为开放职位搜索接口 | 暂不支持自动获取；手动导入可用 |

Figure 的公开接口已按应用实际 `safe_fetch` 与 `fetch_source` 连接实现复验，响应约 542 KB、97 条，board 元信息公司名为 Figure；完整适配调用约 4 秒。Lever 实际适配器分页获取了 Palantir 的 321 条公开职位，JobPosting 适配器实际返回上述页面的 1 条职位。这些核验未向应用库注入测试岗位。相关职位示例只用于确认来源与方向存在，不代表适配用户地点、签证、薪资和资历要求。没有足够适合的岗位时，日报应为空或少量，而不是把全部来源岗位当推荐。

## 核验地址与依据

- [Figure 公开职位 API](https://boards-api.greenhouse.io/v1/boards/figureai/jobs?content=true)，HTTP 200；[Perception 岗位](https://job-boards.greenhouse.io/figureai/jobs/4007375006)、[Robot Learning 岗位](https://job-boards.greenhouse.io/figureai/jobs/4649851006)。岗位名称和地点来自该 API。
- [Greenhouse 官方 Job Board API 文档](https://docs.greenhouse.io/job-board.html)明确公开读取 GET 不要求身份认证。[Anthropic board 元信息](https://boards-api.greenhouse.io/v1/boards/anthropic) HTTP 200；[完整职位](https://boards-api.greenhouse.io/v1/boards/anthropic/jobs?content=true) HTTP 200。
- [Lever 官方 Postings API 文档](https://github.com/lever/postings-api)说明其 published 职位公开读取与分页方式。[Palantir 两条结果查询](https://api.lever.co/v0/postings/palantir?mode=json&limit=2) HTTP 200；[Lever 自身 board](https://api.lever.co/v0/postings/lever?mode=json&limit=2) HTTP 200、`[]`。空列表是合法成功结果，不能编造岗位。
- [Lever robots](https://jobs.lever.co/robots.txt) HTTP 200，允许公开路径并要求 1 秒抓取间隔；[实际含 JobPosting 的公开岗位页](https://jobs.lever.co/palantir/6ed76ce8-4156-4b60-b120-403538bd66cd) HTTP 200、1 个 JSON-LD JobPosting，属于 Palantir Technologies。
- [Greenhouse 页面 robots](https://job-boards.greenhouse.io/robots.txt) HTTP 200。[实测 Anthropic 页面](https://job-boards.greenhouse.io/anthropic/jobs/4461450008) HTTP 200，但未找到 JSON-LD 脚本，因此该页面不假装已成功解析，应使用该公司的公开 API 或手动导入。
- [BOSS robots](https://www.zhipin.com/robots.txt)、[BOSS 平台协议入口](https://www.zhipin.com/web/common/protocol/index.html)。仅确认本项目未接入获授权的自动职位读取渠道，**不声称平台不存在任何商业合作接口**。
- [猎聘 robots](https://www.liepin.com/robots.txt)、[猎聘官方合作入口](https://wow.liepin.com/t1007623/index.html)。企业发布/候选人整合接口与个人获取全站岗位不是同一种权限。
- [LinkedIn robots](https://www.linkedin.com/robots.txt)、[LinkedIn 官方 API 权限说明](https://learn.microsoft.com/en-us/linkedin/shared/authentication/getting-access)。不会用登录 Cookie、验证码处理、伪装浏览器或私有接口绕过限制。

另外测试过 `skildai`、`physicalintelligence`、`intrinsic`、`physicalintelligence1`、`1x`、`dexterity` 的 Greenhouse board slug，均 HTTP 404；Lever `scaleai` 也 HTTP 404。**仅表示所试 slug 不存在，不能推断公司没有招聘或岗位已经关闭。**

## 配置示例

```json
{"kind":"greenhouse","board":"figureai","company":"Figure","enabled":true}
```

```json
{"kind":"lever","site":"palantir","company":"Palantir Technologies","enabled":false}
```

```json
{"kind":"public_page","url":"https://careers.example.com/a-real-job","enabled":false}
```

第三个是**格式示例**，不是实际岗位。接口函数 `fetch_source(config)` 返回统一岗位字段；缺失字段为“未知”，发布时间未知为 `null`。Greenhouse 只采用 `first_published`，**不会把 `updated_at` 当发布时间**；Lever 未提供发布时间时保持未知。JSON-LD 只采用 `datePosted`。薪资保留货币与周期，年包不除以 12；正文中的复杂薪资由后续解析核对，不在来源连接器猜测。

## 安全与失败语义

- 只读取 HTTPS 标准 443 端口，拒绝凭据 URL、IP 字面量、混淆 IP、本地域名、私网和元数据地址。域名的每一个 DNS 结果都必须是公网地址。
- TLS 连接固定到已检查的公网 IP，同时仍校验原主机名的证书；避免“先检查域名、连接时重新解析”带来的 DNS 重绑定。每次重定向重新检查，最多 3 次。自动读取器完全拒绝 BOSS、猎聘与 LinkedIn 域名。
- 标准库 HTTPS 不继承代理环境变量，避免意外把个人手动输入链接交给未知代理。默认核验证书，不关闭 TLS 验证。
- 公开 HTML 核验 robots，包括 `*`、`$`、更具体的 Allow，以及 Crawl-delay / Request-rate；robots 获取失败、登录页、禁止读取时停止。robots 缓存 5 分钟。仅官方公开 API 适配器不使用网页 robots 流程。
- 响应限制为 16 MB（HTML 为 5 MB），连接超时 10 秒、读取总时限 30 秒。使用未压缩响应，拒绝服务器忽略这一要求后的压缩响应，以限制解压风险。内容只作为不可信数据解析，不执行页面脚本。
- 单次 404、403、超时、无法识别 JSON-LD **都不是岗位已关闭的证据**。自动 API 返回的公开发布岗位标记为“有效（本次核验）”；JSON-LD 无其他有效性证据则为“未知”，明确过期的 `validThrough` 可标记关闭并保留原始字段供核对。
- 未配置账号或接口的来源不得显示“已连接”；临时获取失败由应用保存最近失败状态和错误。配置保留但运行失败时，不抹掉之前岗位或已有材料。

## 测试范围

`tests/test_sources.py` 是不联网的可重复测试，覆盖：SSRF 常见地址/混淆形式、混合公网私网 DNS、每次重定向复核、robots 通配符与特定爬虫分组、失败不等于关闭、发布时间与更新时间分离、年薪保持原周期、JSON-LD 图结构和重复节点、无结构化数据拒绝伪造、来源默认状态、slug 注入防护。上述“HTTP 200/404”记录来自本次独立实测，**不是 mock 结果**。
