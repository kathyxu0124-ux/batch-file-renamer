#!/usr/bin/env python3
"""
批量文件重命名工具 (Batch File Renamer)
- 预览变化后再应用
- 多种模式：前缀、后缀、替换、正则、序号、扩展名
- 支持撤销
- 高效处理 1000+ 文件
- 保留文件元数据（使用 os.rename）
"""

import http.server
import json
import os
import re
import sys
import urllib.parse
import webbrowser
import threading
import time
from pathlib import Path
from datetime import datetime

HOST = "127.0.0.1"
PORT = 8765

# ─── Rename History (for undo) ───────────────────────────────────────────────
rename_history: list[dict] = []  # [{timestamp, folder, mappings: [{old, new}]}]


def list_files(folder: str, recursive: bool = False) -> list[dict]:
    """List files in a folder, returning name + metadata."""
    results = []
    folder_path = Path(folder)
    if not folder_path.is_dir():
        return results

    if recursive:
        iterator = folder_path.rglob("*")
    else:
        iterator = folder_path.iterdir()

    for p in iterator:
        if p.is_file():
            try:
                stat = p.stat()
                rel = p.relative_to(folder_path)
                results.append({
                    "name": p.name,
                    "rel_path": str(rel),
                    "dir": str(p.parent.relative_to(folder_path)) if p.parent != folder_path else "",
                    "size": stat.st_size,
                    "modified": stat.st_mtime,
                    "ext": p.suffix,
                })
            except (PermissionError, OSError):
                continue
    results.sort(key=lambda x: x["name"].lower())
    return results


def compute_new_name(name: str, index: int, rule: dict) -> str:
    """Apply a single renaming rule to a filename."""
    mode = rule.get("mode", "")
    stem = Path(name).stem
    ext = Path(name).suffix

    if mode == "prefix":
        prefix = rule.get("prefix", "")
        return f"{prefix}{name}"

    elif mode == "suffix":
        suffix = rule.get("suffix", "")
        return f"{stem}{suffix}{ext}"

    elif mode == "replace":
        find = rule.get("find", "")
        replace_with = rule.get("replace", "")
        case_sensitive = rule.get("caseSensitive", True)
        if not find:
            return name
        if case_sensitive:
            return name.replace(find, replace_with)
        else:
            pattern = re.compile(re.escape(find), re.IGNORECASE)
            return pattern.sub(replace_with, name)

    elif mode == "regex":
        pattern = rule.get("pattern", "")
        replacement = rule.get("replacement", "")
        if not pattern:
            return name
        try:
            return re.sub(pattern, replacement, name)
        except re.error:
            return name

    elif mode == "sequence":
        template = rule.get("template", "{name}_{num}")
        start = int(rule.get("start", 1))
        padding = int(rule.get("padding", 3))
        num_str = str(index + start).zfill(padding)
        return template.replace("{name}", stem).replace("{num}", num_str).replace("{ext}", ext)

    elif mode == "extension":
        new_ext = rule.get("newExt", "")
        if new_ext and not new_ext.startswith("."):
            new_ext = "." + new_ext
        return f"{stem}{new_ext}"

    elif mode == "case":
        case_type = rule.get("caseType", "lower")
        if case_type == "lower":
            return name.lower()
        elif case_type == "upper":
            return name.upper()
        elif case_type == "title":
            return name.title()
        elif case_type == "capitalize":
            return stem.capitalize() + ext
        return name

    elif mode == "remove":
        # Remove specific characters or patterns
        chars = rule.get("chars", "")
        if chars:
            for c in chars:
                name = name.replace(c, "")
        return name

    elif mode == "insert":
        text = rule.get("text", "")
        position = int(rule.get("position", 0))
        if position >= 0:
            return stem[:position] + text + stem[position:] + ext
        return name

    return name


