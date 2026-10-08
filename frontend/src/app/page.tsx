// SkillOps changes: replace upstream wizard with a persistent release workbench.
"use client";

import { useEffect, useState } from "react";
import { diffLines } from "diff";

const API = process.env.NEXT_PUBLIC_API_URL || "http://127.0.0.1:8892";
const active = (status?: string) =>
  ["queued", "running", "stopping"].includes(status || "");
const text = (value: unknown) => JSON.stringify(value, null, 2);
const short = (id?: string | null) => id?.slice(0, 8) || "未发布";
const labels: Record<string, string> = {
  draft: "草稿",
  confirmed: "已冻结",
  candidate: "训练候选",
  approved: "待发布",
  released: "已发布过",
  rejected: "已拒绝",
  queued: "排队中",
  running: "运行中",
  completed: "已完成",
  failed: "失败",
  stopped: "已停止",
  interrupted: "重启中断",
};

type Skill = {
  id: string;
  name: string;
  description: string;
  current_version_id: string | null;
};
type Version = {
  id: string;
  parent_id: string | null;
  content: string;
  status: string;
  summary: string;
};
type Case = {
  id: string;
  input: string;
  split: string;
  checks: Record<string, unknown>[];
};
type Suite = {
  id: string;
  number: number;
  cases: Case[];
  status: string;
  content_hash: string;
};
type Detail = {
  skill: Skill;
  versions: Version[];
  suites: Suite[];
  jobs: Job[];
  decisions: {
    id: string;
    action: string;
    reason: string;
    version_id: string;
  }[];
};
type Workspace = {
  id: string;
  skill_id: string;
  version: Version;
  skill: Skill;
  suite: Suite | null;
  job: Job | null;
};
type Result = {
  decision: string;
  reasons: string[];
  candidate_version_id: string;
  baseline_version_id: string;
  train_before: number;
  train_after: number;
  baseline_holdout_score: number;
  candidate_holdout_score: number;
  model_requests: number;
  tokens: number;
  mode: string;
  demo_notice: string | null;
  experiments: {
    round: number;
    score_before: number;
    score_after: number;
    kept: boolean;
    reason: string;
  }[];
};
type Job = {
  id: string;
  status: string;
  error: string | null;
  result: Result | null;
  runs?: {
    id: string;
    split: string;
    version_id: string;
    report: {
      score: number;
      safety_score: number;
      errors: number;
      details: Record<string, unknown>[];
    };
  }[];
  events?: { id: number; type: string; data: Record<string, unknown> }[];
};
type Settings = { mode: string; model: string; key_configured: boolean };

async function api<T>(path: string, body?: object | FormData): Promise<T> {
  const form = body instanceof FormData;
  const response = await fetch(API + path, {
    method: body ? "POST" : "GET",
    headers: form || !body ? undefined : { "Content-Type": "application/json" },
    body: body ? (form ? body : JSON.stringify(body)) : undefined,
  });
  const value = await response.json();
  if (!response.ok)
    throw new Error(
      typeof value.detail === "string" ? value.detail : text(value.detail),
    );
  return value;
}

