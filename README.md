# Agent SkillOps

基于 [awesome-llm-apps 的 self-improving-agent-skills](https://github.com/Shubhamsaboo/awesome-llm-apps/tree/main/agent_skills/self-improving-agent-skills) 的学习型二次开发：让 Skill 优化有固定评测、失败证据、回归验证、人工发布和版本回滚。

不是从零原创，也不是模型训练或 Agent 权重自进化。优化对象仍然是 `SKILL.md`。原始项目说明保留在 [upstream-readme](docs/upstream-readme.md)，许可证为 Apache-2.0，见 [LICENSE](LICENSE) 和 [NOTICE](NOTICE)。本阶段不做 RAG。

## 先体验

默认 `demo` 模式，无需 API Key；它用确定性脚本模拟模型输出，只能证明工程流程，不代表模型能力或优化效果。

环境：Python 3.12、Node.js 22+、pnpm 11.25.0。首次安装：

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r backend/requirements.txt
cd frontend
pnpm install --frozen-lockfile
```

两个终端分别启动（从仓库根目录开始）：

```sh
.venv/bin/python backend/app.py
```

```sh
cd frontend
pnpm dev
```

打开 [http://localhost:3001](http://localhost:3001)。后端为 [http://127.0.0.1:8892](http://127.0.0.1:8892)，接口说明 [Swagger](http://127.0.0.1:8892/docs)。端口与上游的 3000/8891 分离。

依次点“代码审查样例 → 保存并确认冻结 → 启动优化与验证 → 查看逐项证据 → 人工发布”。再点“安全拒绝样例”，观察功能提高但安全退化的候选被舍弃。

## 新增能力

| 模块 | 实现 | 解决的问题 |
| --- | --- | --- |
| 数据与版本 | SQLite + SQLModel，独立 Skill ID、版本父链、发布指针、审计 | 重启丢历史、同名串数据、改坏无法追溯 |
| 固定评测集 | Skill 专属草稿、人工确认、内容 hash、train/holdout、场景绑定标准 | 每次临时出题导致分数不可比较 |
| 可信评分 | keyword/forbidden/JSON Schema + 独立 Judge，严格 boolean 与 ID 完整性 | 漏判标准、格式错误被当作通过 |
| 证据驱动优化 | 实际失败输出传 Analyst，Mutator 定向改动，训练改进才保留 | 只看总分、不知道改动依据 |
| 发布门禁 | 保留集基线/候选各 3 次配对复测、安全 100%、关键能力不回归、人工发布 | 高分掩盖安全退化、训练集过拟合 |
| 任务与体验 | 2 运行 + 10 等待、取消、预算、重试、超时、SSE + 轮询、历史与 diff | 无边界成本、任务失控、进度断线 |

后端只分 `domain / store / evaluation / adk_optimizer / jobs / app`，不额外引入 LangGraph、队列中间件或框架包装。

## 真实模型模式

把根目录 [.env.example](.env.example) 的内容保存为根目录 `.env`，修改：

```dotenv
SKILLOPS_MODE=gemini
GOOGLE_API_KEY=在这里填写你的服务端密钥
SKILLOPS_MODEL=gemini-3-flash-preview
```

重启后端。模型名沿用上游默认值，可换成账户实际支持的 Gemini 模型；默认模型的当前可用性、真实 API 调用及实际优化效果尚未验证。前端不收集 API Key，也不会在浏览器保存它。

上传真实 Skill 后，点“模型生成草稿”，**人工审查并修改**评测 JSON，确认后再启动。也可直接导入自己的 `{ "cases": [...] }`。不要把离线样例分数写成真实模型提升。

每个模型调用使用独立 ADK Session，由 Python 把必要的上下文显式传给下一角色。Executor/Judge/Analyst/Mutator 使用同一配置模型，但角色和上下文分离；这并不消除同模型偏差。

## 测试与学习

```sh
.venv/bin/python -m pytest backend/tests -q
cd frontend
pnpm typecheck
pnpm build
```

阅读顺序从 [小白上手](docs/start-here.md) 开始，再读 [模块讲解与面试问答](docs/modules.md)、[执行路径与接口](docs/runtime.md)、[测试记录](docs/verification.md) 和 [后续真实实验](docs/experiments.md)。GitHub Actions 会执行测试与前端构建，但远端运行结果以 GitHub 实际状态为准。

## 边界与安全

- **本地单用户原型，不是生产平台**：没有账号、权限、多租户、加密存储或分布式任务队列。后端/前端绑定 loopback，不能用 `uvicorn --workers 2`；并发约束只对一个进程生效。
- 只评估文本 Skill：把正文和 references 放进模型上下文，不运行上传代码、不调用外部工具、不执行 shell。脚本引用会被保守拒绝；静态检查无法识别所有隐含的工具需求，用户仍要人工判断。
- `.env`、数据库、上传内容、依赖目录不进入 Git；不要上传企业机密、个人敏感信息或真实密钥。真实模式会把正文、引用、测试输入和输出发给 Google 模型。
- 安全 100% 仅指已配置检查全部通过，**不是绝对安全保证**。小保留集/三次复测也不能证明统计显著提升。
- 超时 60 秒；瞬时错误最多重试 2 次；默认 150 次请求预算含重试；没有供应商级熔断、Token 级限流或自动换模型降级。不会把真实调用失败悄悄改成离线分数。
- 发布只改变本工具中的版本指针并供下载，不会替你部署到外部 Agent。
