const SAMPLE_CODE = `package com.example.shop.service;

import java.text.SimpleDateFormat;
import java.sql.Connection;
import java.sql.Statement;
import java.util.List;

public class OrderService {
    private static final SimpleDateFormat FORMAT = new SimpleDateFormat("yyyy-MM-dd HH:mm:ss");
    private OrderDao orderDao;
    private UserDao userDao;

    public List<OrderVO> exportOrders(String userId, String date) {
        String sql = "SELECT * FROM t_order WHERE user_id = '" + userId + "' AND create_time = '" + date + "'";
        Connection conn = ConnectionPool.borrow();
        try {
            Statement stmt = conn.createStatement();
            List<Order> orders = orderDao.query(conn, sql);
            List<OrderVO> result = new ArrayList<>();
            for (Order order : orders) {
                User user = userDao.findById(order.getUserId());   // 循环内逐条查询
                OrderVO vo = new OrderVO(order, user);
                vo.setCreateTime(FORMAT.format(order.getCreatedAt()));
                result.add(vo);
            }
            return result;
        } catch (Exception e) {
            // 忽略
        }
        return null;
    }

    public String getToken() {
        String secret = "admin123!";   // 硬编码凭据
        return Md5Util.md5(secret + System.currentTimeMillis());
    }
}`;

const $ = (id) => document.getElementById(id);
let activeTab = "code";
let attachedImages = [];

document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    activeTab = tab.dataset.tab;
    document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t === tab));
    $("input-code").hidden = activeTab !== "code";
    $("input-diff").hidden = activeTab !== "diff";
  });
});

$("load-sample").addEventListener("click", () => {
  $("input-code").value = SAMPLE_CODE;
  $("filename").value = "OrderService.java";
});

$("images").addEventListener("change", async (event) => {
  attachedImages = [];
  for (const file of event.target.files) {
    if (file.size > 8 * 1024 * 1024) continue;
    const dataUrl = await fileToDataUrl(file);
    attachedImages.push({
      name: file.name,
      mime: file.type || "image/png",
      data_b64: dataUrl.split(",", 2)[1],
    });
  }
  $("image-names").textContent = attachedImages.map((img) => img.name).join("，");
});

function fileToDataUrl(file) {
  return new Promise((resolve) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.readAsDataURL(file);
  });
}

$("submit").addEventListener("click", async () => {
  const payload = {
    code: activeTab === "code" ? $("input-code").value : "",
    diff: activeTab === "diff" ? $("input-diff").value : "",
    filename: $("filename").value.trim(),
    language: $("language").value.trim() || "java",
    images: attachedImages,
  };
  if (!payload.code && !payload.diff && payload.images.length === 0) {
    alert("请先粘贴代码 / diff，或附加图片");
    return;
  }
  $("submit").disabled = true;
  resetOutput();
  try {
    const resp = await fetch("/api/reviews", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!resp.ok) throw new Error(await resp.text());
    const { session_id } = await resp.json();
    streamEvents(session_id);
  } catch (err) {
    addEvent("error", `提交失败：${err.message}`);
    $("submit").disabled = false;
  }
});

function streamEvents(sessionId) {
  const source = new EventSource(`/api/reviews/${sessionId}/events`);
  source.onmessage = (msg) => {
    const event = JSON.parse(msg.data);
    handleEvent(event);
    if (event.type === "report" || event.type === "error") {
      source.close();
      $("submit").disabled = false;
    }
  };
  source.onerror = () => {
    source.close();
    $("submit").disabled = false;
  };
}

const PHASE_LABEL = { intake: "受理", retrieve: "知识库检索", aggregate: "主 Agent 汇总" };
const AGENT_LABEL = {
  security: "安全专家", performance: "性能专家", style: "规范专家",
  test: "测试专家", vision: "多模态专家",
};

