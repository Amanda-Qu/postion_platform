# 公司招聘来源与工作流完善 · 2026-10-01

## 已完成

- 11 家公司官网目录，显式选择并按 id 追加；保留已有设置与开关，新增默认关闭
- TokenFab、极视角专用静态岗位解析；来源记录使用真实官网 URL 和独立岗位标识，避免同页岗位互相覆盖
- 其余 9 家明确显示仅招聘入口、自动读取待实现；BOSS/猎聘/LinkedIn 继续人工查看/导入
- 目标城市/固定月薪分组：明确符合、区间可能符合、信息不足、其他城市、低于目标；年包不折算固定月薪，未知预算仍保留调查
- 技能证据覆盖度、微调/多模态区分、AI 主张核对标记，避免少量已知技能产生“整体100分”的误解
- 简历导入保留期间的新修改；冲突停止覆盖。失败或重试仍可查看旧材料，历史简历独立判断是否过期

## 使用

按 README 安装并启动。进入设置 → 选择公司目录 → 选择来源 → 添加到设置草稿 → 保存设置。选择 TokenFab 或极视角后可手动启用并验证获取；其余入口人工查看，将相关岗位文字、截图或文件导入。首次启动仍保留原项目的默认调度设置，本交付未启动用户环境的定时任务。

基于技能的开放网络公司发现尚未实现，不能把本目录视作全网搜索。

## 验证

- `.venv/bin/python -m pytest tests -q -rs`：147 passed，2 skipped，33 subtests passed
- `node --check static/app.js`：通过
- `node --test tests/test_ui_render.cjs`：6 passed
- `.venv/bin/python -m compileall -q app`：通过
- `git diff --check`：通过

两项跳过均为 Windows OCR 集成测试（当前为 Linux，缺少 Windows 中文语言包/接口）。测试使用独立临时数据，未读取个人数据库。

公开网页观测：TokenFab 38 个岗位，极视角13个岗位（排除5个占位项）；不代表均在招、均符合深圳40K或技术匹配。测试仅包含必要招聘区域的提取夹具与来源记录，没有完整网页或用户资料。

## 未验证/限制

- 安全获取器 `safe_fetch` 在本执行环境 DNS 失败；公开 HTTPS HTML 读取及解析成功，但不声称完整 robots 校验后的生产获取已通过
- 隔离测试网页在浏览器侧报 `net::ERR_BLOCKED_BY_CLIENT`，实际浏览器交互和视觉检查未完成；6项 Node 测试仅验证渲染逻辑与转义
- 真实 AI、SMTP、系统 OCR 和目标环境网络仍需环境配置与验证
- 本地完成，未推送 GitHub、未部署、未登录招聘平台、未联系任何招聘方

## 应用补丁

`source-catalog-milestone.patch` 以 GitHub 上游 `9e988ab54d628d3861f8f1ff6a495b0a176a9dfd` 的精确文件树为基线。应在干净检出中先运行 `git apply --check source-catalog-milestone.patch`，再运行 `git apply source-catalog-milestone.patch`。不覆盖现有个人数据目录；升级前建议备份数据。
