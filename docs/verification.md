# 验证记录

日期：2026-10-08。环境：macOS / Python 3.12.14 / Node.js 24.19.0 / pnpm 11.25.0。测试全部使用离线输出和临时数据库，不发送真实模型请求。

## 实际执行结果

| 检查 | 命令 | 结果 |
| --- | --- | --- |
| 后端自动化 | `.venv/bin/python -m pytest backend/tests -q` | 36 passed；1 条 TestClient 依赖弃用警告，不影响通过 |
| Python 静态检查 | `.venv/bin/ruff check backend` | All checks passed |
| Python 格式检查 | `.venv/bin/ruff format --check backend` | 10 files already formatted |
| 依赖兼容 | `.venv/bin/python -m pip check` | No broken requirements found |
| 前端类型 | `pnpm typecheck` | 通过 |
| 前端生产构建 | `pnpm build` | 通过；首页 First Load JS 约 110 kB（构建输出，不是性能压测） |
| 补丁空白检查 | `git diff --check` | 通过 |

## 覆盖的边界

- 从样例创建到评测冻结、运行、三次配对复测、人工发布、下载完整 ZIP。
- confirmed Suite 不原地修改，保存为新版本；只能用归属正确的冻结评测集启动。
- Judge 缺项、未知 ID、重复 ID、字符串 boolean 会触发一次修复，仍失败则全部预期检查记 error。
- keyword/forbidden/JSON Schema 判定；规则与模型评分都保存完整证据。
- 功能提升但安全退化的候选被保存为 rejected，不可发布。
- 关键能力回归、保留集下降、评测错误、复测次数不足、训练未改进均阻断批准。
- 训练失败原文包含输出证据；holdout 输入没有进入 Analyst/Mutator Prompt。
- 元数据被改动则舍弃；引用缺失拒绝；同名上传不合并 Skill。
- 2 运行 + 10 等待上限、同 Skill 不重复运行、任务真实取消、预算耗尽失败。
- 429/503 最多 3 次尝试且扣预算；401 不重试。
- SSE 序号补读/Last-Event-ID；服务重启保留记录并将遗留任务标为 interrupted。
- 路径穿越、隐藏文件、非文本上传被拒绝；需脚本运行的 Skill 不启动。
- 发布基线改变后不能沿用旧批准；只能回滚历史发布版本。
- 连续两次发布后回滚到第一次，发布指针实际切回、previous_id 保留第二次发布。
- 真实模式必须配置服务端 Key；settings 接口只返回是否配置，不返回密钥。

## 页面检查

在本地 3001 页面实际操作：代码审查样例 → 冻结 → 启动 → SSE 显示进度 → 展示待发布结果。移动宽度下检查无横向溢出，页面明确显示离线分数为脚本模拟。未替用户把浏览器样例自动发布，保留人工发布入口供体验。

脚本样例的一次正常任务记录 58 次模拟请求：训练基线 10 次 + 诊断/修改 2 次 + 候选训练 10 次 + 保留集配对 3 × 12 次。没有真实 Token 费用。33.3 → 100 是预设脚本行为，**不能作为简历优化效果**。

## 尚未验证

真实 Gemini 连通性、默认模型当前可用性、真实模型收益、Judge 与人工一致率、真实供应商错误/限流、真实耗时/账单、服务压力、多用户权限与线上部署。没有声称上述检查已通过。

GitHub Actions 结果请以仓库实际工作流状态为准，本表记录的是本地执行结果。