function handleEvent(event) {
  const data = event.data || {};
  switch (event.type) {
    case "phase":
      addEvent("phase", `阶段：${PHASE_LABEL[data.phase] || data.phase}` +
        (data.refs && data.refs.length ? ` <span class="muted">（命中知识 ${data.refs.length} 条）</span>` : ""));
      break;
    case "plan":
      addEvent("plan", `调度计划 → 子 Agent：${(data.sub_agents || []).map((n) => AGENT_LABEL[n] || n).join("、")}` +
        (data.focus_points && data.focus_points.length
          ? `<br/><span class="muted">关注点：${escapeHtml(data.focus_points.join("；"))}</span>` : ""));
      break;
    case "agent_start":
      addEvent("agent", `<span class="tag">${AGENT_LABEL[data.agent] || data.agent}</span>开始工作`);
      break;
    case "skill_call":
      addEvent("skill", `<span class="tag">${AGENT_LABEL[data.agent] || data.agent}</span>调用技能 <code>${escapeHtml(data.skill)}</code> <span class="muted">${escapeHtml(JSON.stringify(data.arguments || {}))}</span>`);
      break;
    case "agent_done":
      addEvent("agent", `<span class="tag">${AGENT_LABEL[data.agent] || data.agent}</span>完成，发现 ${data.findings} 项` +
        (data.error ? ` <span class="muted">（${escapeHtml(data.error)}）</span>` : ""));
      break;
    case "error":
      addEvent("error", `错误：${escapeHtml(data.message || "")}`);
      break;
    case "report":
      renderReport(data);
      break;
  }
}

function addEvent(cls, html) {
  const timeline = $("timeline");
  const empty = timeline.querySelector(".empty");
  if (empty) empty.remove();
  const div = document.createElement("div");
  div.className = `event ${cls}`;
  div.innerHTML = html;
  timeline.appendChild(div);
  timeline.scrollTop = timeline.scrollHeight;
}

function resetOutput() {
  $("timeline").innerHTML = "";
  $("report").hidden = true;
  $("report").innerHTML = "";
}

function renderReport(report) {
  const box = $("report");
  box.hidden = false;
  if (report.status === "failed") {
    box.innerHTML = `<div class="summary">评审失败：${escapeHtml(report.summary || "未知错误")}</div>`;
    return;
  }
  const counts = {};
  (report.findings || []).forEach((f) => { counts[f.severity] = (counts[f.severity] || 0) + 1; });
  const chips = Object.entries(counts)
    .map(([sev, n]) => `<span class="sev-chip badge ${sev}">${sev} × ${n}</span>`)
    .join("");
  const findings = (report.findings || []).map(renderFinding).join("");
  box.innerHTML = `
    <div class="summary">${escapeHtml(report.summary || "")}
      <div class="meta">耗时 ${(report.duration_ms / 1000).toFixed(1)}s · 语言 ${escapeHtml(report.language || "")}</div>
    </div>
    <div class="sev-row">${chips || '<span class="muted">未发现问题</span>'}</div>
    ${findings}`;
}

function renderFinding(f) {
  return `<div class="finding ${f.severity}">
    <h4><span class="badge ${f.severity}">${f.severity}</span>${escapeHtml(f.title || "")}</h4>
    <div class="meta">${AGENT_LABEL[f.agent] || f.agent || ""} · ${escapeHtml(f.category || "")} · ${escapeHtml(f.file || "")}${f.lines ? ":" + escapeHtml(f.lines) : ""} · 置信度 ${Number(f.confidence || 0).toFixed(2)}</div>
    <p>${escapeHtml(f.description || "")}</p>
    ${f.code_snippet ? `<pre>${escapeHtml(f.code_snippet)}</pre>` : ""}
    ${f.suggestion ? `<p><span class="label">建议：</span>${escapeHtml(f.suggestion)}</p>` : ""}
    ${(f.references || []).length ? `<div class="refs">${f.references.map((r) => `<span class="ref">KB:${escapeHtml(r)}</span>`).join("")}</div>` : ""}
  </div>`;
}

function escapeHtml(text) {
  return String(text ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

async function refreshHealth() {
  try {
    const data = await (await fetch("/api/health")).json();
    const items = [
      ["LLM", data.llm],
      ["Qdrant", data.qdrant],
      ["ES", data.elasticsearch],
    ];
    if (data.vision_enabled) items.splice(1, 0, ["VL", data.vision]);
    $("health").innerHTML = items
      .map(([name, ok]) => `<span class="dot ${ok ? "ok" : "bad"}">${name}</span>`)
      .join("");
  } catch {
    $("health").innerHTML = '<span class="dot bad">服务不可达</span>';
  }
}
refreshHealth();
setInterval(refreshHealth, 15000);