export default function Home() {
  const [settings, setSettings] = useState<Settings>();
  const [skills, setSkills] = useState<Skill[]>([]);
  const [workspace, setWorkspace] = useState<Workspace>();
  const [detail, setDetail] = useState<Detail>();
  const [job, setJob] = useState<Job>();
  const [editor, setEditor] = useState('{"cases": []}');
  const [selectedVersion, setSelectedVersion] = useState<string>();
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [connection, setConnection] = useState("轮询兜底");
  const [rounds, setRounds] = useState(5);
  const [budget, setBudget] = useState(150);

  async function load(sessionId: string) {
    const ws = await api<Workspace>(`/api/sessions/${sessionId}`);
    const [snapshot, list] = await Promise.all([
      api<Detail>(`/api/skills/${ws.skill_id}`),
      api<Skill[]>("/api/skills"),
    ]);
    setWorkspace(ws);
    setDetail(snapshot);
    setSkills(list);
    setJob(ws.job ? await api<Job>(`/api/jobs/${ws.job.id}`) : undefined);
    setEditor(text({ cases: ws.suite?.cases || [] }));
    setSelectedVersion(ws.version.id);
    localStorage.setItem("skillops-session", sessionId);
  }

  async function act(operation: () => Promise<void>) {
    setBusy(true);
    setError("");
    try {
      await operation();
    } catch (e) {
      setError(e instanceof Error ? e.message : "操作失败");
    } finally {
      setBusy(false);
    }
  }

  useEffect(() => {
    Promise.all([api<Settings>("/api/settings"), api<Skill[]>("/api/skills")])
      .then(([config, list]) => {
        setSettings(config);
        setSkills(list);
      })
      .catch(() => setError("后端未连接，请启动 8892 端口的服务"));
    const saved = localStorage.getItem("skillops-session");
    if (saved)
      load(saved).catch(() => localStorage.removeItem("skillops-session"));
  }, []);

  const jobId = job?.id;
  const running = active(job?.status);
  useEffect(() => {
    if (!jobId || !running || !workspace) return;
    let alive = true;
    const skillId = workspace.skill_id;
    async function refresh() {
      try {
        const current = await api<Job>(`/api/jobs/${jobId}`);
        if (!alive) return;
        setJob(current);
        if (!active(current.status))
          setDetail(await api<Detail>(`/api/skills/${skillId}`));
      } catch {
        if (alive) setConnection("连接暂时中断，正在重试");
      }
    }
    const source = new EventSource(`${API}/api/jobs/${jobId}/events`);
    source.onopen = () => setConnection("SSE 实时更新 · 轮询兜底");
    source.onmessage = () => {
      void refresh();
    };
    source.onerror = () => setConnection("SSE 重连中 · 轮询兜底");
    const interval = setInterval(refresh, 2000);
    return () => {
      alive = false;
      source.close();
      clearInterval(interval);
    };
  }, [jobId, running, workspace?.skill_id]);

  async function upload(files: FileList | null, asVersion = false) {
    if (!files?.length) return;
    await act(async () => {
      const body = new FormData();
      Array.from(files).forEach((file) =>
        body.append("files", file, file.webkitRelativePath || file.name),
      );
      if (asVersion && workspace) body.append("skill_id", workspace.skill_id);
      const result = await api<{ session_id: string }>("/api/upload", body);
      await load(result.session_id);
    });
  }

  async function saveSuite(confirm = false) {
    if (!workspace) return;
    await act(async () => {
      const suite = await api<Suite>(
        `/api/sessions/${workspace.id}/suites`,
        JSON.parse(editor),
      );
      if (confirm) await api(`/api/suites/${suite.id}/confirm`, {});
      await load(workspace.id);
    });
  }

  let edited = true;
  try {
    edited =
      JSON.stringify(JSON.parse(editor)) !==
      JSON.stringify({ cases: workspace?.suite?.cases || [] });
  } catch {
    /* Keep invalid drafts editable, but disable start. */
  }
  const version = detail?.versions.find((v) => v.id === selectedVersion);
  const parent = detail?.versions.find((v) => v.id === version?.parent_id);
  const result = job?.result;

  return (
    <main className="workbench">
      <header>
        <span className="eyebrow">AGENT ENGINEERING / SKILL LIFECYCLE</span>
        <h1>
          Agent <span className="gradient-text">SkillOps</span>
        </h1>
        <p>
          把“自动改好”变成“有证据、能验证、可回滚”。仅支持文本 Skill，暂不包含
          RAG 或脚本执行。
        </p>
        <div className="steps">
          <span>01 上传</span>
          <span>02 确认评测集</span>
          <span>03 优化与回归验证</span>
          <span>04 人工发布</span>
        </div>
      </header>
      <div className="notice">
        {settings?.mode === "demo"
          ? "离线演示：无需 API Key。输出与分数由脚本模拟，不代表模型质量或真实提升。"
          : `真实模型：${settings?.model || "连接中"} · 服务端 Key ${settings?.key_configured ? "已配置" : "未配置"}。前端不接收或保存密钥。`}
      </div>
      {error && (
        <div className="error" role="alert">
          {error}
        </div>
      )}
      <section className="panel">
        <h2>开始体验</h2>
        <div className="actions">
          {settings?.mode === "demo" &&
            [
              ["code-review", "代码审查样例"],
              ["writing", "内容写作样例"],
              ["unsafe", "安全拒绝样例"],
            ].map(([kind, name]) => (
              <button
                disabled={busy || running}
                key={kind}
                onClick={() =>
                  act(async () => {
                    const r = await api<{ session_id: string }>(
                      `/api/examples/${kind}`,
                      {},
                    );
                    await load(r.session_id);
                  })
                }
              >
                {name}
              </button>
            ))}
          <label className="button">
            上传 ZIP / SKILL.md
            <input
              type="file"
              accept=".zip,.md"
              disabled={busy || running}
              onChange={(e) => {
                void upload(e.target.files);
                e.target.value = "";
              }}
            />
          </label>
          <label className="button">
            上传文件夹
            <input
              type="file"
              multiple
              {...{ webkitdirectory: "" }}
              disabled={busy || running}
              onChange={(e) => {
                void upload(e.target.files);
                e.target.value = "";
              }}
            />
          </label>
        </div>
        <p className="muted">
          上传创建独立 Skill；下面的“上传新版本”才会归入当前 Skill。不要上传
          .env 或其他密钥文件。
        </p>
      </section>
      <div className="columns">
        <aside className="panel">
          <h2>Skill 历史</h2>
          {!skills.length && (
            <p className="muted">还没有记录。先加载一个样例。</p>
          )}
          {skills.map((skill) => (
            <button
              className={`history ${skill.id === workspace?.skill_id ? "selected" : ""}`}
              disabled={busy || running}
              key={skill.id}
              onClick={() =>
                act(async () => {
                  const r = await api<{ session_id: string }>(
                    `/api/skills/${skill.id}/resume`,
                    {},
                  );
                  await load(r.session_id);
                })
              }
            >
              <strong>{skill.name}</strong>
              <small>
                {short(skill.id)} · 发布 {short(skill.current_version_id)}
              </small>
            </button>
          ))}
        </aside>
        <div className="content">
          {workspace ? (
            <>
              <section className="panel">
                <h2>
                  {workspace.skill.name}{" "}
                  <small>{short(workspace.skill_id)}</small>
                </h2>
                <p>{workspace.skill.description}</p>
                <p className="muted">
                  当前发布指针：{short(detail?.skill.current_version_id)} ·
                  本次起始版本：{short(workspace.version.id)}
                </p>
                <label className="button">
                  上传当前 Skill 的新版本
                  <input
                    type="file"
                    accept=".zip,.md"
                    disabled={busy || running}
                    onChange={(e) => {
                      void upload(e.target.files, true);
                      e.target.value = "";
                    }}
                  />
                </label>
              </section>
              <section className="panel">
                <h2>
                  固定评测集{" "}
                  <small>
                    {workspace.suite
                      ? `v${workspace.suite.number} · ${labels[workspace.suite.status]}`
                      : "未创建"}
                  </small>
                </h2>
                <p className="muted">
                  至少 5 个 train + 3 个
                  holdout，两组都有安全检查。每个场景绑定自己的
                  checks。确认后不能原地修改，保存会创建新版本。
                </p>
                {workspace.suite && (
                  <p className="muted">
                    train{" "}
                    {
                      workspace.suite.cases.filter((c) => c.split === "train")
                        .length
                    }{" "}
                    · holdout{" "}
                    {
                      workspace.suite.cases.filter((c) => c.split === "holdout")
                        .length
                    }{" "}
                    · hash {short(workspace.suite.content_hash)}
                  </p>
                )}
                <textarea
                  aria-label="评测集 JSON"
                  className="code editor"
                  value={editor}
                  disabled={running}
                  onChange={(e) => setEditor(e.target.value)}
                  spellCheck={false}
                />
                <div className="actions">
                  <button
                    disabled={busy || running}
                    onClick={() => saveSuite()}
                  >
                    保存草稿
                  </button>
                  <button
                    className="primary"
                    disabled={busy || running}
                    onClick={() => saveSuite(true)}
                  >
                    保存并确认冻结
                  </button>
                  <label className="button">
                    导入 JSON
                    <input
                      type="file"
                      accept=".json"
                      disabled={busy || running}
                      onChange={(e) => {
                        const file = e.target.files?.[0];
                        if (file)
                          void act(async () => {
                            const raw = JSON.parse(await file.text());
                            setEditor(text({ cases: raw.cases }));
                          });
                        e.target.value = "";
                      }}
                    />
                  </label>
                  <button
                    disabled={busy}
                    onClick={() => {
                      const link = document.createElement("a");
                      link.href = URL.createObjectURL(
                        new Blob([editor], { type: "application/json" }),
                      );
                      link.download = "suite.json";
                      link.click();
                      URL.revokeObjectURL(link.href);
                    }}
                  >
                    导出 JSON
                  </button>
                  {settings?.mode !== "demo" && (
                    <button
                      disabled={busy || running}
                      onClick={() =>
                        act(async () => {
                          await api(
                            `/api/sessions/${workspace.id}/analyze`,
                            {},
                          );
                          await load(workspace.id);
                        })
                      }
                    >
                      模型生成草稿
                    </button>
                  )}
                </div>
                {edited && (
                  <p className="muted">
                    当前编辑内容尚未冻结。启动前请保存并确认。
                  </p>
                )}
              </section>
              <section className="panel">
                <h2>优化任务</h2>
                <div className="actions">
                  <label>
                    最多轮数{" "}
                    <input
                      className="number"
                      type="number"
                      min={1}
                      max={50}
                      value={rounds}
                      onChange={(e) => setRounds(Number(e.target.value))}
                    />
                  </label>
                  <label>
                    请求预算{" "}
                    <input
                      className="number"
                      type="number"
                      min={1}
                      max={1000}
                      value={budget}
                      onChange={(e) => setBudget(Number(e.target.value))}
                    />
                  </label>
                  <button
                    className="primary"
                    disabled={
                      busy ||
                      running ||
                      edited ||
                      workspace.suite?.status !== "confirmed"
                    }
                    onClick={() =>
                      act(async () => {
                        await api(`/api/sessions/${workspace.id}/start`, {
                          max_rounds: rounds,
                          request_budget: budget,
                        });
                        await load(workspace.id);
                      })
                    }
                  >
                    启动优化与验证
                  </button>
                  {running && (
                    <button
                      disabled={busy}
                      onClick={() =>
                        act(async () => {
                          await api(`/api/jobs/${jobId}/stop`, {});
                          await load(workspace.id);
                        })
                      }
                    >
                      停止任务
                    </button>
                  )}
                </div>
                <p className="muted">
                  单进程最多 2 个任务运行，10 个等待；同一个 Skill
                  不并行优化。保留集不进入修改 Prompt。
                </p>
                {job && (
                  <>
                    <p className="job-status">
                      {labels[job.status] || job.status} · {short(job.id)}{" "}
                      {running && <small>{connection}</small>}
                    </p>
                    {job.error && <p className="error">{job.error}</p>}
                    <div className="event-log">
                      {job.events?.slice(-12).map((event) => (
                        <p key={event.id}>
                          <span>
                            #{event.id} {event.type}
                          </span>{" "}
                          {String(
                            event.data.message ||
                              event.data.reason ||
                              (event.data.score !== undefined
                                ? `${event.data.split || "train"} ${event.data.score} 分 · 安全 ${event.data.safety_score}%`
                                : ""),
                          )}
                        </p>
                      ))}
                    </div>
                  </>
                )}
                {result && (
                  <>
                    <div
                      className={`notice ${result.decision === "rejected" ? "rejected" : ""}`}
                    >
                      {result.decision === "approved"
                        ? "验证通过，等待人工发布"
                        : "验证拒绝，不能发布"}
                      ：{result.reasons.join("；")}
                    </div>
                    <div className="metrics">
                      <div>
                        训练分数
                        <strong>
                          {result.train_before} → {result.train_after}
                        </strong>
                      </div>
                      <div>
                        保留集平均分
                        <strong>
                          {result.baseline_holdout_score} →{" "}
                          {result.candidate_holdout_score}
                        </strong>
                      </div>
                      <div>
                        模型请求 / Tokens
                        <strong>
                          {result.model_requests} / {result.tokens}
                        </strong>
                      </div>
                    </div>
                    {result.demo_notice && (
                      <p className="muted">{result.demo_notice}</p>
                    )}
                    {result.experiments.map((round) => (
                      <p key={round.round} className="muted">
                        第 {round.round} 轮：{round.score_before} →{" "}
                        {round.score_after} · {round.kept ? "保留" : "舍弃"} ·{" "}
                        {round.reason}
                      </p>
                    ))}
                    {result.decision === "approved" && (
                      <button
                        className="primary"
                        disabled={
                          busy ||
                          running ||
                          detail?.skill.current_version_id ===
                            result.candidate_version_id
                        }
                        onClick={() =>
                          act(async () => {
                            await api(
                              `/api/skills/${workspace.skill_id}/publish/${result.candidate_version_id}`,
                              {},
                            );
                            await load(workspace.id);
                          })
                        }
                      >
                        人工发布候选 {short(result.candidate_version_id)}
                      </button>
                    )}
                  </>
                )}
                {job?.runs?.map((run) => (
                  <details key={run.id}>
                    <summary>
                      {run.split} · {short(run.version_id)} · 功能{" "}
                      {run.report.score} · 安全 {run.report.safety_score}% ·
                      错误 {run.report.errors}
                    </summary>
                    <pre className="code">{text(run.report)}</pre>
                  </details>
                ))}
              </section>
              <section className="panel">
                <h2>版本与变更</h2>
                <div className="actions">
                  {detail?.versions.map((v) => (
                    <button
                      key={v.id}
                      className={v.id === selectedVersion ? "selected" : ""}
                      onClick={() => setSelectedVersion(v.id)}
                    >
                      {short(v.id)} · {labels[v.status]}
                    </button>
                  ))}
                </div>
                {version && (
                  <>
                    <p>{version.summary}</p>
                    <div className="actions">
                      <a
                        className="button"
                        href={`${API}/api/versions/${version.id}/download`}
                      >
                        下载此版本 ZIP
                      </a>
                      {version.status === "released" &&
                        version.id !== detail?.skill.current_version_id && (
                          <button
                            disabled={busy || running}
                            onClick={() =>
                              act(async () => {
                                await api(
                                  `/api/skills/${workspace.skill_id}/rollback/${version.id}`,
                                  {},
                                );
                                await load(workspace.id);
                              })
                            }
                          >
                            回滚到此历史发布版本
                          </button>
                        )}
                    </div>
                    <pre className="code diff">
                      {diffLines(parent?.content || "", version.content).map(
                        (part, i) => (
                          <span
                            key={i}
                            className={
                              part.added
                                ? "added"
                                : part.removed
                                  ? "removed"
                                  : ""
                            }
                          >
                            {part.value}
                          </span>
                        ),
                      )}
                    </pre>
                  </>
                )}
                <details>
                  <summary>发布与验证审计记录</summary>
                  <pre className="code">{text(detail?.decisions)}</pre>
                </details>
                <div className="actions">
                  {detail?.jobs.map((j) => (
                    <button
                      key={j.id}
                      disabled={busy || running}
                      onClick={() =>
                        act(async () =>
                          setJob(await api<Job>(`/api/jobs/${j.id}`)),
                        )
                      }
                    >
                      查看任务 {short(j.id)} · {labels[j.status]}
                    </button>
                  ))}
                </div>
              </section>
            </>
          ) : (
            <section className="panel empty">
              <h2>从一个可复现的样例开始</h2>
              <p>
                选“代码审查样例” → 确认冻结 → 启动 → 查看评测证据 → 人工发布。
              </p>
              <p className="muted">
                再试“安全拒绝样例”，观察候选有功能改进但被安全检查拦截。
              </p>
            </section>
          )}
        </div>
      </div>
      <footer>
        基于 awesome-llm-apps / self-improving-agent-skills 二次开发 ·
        Apache-2.0 · 本地单用户原型
      </footer>
    </main>
  );
}
