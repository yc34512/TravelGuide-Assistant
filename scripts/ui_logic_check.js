/* 轻量 UI 逻辑检查：从 index.html 抽取版本相关函数，用模拟 DOM/fetch 执行。
   不启动浏览器，不访问网络；用于补齐真实浏览器 E2E 未完成时的逻辑验证。 */
const fs = require("node:fs");
const path = require("node:path");

const html = fs.readFileSync(path.join(__dirname, "..", "web", "index.html"), "utf8");
const start = html.indexOf("// —— 报告版本");
const end = html.indexOf("$('askChips').addEventListener");
if (start < 0 || end < 0) throw new Error("version UI functions not found");

const elements = {};
const node = (id) => (elements[id] ||= {
  id, innerHTML: "", textContent: "",
  classList: { add() {}, remove() {}, toggle() {} },
});
const $ = (id) => node(id);
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => (
  { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const alerts = [];
global.alert = (m) => alerts.push(String(m));
global.confirm = () => true;
global.crypto = { randomUUID: () => "uuid-test" };

const versions = {
  versions: [
    { version_id: "v1", version_no: 1, quality_status: "pass", created_at: 1, change_summary: "" },
    { version_id: "v2", version_no: 2, quality_status: "warning", created_at: 2, change_summary: "排除乙馆" },
  ],
};
const diff = {
  from_version_no: 1, to_version_no: 2, summary: "移除 乙馆；新增 丙塔",
  added_spots: ["丙塔"], removed_spots: ["乙馆"], moved_spots: [],
  recheck: {
    from_under_new_constraints: { valid: false, issues: ["用户明确排除乙馆，旧方案仍排入"] },
    to_under_new_constraints: { valid: true, issues: [] },
  },
  quality: { from: { score: 96, passed: true }, to: { score: 90, passed: true } },
};
global.fetch = async (url) => {
  const u = String(url);
  let body;
  if (u.includes("/versions")) body = versions;
  else if (u.includes("/summary")) body = { active_version_id: "v1" };
  else if (u.includes("/diff")) body = diff;
  else body = {};
  return { ok: true, json: async () => body };
};

const factory = new Function(
  "$", "esc", "alert", "confirm", "crypto",
  `let currentActiveVersionId = null;
${html.slice(start, end)}
return { loadVersions, showDiff };`,
);
const { loadVersions, showDiff } = factory(
  $, esc, global.alert, global.confirm, global.crypto);
(async () => {
  await loadVersions("rid");
  const list = node("versionsList").innerHTML;
  const checks = {
    version_rows: list.includes("v1") && list.includes("v2"),
    activate_button: list.includes("采用此版"),
    diff_button: list.includes("查看差异"),
  };
  await showDiff("rid", "v2");
  const box = node("diffBox").innerHTML;
  checks.diff_summary = box.includes("移除 乙馆");
  checks.diff_recheck = box.includes("有硬冲突") && box.includes("通过");
  checks.diff_quality = box.includes("90");
  const failed = Object.entries(checks).filter(([, ok]) => !ok).map(([k]) => k);
  console.log(JSON.stringify({ ok: failed.length === 0, checks, failed, alerts }));
  if (failed.length) process.exit(1);
})();