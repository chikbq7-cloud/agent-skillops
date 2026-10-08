# 执行路径与接口

## 从上传到发布的函数路径

```text
POST /api/upload
  upload → read_files → skill_content → register
  保存 Skill、Version、Workspace

POST /api/sessions/{id}/analyze （真实模型，可选）
  Agents.json(designer) → SuiteInput → save_suite（草稿）
POST /api/sessions/{id}/suites
  SuiteInput → save_suite（每次新版本）
POST /api/suites/{id}/confirm
  SuiteInput.confirm → hash 校验 → confirmed

POST /api/sessions/{id}/start
  StartInput → Jobs.start → asyncio.create_task(worker)
  worker → Semaphore(2) → execute
    读取起始 Version、冻结 Suite、当前发布基线
    SkillOptimizer.optimize （仅 train）
      evaluate → record → Evaluator.run
        executor → rule_check / judge → summarize → 保存 Run
      failed_training_checks → analyst → mutator
      验证元数据/引用 → candidate 训练评测 → 保留或拒绝
    基线/最佳候选 holdout 配对复测 × 3
    release_gate → 保存 Decision 和 Job.result
    SSE event: completed （尚未发布）

POST /api/skills/{id}/publish/{version_id}
  Store.publish → 审核证据/基线有效性 → 同事务更新指针和审计
POST /api/skills/{id}/rollback/{version_id}
  Store.publish(rollback=True) → 仅历史发布版本 → 指针更新
```

## 模型调用的上下文

Executor 收到 Skill 正文、references 文本和一个具体测试输入。Judge 收到测试输入、Executor 输出以及该场景的标准。Analyst 收到 Skill 和失败的 **train** 检查（包含输出、标准、理由）。Mutator 收到 Skill 和 Analyst 的诊断。

每次调用新建 ADK Session；没有自动共享整份聊天历史，也没有 Agent 之间的 RPC。共享的业务状态由 Python 局部变量与 SQLite 记录承载。保留集只由最终验证阶段读入，不送给 Analyst/Mutator。

## 状态与终止

Job：`queued → running → completed / failed / stopped`；服务启动把遗留活动任务标为 `interrupted`，不偷偷重放付费调用。

Version：`draft → candidate / rejected → approved → released`。`released` 表示发布过，是否“当前正在发布”要看 `Skill.current_version_id`。已发布内容不会被覆盖。

Suite：`draft → confirmed`。没有更新 confirmed 内容的接口；任何修改生成新 Suite ID、版本号与 hash。

默认最多 5 轮；连续 2 轮无保留改进提前停止；功能/安全都 100% 时不再优化；发生评测错误不拿错误数据继续诊断。预算耗尽任务失败，不批准发布。

## 关键接口

| 接口 | 用途 |
| --- | --- |
| `GET /api/settings` | 模式、模型名、是否配置 Key；不返回密钥 |
| `POST /api/examples/{code-review|writing|unsafe}` | 新建离线样例 Skill + 草稿 Suite |
| `GET /api/skills`、`GET /api/skills/{id}` | 历史、版本、任务、审计 |
| `POST /api/skills/{id}/resume` | 新业务 Workspace，选择当前发布版或最新快照 |
| `GET /api/sessions/{id}` | 恢复前端本次操作 |
| `GET /api/suites/{id}` | 导出/检查评测集 |
| `GET /api/jobs/{id}` | 全部 Run、Event、最终验证决定 |
| `GET /api/jobs/{id}/events?after=N` | SSE，按事件 ID 恢复；支持 Last-Event-ID |
| `POST /api/jobs/{id}/stop` | 取消真正的 asyncio Task，包括排队任务 |
| `GET /api/versions/{id}/download` | 内存中打包 ZIP，不写临时上传路径 |

旧 `scenarios + evals` 请求可通过 `/api/sessions/{id}/config` 转为草稿；它只是迁移入口，不能绕过人工确认和安全/保留集要求。旧前端 API 不保证完全兼容，旧源码可从基线提交找回。

## 发布判断的具体含义

训练功能分严格高于初始发布基线（未发布时用上传版本）；候选 train/holdout 安全检查全部通过；没有评测错误；原先稳定通过的关键项不能失败；保留集三次平均功能分不能降低。

人工发布时再次确认：版本属于当前 Skill、存在完成且批准的验证 Job、验证所针对的发布指针没有变化、当前没有该 Skill 的活动任务。回滚只接受已经人工发布过的版本。

这不是线上 A/B 或灰度发布。指针只是本工具内部的“当前版本”，外部部署由用户下载后完成。