def preview_renames(folder: str, files: list[dict], rules: list[dict],
                    filter_ext: str = "", recursive: bool = False) -> list[dict]:
    """Generate a preview of all renames."""
    results = []
    filtered = files
    if filter_ext:
        exts = [e.strip().lower() for e in filter_ext.split(",")]
        exts = ["." + e if not e.startswith(".") else e for e in exts]
        filtered = [f for f in files if f["ext"].lower() in exts]

    for i, f in enumerate(filtered):
        new_name = f["name"]
        for rule in rules:
            new_name = compute_new_name(new_name, i, rule)

        has_change = new_name != f["name"]
        conflict = False
        results.append({
            "original": f["name"],
            "new": new_name,
            "dir": f.get("dir", ""),
            "changed": has_change,
            "conflict": conflict,
            "size": f["size"],
        })

    # Detect conflicts (duplicate new names in same directory)
    seen: dict[str, int] = {}
    for i, r in enumerate(results):
        key = os.path.join(r["dir"], r["new"])
        if key in seen:
            results[i]["conflict"] = True
            results[seen[key]]["conflict"] = True
        else:
            seen[key] = i

    return results


def apply_renames(folder: str, previews: list[dict]) -> dict:
    """Apply the renames. Returns stats."""
    mappings = []
    errors = []
    renamed = 0

    for p in previews:
        if not p["changed"]:
            continue
        if p["conflict"]:
            errors.append(f"冲突跳过: {p['original']}")
            continue

        sub_dir = p.get("dir", "")
        old_path = os.path.join(folder, sub_dir, p["original"])
        new_path = os.path.join(folder, sub_dir, p["new"])

        if os.path.exists(new_path) and old_path != new_path:
            errors.append(f"目标已存在: {p['new']}")
            continue

        try:
            os.rename(old_path, new_path)
            mappings.append({"old": old_path, "new": new_path})
            renamed += 1
        except OSError as e:
            errors.append(f"重命名失败 {p['original']}: {e}")

    if mappings:
        rename_history.append({
            "timestamp": datetime.now().isoformat(),
            "folder": folder,
            "count": renamed,
            "mappings": mappings,
        })

    return {"renamed": renamed, "errors": errors, "total": len(previews)}


def undo_last() -> dict:
    """Undo the last rename batch."""
    if not rename_history:
        return {"success": False, "message": "没有可撤销的操作"}

    batch = rename_history.pop()
    errors = []
    restored = 0

    for m in reversed(batch["mappings"]):
        try:
            os.rename(m["new"], m["old"])
            restored += 1
        except OSError as e:
            errors.append(f"撤销失败 {m['new']}: {e}")

    return {"success": True, "restored": restored, "errors": errors}


# ─── HTML UI ─────────────────────────────────────────────────────────────────
HTML_PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>批量文件重命名工具</title>
<style>
  :root {
    --bg: #0f1117;
    --surface: #1a1d27;
    --surface2: #242733;
    --border: #2e3245;
    --text: #e4e6f0;
    --text2: #8b8fa3;
    --accent: #6c8cff;
    --accent-hover: #859dff;
    --green: #4ade80;
    --red: #f87171;
    --orange: #fbbf24;
    --radius: 8px;
  }
  * { margin:0; padding:0; box-sizing:border-box; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif;
    background: var(--bg);
    color: var(--text);
    line-height: 1.5;
    min-height: 100vh;
  }
  .container { max-width: 1200px; margin: 0 auto; padding: 24px; }

  header {
    display: flex; align-items: center; justify-content: space-between;
    margin-bottom: 24px; padding-bottom: 16px; border-bottom: 1px solid var(--border);
  }
  header h1 { font-size: 22px; font-weight: 600; }
  header .subtitle { color: var(--text2); font-size: 13px; margin-top: 2px; }

  .panel {
    background: var(--surface); border: 1px solid var(--border);
    border-radius: var(--radius); padding: 20px; margin-bottom: 16px;
  }
  .panel-title {
    font-size: 14px; font-weight: 600; margin-bottom: 14px;
    color: var(--text2); text-transform: uppercase; letter-spacing: 0.5px;
  }

  /* Folder selector */
  .folder-row { display: flex; gap: 10px; align-items: center; }
  .folder-input {
    flex: 1; padding: 10px 14px; background: var(--surface2);
    border: 1px solid var(--border); border-radius: var(--radius);
    color: var(--text); font-size: 14px; outline: none;
    transition: border-color 0.2s;
  }
  .folder-input:focus { border-color: var(--accent); }
  .folder-input::placeholder { color: var(--text2); }

  .btn {
    padding: 10px 20px; border: none; border-radius: var(--radius);
    font-size: 14px; font-weight: 500; cursor: pointer;
    transition: all 0.2s; display: inline-flex; align-items: center; gap: 6px;
  }
  .btn-primary { background: var(--accent); color: #fff; }
  .btn-primary:hover { background: var(--accent-hover); }
  .btn-success { background: var(--green); color: #000; }
  .btn-success:hover { opacity: 0.9; }
  .btn-danger { background: var(--red); color: #fff; }
  .btn-danger:hover { opacity: 0.9; }
  .btn-outline {
    background: transparent; border: 1px solid var(--border); color: var(--text);
  }
  .btn-outline:hover { border-color: var(--accent); color: var(--accent); }
  .btn:disabled { opacity: 0.4; cursor: not-allowed; }

  /* Rule builder */
  .rules-container { display: flex; flex-direction: column; gap: 12px; }
  .rule-card {
    background: var(--surface2); border: 1px solid var(--border);
    border-radius: var(--radius); padding: 14px;
    display: flex; flex-wrap: wrap; gap: 10px; align-items: center;
  }
  .rule-card select, .rule-card input {
    padding: 8px 12px; background: var(--bg); border: 1px solid var(--border);
    border-radius: 6px; color: var(--text); font-size: 13px; outline: none;
  }
  .rule-card select { min-width: 140px; }
  .rule-card input { min-width: 160px; flex: 1; }
  .rule-card select:focus, .rule-card input:focus { border-color: var(--accent); }
  .rule-remove {
    background: none; border: none; color: var(--red); cursor: pointer;
    font-size: 18px; padding: 4px 8px; border-radius: 4px;
  }
  .rule-remove:hover { background: rgba(248,113,113,0.1); }

  .add-rule-btn {
    background: transparent; border: 1px dashed var(--border); color: var(--text2);
    padding: 10px; border-radius: var(--radius); cursor: pointer;
    text-align: center; font-size: 13px; transition: all 0.2s;
  }
  .add-rule-btn:hover { border-color: var(--accent); color: var(--accent); }

  /* Options row */
  .options-row {
    display: flex; gap: 16px; align-items: center; flex-wrap: wrap;
    margin-bottom: 14px;
  }
  .option-group { display: flex; align-items: center; gap: 6px; }
  .option-group label { font-size: 13px; color: var(--text2); }
  .option-group input[type="text"] {
    padding: 6px 10px; background: var(--surface2); border: 1px solid var(--border);
    border-radius: 6px; color: var(--text); font-size: 13px; width: 120px; outline: none;
  }
  .option-group input[type="checkbox"] { accent-color: var(--accent); }

  /* Preview table */
  .preview-wrap { overflow-x: auto; }
  table {
    width: 100%; border-collapse: collapse; font-size: 13px;
  }
  th {
    text-align: left; padding: 10px 12px; color: var(--text2);
    border-bottom: 1px solid var(--border); font-weight: 500;
    position: sticky; top: 0; background: var(--surface);
  }
  td {
    padding: 8px 12px; border-bottom: 1px solid var(--border);
    max-width: 350px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  tr.changed td:nth-child(2) { color: var(--green); }
  tr.conflict td:nth-child(2) { color: var(--red); }
  tr.unchanged td { color: var(--text2); }
  .arrow { color: var(--text2); font-size: 16px; }

  .stats-bar {
    display: flex; gap: 20px; padding: 12px 0; font-size: 13px; color: var(--text2);
  }
  .stats-bar span { display: flex; align-items: center; gap: 4px; }
  .dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }
  .dot-green { background: var(--green); }
  .dot-red { background: var(--red); }
  .dot-gray { background: var(--text2); }

  /* Action bar */
  .action-bar {
    display: flex; gap: 10px; justify-content: space-between;
    align-items: center; flex-wrap: wrap;
  }
  .action-left { display: flex; gap: 10px; }

  /* Pagination */
  .pagination {
    display: flex; gap: 6px; align-items: center; justify-content: center;
    margin-top: 12px; font-size: 13px; color: var(--text2);
  }
  .pagination button {
    padding: 6px 12px; background: var(--surface2); border: 1px solid var(--border);
    border-radius: 6px; color: var(--text); cursor: pointer; font-size: 13px;
  }
  .pagination button:hover { border-color: var(--accent); }
  .pagination button:disabled { opacity: 0.3; cursor: not-allowed; }

  /* Toast notification */
  .toast {
    position: fixed; top: 20px; right: 20px; padding: 14px 20px;
    border-radius: var(--radius); font-size: 14px; z-index: 1000;
    animation: slideIn 0.3s ease;
  }
  .toast-success { background: var(--green); color: #000; }
  .toast-error { background: var(--red); color: #fff; }
  @keyframes slideIn {
    from { transform: translateX(100%); opacity: 0; }
    to { transform: translateX(0); opacity: 1; }
  }

  /* History */
  .history-item {
    display: flex; justify-content: space-between; align-items: center;
    padding: 10px; background: var(--surface2); border-radius: 6px;
    margin-bottom: 8px; font-size: 13px;
  }
  .history-item .time { color: var(--text2); }

  .loading { text-align: center; padding: 40px; color: var(--text2); }
  .empty-state {
    text-align: center; padding: 60px 20px; color: var(--text2);
  }
  .empty-state .icon { font-size: 48px; margin-bottom: 12px; }
  .empty-state p { font-size: 14px; }

  /* Dropzone visual */
  .dropzone-active { border: 2px dashed var(--accent) !important; }
</style>
</head>
<body>
<div class="container">
  <header>
    <div>
      <h1>&#128221; 批量文件重命名工具</h1>
      <div class="subtitle">支持正则表达式 / 预览 / 撤销 / 大批量处理</div>
    </div>
    <div style="display:flex;gap:8px;">
      <button class="btn btn-outline" onclick="showHistory()" id="historyBtn">历史记录</button>
      <button class="btn btn-danger" onclick="undoLast()" id="undoBtn" disabled>撤销上次</button>
    </div>
  </header>

  <!-- Folder Selection -->
  <div class="panel" id="folderPanel">
    <div class="panel-title">1. 选择文件夹</div>
    <div class="folder-row">
      <input type="text" class="folder-input" id="folderPath"
             placeholder="输入文件夹路径，例如 /Users/you/Documents/photos" />
      <button class="btn btn-primary" onclick="loadFolder()">加载文件</button>
    </div>
    <div class="options-row" style="margin-top: 12px;">
      <div class="option-group">
        <input type="checkbox" id="recursive" />
        <label for="recursive">包含子文件夹</label>
      </div>
      <div class="option-group">
        <label>过滤扩展名:</label>
        <input type="text" id="filterExt" placeholder=".jpg,.png" />
      </div>
    </div>
  </div>

  <!-- Rules -->
  <div class="panel">
    <div class="panel-title">2. 设置命名规则（可多条叠加）</div>
    <div class="rules-container" id="rulesContainer"></div>
    <div class="add-rule-btn" onclick="addRule()">+ 添加规则</div>
  </div>

  <!-- Preview -->
  <div class="panel" id="previewPanel" style="display:none;">
    <div class="panel-title">3. 预览变化</div>
    <div class="stats-bar" id="statsBar"></div>
    <div class="preview-wrap" style="max-height: 500px; overflow-y: auto;">
      <table>
        <thead><tr><th>原文件名</th><th></th><th>新文件名</th><th>子目录</th></tr></thead>
        <tbody id="previewBody"></tbody>
      </table>
    </div>
    <div class="pagination" id="pagination"></div>
    <div class="action-bar" style="margin-top:16px;">
      <div class="action-left">
        <button class="btn btn-primary" onclick="refreshPreview()">刷新预览</button>
      </div>
      <button class="btn btn-success" onclick="applyRenames()" id="applyBtn">
        &#10003; 应用重命名
      </button>
    </div>
  </div>

  <!-- Empty state -->
  <div id="emptyState" class="panel empty-state">
    <div class="icon">&#128193;</div>
    <p>输入文件夹路径并点击「加载文件」开始</p>
  </div>

  <!-- History Modal -->
  <div id="historyModal" style="display:none; position:fixed; inset:0; background:rgba(0,0,0,0.6); z-index:99; display:none; align-items:center; justify-content:center;">
    <div class="panel" style="width:500px; max-height:70vh; overflow-y:auto;">
      <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:14px;">
        <div class="panel-title" style="margin:0;">操作历史</div>
        <button class="btn btn-outline" onclick="hideHistory()" style="padding:6px 12px;">关闭</button>
      </div>
      <div id="historyList"></div>
    </div>
  </div>
</div>

<script>
let allFiles = [];
let previewData = [];
let currentFolder = "";
const PAGE_SIZE = 100;
let currentPage = 0;

// ── API helpers ────────────────────────────────────────────────────────────
async function api(endpoint, data) {
  const resp = await fetch(endpoint, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
  return resp.json();
}

function toast(msg, type = "success") {
  const el = document.createElement("div");
  el.className = `toast toast-${type}`;
  el.textContent = msg;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 3000);
}

// ── Folder Loading ─────────────────────────────────────────────────────────
async function loadFolder() {
  const folder = document.getElementById("folderPath").value.trim();
  if (!folder) { toast("请输入文件夹路径", "error"); return; }
  const recursive = document.getElementById("recursive").checked;

  const data = await api("/api/list", { folder, recursive });
  if (data.error) { toast(data.error, "error"); return; }

  allFiles = data.files;
  currentFolder = folder;
  toast(`已加载 ${allFiles.length} 个文件`);
  refreshPreview();
}

// ── Rule Management ────────────────────────────────────────────────────────
const MODES = [
  { value: "prefix", label: "添加前缀" },
  { value: "suffix", label: "添加后缀" },
  { value: "replace", label: "查找替换" },
  { value: "regex", label: "正则替换" },
  { value: "sequence", label: "序号命名" },
  { value: "extension", label: "更改扩展名" },
  { value: "case", label: "大小写转换" },
  { value: "remove", label: "删除字符" },
  { value: "insert", label: "插入文本" },
];

function addRule() {
  const container = document.getElementById("rulesContainer");
  const idx = container.children.length;
  const card = document.createElement("div");
  card.className = "rule-card";
  card.dataset.index = idx;
  card.innerHTML = `
    <select onchange="onModeChange(this)">
      ${MODES.map(m => `<option value="${m.value}">${m.label}</option>`).join("")}
    </select>
    <div class="rule-fields"></div>
    <button class="rule-remove" onclick="this.parentElement.remove(); autoPreview();">&times;</button>
  `;
  container.appendChild(card);
  onModeChange(card.querySelector("select"));
}

function onModeChange(sel) {
  const card = sel.closest(".rule-card");
  const fields = card.querySelector(".rule-fields");
  const mode = sel.value;

  const templates = {
    prefix: `<input type="text" data-field="prefix" placeholder="前缀文本" oninput="autoPreview()">`,
    suffix: `<input type="text" data-field="suffix" placeholder="后缀文本" oninput="autoPreview()">`,
    replace: `
      <input type="text" data-field="find" placeholder="查找" oninput="autoPreview()">
      <input type="text" data-field="replace" placeholder="替换为" oninput="autoPreview()">
      <label style="font-size:12px;color:var(--text2);display:flex;align-items:center;gap:4px;">
        <input type="checkbox" data-field="caseSensitive" checked> 区分大小写
      </label>`,
    regex: `
      <input type="text" data-field="pattern" placeholder="正则表达式" oninput="autoPreview()">
      <input type="text" data-field="replacement" placeholder="替换为 (支持 $1 $2)" oninput="autoPreview()">`,
    sequence: `
      <input type="text" data-field="template" placeholder="模板: {name}_{num}{ext}" value="{name}_{num}{ext}" oninput="autoPreview()">
      <input type="number" data-field="start" placeholder="起始号" value="1" style="width:80px" oninput="autoPreview()">
      <input type="number" data-field="padding" placeholder="补零位数" value="3" style="width:80px" oninput="autoPreview()">`,
    extension: `<input type="text" data-field="newExt" placeholder="新扩展名 (如 .png)" oninput="autoPreview()">`,
    case: `
      <select data-field="caseType" onchange="autoPreview()">
        <option value="lower">全部小写</option>
        <option value="upper">全部大写</option>
        <option value="title">首字母大写</option>
        <option value="capitalize">仅首字母大写</option>
      </select>`,
    remove: `<input type="text" data-field="chars" placeholder="要删除的字符" oninput="autoPreview()">`,
    insert: `
      <input type="text" data-field="text" placeholder="插入文本" oninput="autoPreview()">
      <input type="number" data-field="position" placeholder="位置(从0开始)" value="0" style="width:100px" oninput="autoPreview()">`,
  };
  fields.innerHTML = templates[mode] || "";
  autoPreview();
}

function getRules() {
  const cards = document.querySelectorAll(".rule-card");
  return Array.from(cards).map(card => {
    const mode = card.querySelector("select").value;
    const rule = { mode };
    card.querySelectorAll("[data-field]").forEach(el => {
      if (el.type === "checkbox") rule[el.dataset.field] = el.checked;
      else rule[el.dataset.field] = el.value;
    });
    return rule;
  });
}

// ── Preview ────────────────────────────────────────────────────────────────
let previewTimeout = null;
function autoPreview() {
  if (!allFiles.length) return;
  clearTimeout(previewTimeout);
  previewTimeout = setTimeout(refreshPreview, 300);
}

async function refreshPreview() {
  if (!allFiles.length) return;
  const rules = getRules();
  const filterExt = document.getElementById("filterExt").value.trim();

  const data = await api("/api/preview", {
    folder: currentFolder,
    files: allFiles,
    rules,
    filterExt,
  });

  previewData = data.preview;
  currentPage = 0;
  renderPreview();
  document.getElementById("previewPanel").style.display = "block";
  document.getElementById("emptyState").style.display = "none";
}

function renderPreview() {
  const body = document.getElementById("previewBody");
  const totalPages = Math.ceil(previewData.length / PAGE_SIZE);
  const start = currentPage * PAGE_SIZE;
  const pageData = previewData.slice(start, start + PAGE_SIZE);

  const changedCount = previewData.filter(p => p.changed).length;
  const conflictCount = previewData.filter(p => p.conflict).length;
  const unchangedCount = previewData.length - changedCount;

  document.getElementById("statsBar").innerHTML = `
    <span><span class="dot dot-green"></span> 将重命名: ${changedCount}</span>
    <span><span class="dot dot-gray"></span> 无变化: ${unchangedCount}</span>
    <span><span class="dot dot-red"></span> 冲突: ${conflictCount}</span>
    <span>共 ${previewData.length} 个文件</span>
  `;

  body.innerHTML = pageData.map(p => {
    const cls = p.conflict ? "conflict" : (p.changed ? "changed" : "unchanged");
    return `<tr class="${cls}">
      <td title="${p.original}">${esc(p.original)}</td>
      <td class="arrow">${p.changed ? "&#8594;" : ""}</td>
      <td title="${p.new}">${esc(p.new)}${p.conflict ? " &#9888;" : ""}</td>
      <td style="color:var(--text2)">${esc(p.dir)}</td>
    </tr>`;
  }).join("");

  // Pagination
  const pag = document.getElementById("pagination");
  if (totalPages > 1) {
    pag.innerHTML = `
      <button onclick="goPage(${currentPage - 1})" ${currentPage === 0 ? "disabled" : ""}>上一页</button>
      <span>第 ${currentPage + 1} / ${totalPages} 页</span>
      <button onclick="goPage(${currentPage + 1})" ${currentPage >= totalPages - 1 ? "disabled" : ""}>下一页</button>
    `;
  } else {
    pag.innerHTML = "";
  }

  document.getElementById("applyBtn").disabled = changedCount === 0;
}

function goPage(n) { currentPage = n; renderPreview(); }

function esc(s) {
  const d = document.createElement("div");
  d.textContent = s;
  return d.innerHTML;
}

// ── Apply ──────────────────────────────────────────────────────────────────
async function applyRenames() {
  const changedCount = previewData.filter(p => p.changed && !p.conflict).length;
  if (!confirm(`确定要重命名 ${changedCount} 个文件吗？`)) return;

  const data = await api("/api/apply", { folder: currentFolder, previews: previewData });
  if (data.error) {
    toast(data.error, "error");
    return;
  }
  toast(`成功重命名 ${data.renamed} 个文件`);
  if (data.errors && data.errors.length) {
    data.errors.forEach(e => console.warn(e));
    toast(`${data.errors.length} 个错误，查看控制台`, "error");
  }
  document.getElementById("undoBtn").disabled = false;
  loadFolder(); // Refresh
}

// ── Undo ───────────────────────────────────────────────────────────────────
async function undoLast() {
  if (!confirm("确定要撤销上次重命名操作吗？")) return;
  const data = await api("/api/undo", {});
  if (data.success) {
    toast(`已撤销，恢复了 ${data.restored} 个文件`);
    if (data.errors && data.errors.length) {
      toast(`${data.errors.length} 个撤销错误`, "error");
    }
    if (currentFolder) loadFolder();
  } else {
    toast(data.message, "error");
  }
}

// ── History ────────────────────────────────────────────────────────────────
async function showHistory() {
  const data = await api("/api/history", {});
  const list = document.getElementById("historyList");
  if (!data.history || data.history.length === 0) {
    list.innerHTML = '<div style="color:var(--text2);text-align:center;padding:20px;">暂无操作记录</div>';
  } else {
    list.innerHTML = data.history.map(h => `
      <div class="history-item">
        <div>
          <div>${h.folder}</div>
          <div class="time">${h.timestamp} · ${h.count} 个文件</div>
        </div>
      </div>
    `).join("");
  }
  const modal = document.getElementById("historyModal");
  modal.style.display = "flex";
}

function hideHistory() {
  document.getElementById("historyModal").style.display = "none";
}

// Init: add one rule by default
addRule();
</script>
</body>
</html>"""


# ─── HTTP Server ─────────────────────────────────────────────────────────────
class RenamerHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Suppress default logs
        pass

    def _send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html):
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length)) if length else {}

    def do_GET(self):
        self._send_html(HTML_PAGE)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        try:
            body = self._read_body()
        except (json.JSONDecodeError, ValueError):
            self._send_json({"error": "无效的请求数据"}, 400)
            return

        if path == "/api/list":
            folder = body.get("folder", "")
            recursive = body.get("recursive", False)
            if not os.path.isdir(folder):
                self._send_json({"error": f"文件夹不存在: {folder}"})
                return
            files = list_files(folder, recursive)
            self._send_json({"files": files, "count": len(files)})

        elif path == "/api/preview":
            folder = body.get("folder", "")
            files = body.get("files", [])
            rules = body.get("rules", [])
            filter_ext = body.get("filterExt", "")
            preview = preview_renames(folder, files, rules, filter_ext)
            self._send_json({"preview": preview})

        elif path == "/api/apply":
            folder = body.get("folder", "")
            previews = body.get("previews", [])
            result = apply_renames(folder, previews)
            self._send_json(result)

        elif path == "/api/undo":
            result = undo_last()
            self._send_json(result)

        elif path == "/api/history":
            self._send_json({"history": rename_history[::-1]})

        else:
            self._send_json({"error": "未知接口"}, 404)


def main():
    server = http.server.HTTPServer((HOST, PORT), RenamerHandler)
    url = f"http://{HOST}:{PORT}"
    print(f"批量文件重命名工具已启动")
    print(f"浏览器访问: {url}")
    print(f"按 Ctrl+C 停止服务\n")

    # Open browser after a short delay
    def open_browser():
        time.sleep(0.5)
        webbrowser.open(url)

    threading.Thread(target=open_browser, daemon=True).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止服务")
        server.server_close()


if __name__ == "__main__":
    main()
