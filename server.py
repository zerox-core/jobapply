"""jobapply 网申自动化试点 — FastAPI 后端（127.0.0.1:8899）。"""
from __future__ import annotations

import asyncio
import json
import random
import threading
import time
import uuid
from pathlib import Path

import yaml
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from jobapply import chat as chat_mod
from jobapply import filler as filler_mod
from jobapply import kb as kb_mod
from jobapply import notify as notify_mod
from jobapply import profile as profile_mod
from jobapply.browser_ctl import BrowserSession
from jobapply.llm import LLMChain

BASE = Path(__file__).resolve().parent
DATA = BASE / "data"
DATA.mkdir(exist_ok=True)
(BASE / "docs").mkdir(exist_ok=True)

CFG = yaml.safe_load((BASE / "config.yaml").read_text(encoding="utf-8"))

app = FastAPI(title="jobapply 网申自动化试点")
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")

_chain = None


def llm_chain():
    global _chain
    if _chain is None:
        hub = json.loads(Path(CFG["llm"]["hub_config"]).read_text(encoding="utf-8"))
        _chain = LLMChain.from_hub(hub, CFG["llm"]["chain"],
                                   cooldown_seconds=CFG["llm"].get("cooldown_seconds", 300))
    return _chain


# ---------------- 事件广播 ----------------
class Hub:
    def __init__(self):
        self.conns = []
        self.events = []

    async def connect(self, ws):
        await ws.accept()
        self.conns.append(ws)
        for e in self.events[-50:]:
            await ws.send_json(e)

    def disconnect(self, ws):
        if ws in self.conns:
            self.conns.remove(ws)

    def emit(self, kind, msg, **extra):
        e = {"ts": time.strftime("%H:%M:%S"), "kind": kind, "msg": msg, **extra}
        self.events.append(e)
        if loop is None:
            return
        dead = []
        for ws in list(self.conns):
            try:
                asyncio.run_coroutine_threadsafe(ws.send_json(e), loop)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


hub = Hub()
loop = None


@app.on_event("startup")
async def _startup():
    global loop
    loop = asyncio.get_running_loop()


def emit(kind, msg, **kw):
    try:
        hub.emit(kind, msg, **kw)
    except Exception:
        pass


def chat_say(text):
    """系统主动向对话面板说一句（缺口询问 / 完成通知）：写历史 + WS 实时推送。"""
    try:
        chat_mod.append("assistant", text)
        emit("chat", text)
    except Exception:
        pass


# ---------------- 数据存取 ----------------
def load_json(name, default):
    p = DATA / name
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return default


def save_json(name, obj):
    (DATA / name).write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------- 页面 ----------------
@app.get("/")
def index():
    return FileResponse(str(BASE / "static" / "index.html"))


@app.get("/demo", response_class=HTMLResponse)
def demo():
    return (BASE / "static" / "demo_form.html").read_text(encoding="utf-8")


# ---------------- 简历 ----------------
@app.post("/api/profile/upload")
async def upload_profile(request: Request, filename: str):
    raw = await request.body()
    src = DATA / ("resume_src" + Path(filename).suffix.lower())
    src.write_bytes(raw)
    text = profile_mod.extract_text(str(src))
    if len(text.strip()) < 20:
        return JSONResponse({"ok": False, "error": "简历文本过短，解析失败"}, status_code=400)
    emit("profile", f"已读取简历 {filename}，LLM 结构化中…")
    prof = profile_mod.structure_profile(text, llm_chain())
    prof["_source_file"] = filename
    save_json("profile.json", prof)
    emit("profile", f"简历结构化完成（{llm_chain().last_used}）")
    return {"ok": True, "profile": prof}


@app.get("/api/profile")
def get_profile():
    return {"ok": True, "profile": load_json("profile.json", None)}


@app.put("/api/profile")
async def put_profile(request: Request):
    prof = await request.json()
    save_json("profile.json", prof)
    emit("profile", "简历 profile 已手动保存")
    return {"ok": True}


# ---------------- 知识库（边干边沉淀：事实 + 平台规则 + 待补缺口） ----------------
@app.get("/api/kb")
def get_kb():
    return {"ok": True, "kb": kb_mod.load()}


@app.post("/api/kb/fact")
async def kb_fact(request: Request):
    body = await request.json()
    key = (body.get("key") or "").strip()
    if not key:
        return JSONResponse({"ok": False, "error": "缺少 key"}, status_code=400)
    kb_mod.set_fact(key, body.get("value") or "",
                    source=body.get("source") or "manual",
                    status=body.get("status") or "user_confirmed",
                    note=body.get("note") or "")
    emit("kb", f"知识库已更新：{key} = {str(body.get('value') or '')[:30]}")
    return {"ok": True}


@app.post("/api/kb/gap/resolve")
async def kb_gap_resolve(request: Request):
    body = await request.json()
    field = (body.get("field") or "").strip()
    if not field:
        return JSONResponse({"ok": False, "error": "缺少 field"}, status_code=400)
    key = kb_mod.resolve_gap(field, platform=body.get("platform") or "",
                             value=body.get("value") or "", source="api")
    emit("kb", f"缺口已补充：{field}" + (f"（存为事实 {key}，下次自动带上）" if key else "（仅摘除）"))
    return {"ok": True, "fact_key": key}


# ---------------- 对话助手（WebUI 内置 LLM 面板，备胎入口） ----------------
async def _execute_chat_actions(actions):
    """执行对话助手的动作：知识库写入 + 浏览器操作。每个动作独立容错，绝不中断整条回复。"""
    out = []
    for a in actions:
        if not isinstance(a, dict):
            continue
        op = a.get("op")
        try:
            if op == "set_fact":
                key = (a.get("key") or "").strip()
                if not key:
                    raise RuntimeError("缺少 key")
                kb_mod.set_fact(key, a.get("value") or "", source="chat",
                                status=a.get("status") or "user_confirmed",
                                note=a.get("note") or "")
                emit("kb", f"知识库已更新（对话）：{key}")
                out.append({"op": op, "ok": True,
                            "detail": f"已记住 {key}={str(a.get('value') or '')[:20]}"})
            elif op == "resolve_gap":
                field = (a.get("field") or "").strip()
                k = kb_mod.resolve_gap(field, platform=a.get("platform") or "",
                                       value=a.get("value") or "", source="chat")
                emit("kb", f"缺口已补充（对话）：{field}")
                out.append({"op": op, "ok": True,
                            "detail": f"缺口已补：{field}" + (f"→{k}" if k else "")})
            elif op == "open_url":
                url = (a.get("url") or "").strip()
                if not url:
                    raise RuntimeError("缺少 url")
                if not url.startswith(("http://", "https://")):
                    url = "https://" + url
                final = await asyncio.to_thread(_do_start, url)
                emit("session", f"浏览器已打开（对话）：{final}")
                out.append({"op": op, "ok": True, "detail": f"已打开 {final[:40]}"})
            elif op == "extract":
                draft = await asyncio.to_thread(_do_extract)
                out.append({"op": op, "ok": True,
                            "detail": f"预填完成 {len(draft['fills'])}/{len(draft['fields'])}，请到工作台核对"})
            elif op == "fill":
                draft = load_json("draft.json", {})
                fills = [{"idx": f["idx"], "value": f["value"]} for f in draft.get("fills", [])]
                if not fills:
                    raise RuntimeError("还没有预填草稿——先让我「读取表单」")
                results = await asyncio.to_thread(_do_fill, fills)
                ok_n = sum(1 for r in results if r["ok"])
                out.append({"op": op, "ok": True, "detail": f"已写入 {ok_n}/{len(results)} 个字段"})
            else:
                out.append({"op": str(op), "ok": False, "detail": "未知操作"})
        except Exception as e:
            out.append({"op": str(op), "ok": False, "detail": str(e)[:80]})
    return out


@app.post("/api/chat")
async def chat_api(request: Request):
    body = await request.json()
    text = (body.get("message") or "").strip()
    if not text:
        return JSONResponse({"ok": False, "error": "消息为空"}, status_code=400)
    prof = load_json("profile.json", None)
    try:
        result = await asyncio.to_thread(chat_mod.handle_message, text, llm_chain(), prof)
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"LLM 调用失败：{str(e)[:200]}"}, status_code=500)
    executed = await _execute_chat_actions(result.get("actions") or [])
    chat_mod.patch_last_actions(executed)
    return {"ok": True, "reply": result["reply"], "actions": executed}


@app.get("/api/chat/history")
def chat_history():
    return {"ok": True, "history": chat_mod.history()}


# ---------------- 任务 ----------------
@app.get("/api/jobs")
def get_jobs():
    return {"ok": True, "jobs": load_json("jobs.json", [])}


@app.post("/api/jobs")
async def add_job(request: Request):
    body = await request.json()
    jobs = load_json("jobs.json", [])
    job = {"id": uuid.uuid4().hex[:8],
           "company": (body.get("company") or "").strip(),
           "position": (body.get("position") or "").strip(),
           "url": (body.get("url") or "").strip(),
           "status": "待投递",
           "created_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    if not job["company"] or not job["url"]:
        return JSONResponse({"ok": False, "error": "公司和网申链接必填"}, status_code=400)
    jobs.append(job)
    save_json("jobs.json", jobs)
    emit("jobs", f"新增任务：{job['company']} {job['position']}")
    return {"ok": True, "job": job}


@app.post("/api/jobs/delete")
async def jobs_delete(request: Request):
    body = await request.json()
    ids = set(body.get("ids") or [])
    if not ids:
        return JSONResponse({"ok": False, "error": "未选择任务"}, status_code=400)
    jobs = load_json("jobs.json", [])
    jobs2, removed, pool_ids = [], [], []
    for j in jobs:
        if j.get("id") in ids:
            removed.append(j)
            if j.get("pool_id"):
                pool_ids.append(j["pool_id"])
        else:
            jobs2.append(j)
    save_json("jobs.json", jobs2)
    if pool_ids:  # 还原职位库条目的加入状态
        pool = load_json("pool.json", [])
        for p in pool:
            if p.get("id") in pool_ids and p.get("status") == "已加入任务":
                p["status"] = "待投递"
        save_json("pool.json", pool)
    msg = f"已删除 {len(removed)} 个任务"
    if pool_ids:
        msg += f"（{len(pool_ids)} 条职位库记录状态已还原）"
    emit("jobs", msg)
    return {"ok": True, "deleted": len(removed), "msg": msg}


# ---------------- 职位库（飞书表格「2027届秋招校招信息汇总」导入，451 条） ----------------
def _today():
    return time.strftime("%Y-%m-%d")


def _dead(p, today):
    """已截止 或 表格标注已失效。"""
    if p.get("apply_state") == "已失效":
        return True
    return bool(p.get("deadline")) and p["deadline"] < today


# 人工整理标记：用户逐条检查后手动打的状态（mark 字段，空 = 未检查）
POOL_MARKS = {"可用", "链接不对", "打不开", "已过期", "不合适"}
POOL_BAD_MARKS = {"链接不对", "打不开", "已过期", "不合适"}


@app.get("/api/pool")
def get_pool(q: str = "", match: str = "", link_type: str = "", state: str = "",
             mark: str = "", has_url: bool = False, hide_expired: bool = False,
             sort: str = "", seed: str = "",
             page: int = 1, size: int = 30):
    pool = load_json("pool.json", [])
    ql = (q or "").strip().lower()
    today = _today()
    out = []
    for p in pool:
        expired = _dead(p, today)
        if hide_expired and expired:
            continue
        if state and (p.get("apply_state") or "") != state:
            continue
        if mark == "__none__":
            if p.get("mark"):
                continue
        elif mark and (p.get("mark") or "") != mark:
            continue
        if link_type and p.get("link_type") != link_type:
            continue
        if match and p.get("match") != match:
            continue
        if has_url and not p.get("url"):
            continue
        if ql:
            hay = (p.get("company", "") + p.get("title", "") + p.get("direction", "")
                   + p.get("location", "") + p.get("note", "")).lower()
            if ql not in hay:
                continue
        item = dict(p)
        item["expired"] = expired
        out.append(item)
    # 排序：截止日期升/降序（空截止日期固定排最后）；random 用同一 seed 打乱（翻页稳定）
    if sort in ("deadline_asc", "deadline_desc"):
        withdl = [p for p in out if p.get("deadline")]
        nodl = [p for p in out if not p.get("deadline")]
        withdl.sort(key=lambda p: p["deadline"], reverse=(sort == "deadline_desc"))
        out = withdl + nodl
    elif sort == "random":
        random.Random(seed or str(_today())).shuffle(out)
    total = len(out)
    page = max(1, page)
    size = min(max(1, size), 100)
    return {"ok": True, "total": total, "page": page, "size": size,
            "items": out[(page - 1) * size: page * size]}


@app.get("/api/pool/summary")
def pool_summary():
    pool = load_json("pool.json", [])
    today = _today()
    by_match, by_link, by_state, by_mark = {}, {}, {}, {}
    expired = 0
    for p in pool:
        by_match[p.get("match") or "未评估"] = by_match.get(p.get("match") or "未评估", 0) + 1
        lt = p.get("link_type") or ("none" if not p.get("url") else "entry")
        by_link[lt] = by_link.get(lt, 0) + 1
        st = p.get("apply_state") or "未知"
        by_state[st] = by_state.get(st, 0) + 1
        mk = p.get("mark") or "未检查"
        by_mark[mk] = by_mark.get(mk, 0) + 1
        if _dead(p, today):
            expired += 1
    no_url = sum(1 for p in pool if not p.get("url"))
    return {"ok": True, "total": len(pool), "by_match": by_match, "by_link": by_link,
            "by_state": by_state, "by_mark": by_mark,
            "no_url": no_url, "expired": expired, "today": today}


@app.post("/api/pool/pick")
async def pool_pick(request: Request):
    body = await request.json()
    ids = set(body.get("ids") or [])
    if not ids:
        return JSONResponse({"ok": False, "error": "未选择任何职位"}, status_code=400)
    pool = load_json("pool.json", [])
    jobs = load_json("jobs.json", [])
    existing = {(j.get("company", ""), j.get("position", "")) for j in jobs}
    picked_ids = set()
    added, skipped = 0, 0
    today = _today()
    for p in pool:
        if p.get("id") not in ids:
            continue
        expired = _dead(p, today)
        if expired or not p.get("url") or p.get("mark") in POOL_BAD_MARKS \
                or (p.get("company", ""), p.get("title", "")) in existing:
            skipped += 1
            continue
        jobs.append({"id": uuid.uuid4().hex[:8],
                     "company": p.get("company", ""),
                     "position": p.get("title", ""),
                     "url": p["url"],
                     "status": "待投递",
                     "pool_id": p["id"],
                     "deadline": p.get("deadline", ""),
                     "match": p.get("match", ""),
                     "created_at": time.strftime("%Y-%m-%d %H:%M:%S")})
        existing.add((p.get("company", ""), p.get("title", "")))
        picked_ids.add(p["id"])
        added += 1
    save_json("jobs.json", jobs)
    # 回写职位库状态
    changed = False
    for p in pool:
        if p.get("id") in picked_ids:
            p["status"] = "已加入任务"
            changed = True
    if changed:
        save_json("pool.json", pool)
    emit("jobs", f"从职位库加入 {added} 个任务（跳过 {skipped}：重复或无投递链接）")
    return {"ok": True, "added": added, "skipped": skipped}


@app.post("/api/pool/mark")
async def pool_mark(request: Request):
    """人工整理：给职位库条目打/清除标记（可用 / 链接不对 / 打不开 / 已过期 / 不合适）。"""
    body = await request.json()
    ids = set(body.get("ids") or [])
    mark = (body.get("mark") or "").strip()
    if not ids:
        return JSONResponse({"ok": False, "error": "未选择任何职位"}, status_code=400)
    if mark and mark not in POOL_MARKS:
        return JSONResponse({"ok": False, "error": f"非法标记：{mark}"}, status_code=400)
    pool = load_json("pool.json", [])
    n = 0
    for p in pool:
        if p.get("id") in ids:
            if mark:
                p["mark"] = mark
            else:
                p.pop("mark", None)
            n += 1
    save_json("pool.json", pool)
    emit("pool", f"已把 {n} 条职位标记为「{mark or '未检查'}」")
    return {"ok": True, "marked": n}


@app.post("/api/pool/delete")
async def pool_delete(request: Request):
    """人工整理：从职位库删除条目（不影响已加入任务的任务）。"""
    body = await request.json()
    ids = set(body.get("ids") or [])
    if not ids:
        return JSONResponse({"ok": False, "error": "未选择任何职位"}, status_code=400)
    pool = load_json("pool.json", [])
    pool2 = [p for p in pool if p.get("id") not in ids]
    save_json("pool.json", pool2)
    n = len(pool) - len(pool2)
    emit("pool", f"已从职位库删除 {n} 条记录")
    return {"ok": True, "deleted": n}


# ---------------- 浏览器会话 ----------------
session = None
session_lock = threading.Lock()


def _domain_of(url):
    from urllib.parse import urlparse
    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:
        return ""


def _do_start(url, headless=False):
    """同步版启动浏览器（端点与对话助手共用）。"""
    global session
    with session_lock:
        if session is not None:
            try:
                session.close()
            except Exception:
                pass
        session = BrowserSession(str(BASE / "browser_profile"), headless=headless,
                                 cdp_endpoint=str(CFG.get("cdp_endpoint") or ""))
        session.start()
        return session.goto(url)


def _safe_goto(url):
    """带自愈的打开：浏览器窗口被关掉/崩溃后，goto 会抛 TargetClosedError——
    此时清掉僵尸会话、重开浏览器（登录态在 browser_profile 里，不丢）再试一次。"""
    global session
    if session is not None:
        try:
            return session.goto(url)
        except Exception as e:
            emit("session", f"检测到浏览器已关闭（{str(e)[:60]}…），自动重开后重试")
            try:
                session.close()
            except Exception:
                pass
            session = None
    return _do_start(url)


@app.post("/api/session/start")
async def session_start(request: Request):
    body = await request.json()
    url = (body.get("url") or "").strip()
    headless = bool(body.get("headless", False))
    if not url:
        return JSONResponse({"ok": False, "error": "缺少 url"}, status_code=400)
    try:
        final_url = await asyncio.to_thread(_do_start, url, headless)
    except Exception as e:
        emit("error", f"浏览器启动失败：{str(e)[:200]}")
        return JSONResponse({"ok": False, "error": f"浏览器启动失败：{str(e)[:200]}"}, status_code=502)
    emit("session", f"浏览器已打开：{final_url}")
    return {"ok": True, "url": final_url}


@app.post("/api/session/open")
async def session_open(request: Request):
    """在自动化浏览器里打开链接：会话已在跑就复用（共享登录态），没启动才新开。"""
    body = await request.json()
    url = (body.get("url") or "").strip()
    if not url:
        return JSONResponse({"ok": False, "error": "缺少 url"}, status_code=400)
    try:
        final_url = await asyncio.to_thread(_safe_goto, url)
    except Exception as e:
        msg = str(e)[:200]
        friendly = "打开失败：浏览器可能已被关闭，已尝试自动重开但仍失败；请再点一次，若仍失败请重启服务"
        if "net::ERR" in msg or "Timeout" in msg:
            friendly = f"打开失败：目标网站无法访问或响应超时（{msg}）"
        emit("error", friendly)
        return JSONResponse({"ok": False, "error": friendly}, status_code=502)
    emit("session", f"已在自动化浏览器打开：{final_url}")
    return {"ok": True, "url": final_url}


ENTER_APPLY_JS = r"""
(() => {
  const KEYS = ['立即申请', '投递职位', '申请职位', '投递简历', '立即投递', '马上申请', '我要申请', '立即报名', '立即应聘', '应聘', '网申', 'Apply Now', 'Apply'];
  const NEG = ['已申请', '已投递', '查看进度', '收藏', '分享', '下载', '预览', '登录', '注册'];
  const cands = [];
  const els = document.querySelectorAll('button, a, div[role=button], span[role=button], input[type=button], input[type=submit]');
  els.forEach(el => {
    const t = (((el.innerText || '') + (el.value || ''))).trim().replace(/\s+/g, ' ');
    if (!t || t.length > 20) return;
    if (NEG.some(k => t.includes(k))) return;
    let score = -1;
    KEYS.forEach((k, i) => {
      if (t === k) score = Math.max(score, 1000 - i);
      else if (t.includes(k)) score = Math.max(score, 500 - i);
    });
    if (score < 0) return;
    const r = el.getBoundingClientRect();
    if (r.width < 20 || r.height < 10) return;
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden') return;
    cands.push({score, text: t, el});
  });
  if (!cands.length) return 'no_entry';
  cands.sort((a, b) => b.score - a.score);
  cands[0].el.scrollIntoView({block: 'center'});
  cands[0].el.click();
  return 'clicked:' + cands[0].text;
})()
"""


@app.post("/api/session/enter_apply")
async def session_enter_apply():
    """AI 找投递入口：在当前页定位「立即申请 / 投递简历」类按钮并点击，
    然后切到最新标签（弹新页场景）并等加载——只负责点到表单页，绝不提交。"""
    if session is None:
        return JSONResponse({"ok": False, "error": "浏览器未启动，请先打开职位链接"}, status_code=400)
    try:
        r = await asyncio.to_thread(session.eval_js, ENTER_APPLY_JS)
    except Exception as e:
        return JSONResponse({"ok": False, "error": repr(e)[:300]}, status_code=502)
    if not (isinstance(r, str) and r.startswith("clicked:")):
        return {"ok": False, "error": "当前页没找到「申请 / 投递」类按钮——先进到职位详情页再试"}
    await asyncio.sleep(4)
    try:
        url = await asyncio.to_thread(session.switch_to_newest)
    except Exception as e:
        return JSONResponse({"ok": False, "error": repr(e)[:300]}, status_code=502)
    emit("session", f"已点投递入口「{r[8:]}」，当前页：{url}")
    return {"ok": True, "clicked": r[8:], "url": url}


def _do_extract():
    """同步版读取+预填（端点与对话助手共用）。失败抛 RuntimeError（friendly 文案）。"""
    if session is None:
        raise RuntimeError("浏览器未启动——先打开网申页面")
    try:
        fields = session.extract_fields()
    except Exception as e:
        msg = repr(e)
        friendly = "页面正在跳转，请等浏览器加载完成后再试一次"
        if "Execution context was destroyed" not in msg and "navigation" not in msg:
            friendly = f"读取页面失败：{msg[:200]}"
        emit("error", f"读取表单失败：{friendly}")
        raise RuntimeError(friendly)
    if not fields:
        raise RuntimeError("当前页面没有识别到表单字段——请先在浏览器里登录并进入网申表单页")
    emit("session", f"识别到 {len(fields)} 个字段，LLM 生成预填值…")
    prof = load_json("profile.json", None)
    if not prof:
        raise RuntimeError("请先上传并结构化简历")
    jobs = load_json("jobs.json", [])
    cur = jobs[-1] if jobs else {}
    fills = filler_mod.map_fields(fields, prof, cur, llm_chain())
    page_url = session.current_url()
    # 知识库回退链：LLM/简历没覆盖的字段，用沉淀的事实补充；仍无值的记缺口
    fills, kb_gaps, kb_added = kb_mod.backfill_fills(fills, fields, page_url)
    draft = {"fields": fields, "fills": fills, "page_url": page_url, "gaps": kb_gaps}
    save_json("draft.json", draft)
    if kb_added:
        emit("session", f"知识库回填 {kb_added} 个字段（简历未覆盖，来自历史沉淀）")
    if kb_gaps:
        names = "、".join(g["field"] for g in kb_gaps)
        emit("gap", f"待补缺口：{names}——补充后下次自动带上", gaps=kb_gaps)
        chat_say(f"这几个字段简历和知识库里都没有：{names}。你直接回复我（比如「GPA 3.6」），"
                 "我会记住并用于这次和以后的填写。")
    emit("session", f"预填完成：{len(fills)}/{len(fields)} 个字段待核对（{llm_chain().last_used}）")
    notify_mod.notify("jobapply 预填完成",
                      f"{len(fills)}/{len(fields)} 个字段待核对；页面保持不动，请到浏览器逐项核对后手动提交")
    return draft


@app.post("/api/session/extract")
async def session_extract():
    try:
        draft = await asyncio.to_thread(_do_extract)
    except RuntimeError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    return {"ok": True, "draft": draft}


def _do_fill(fills):
    """同步版写入（端点与对话助手共用）。失败抛 RuntimeError。"""
    if session is None:
        raise RuntimeError("浏览器未启动")
    draft = load_json("draft.json", {})
    by_idx = {f["idx"]: f for f in draft.get("fields", [])}
    total = len(draft.get("fields", []))
    attach = CFG.get("resume_attachment") or ""
    results = []
    for item in fills:
        idx = item.get("idx")
        val = item.get("value")
        f = by_idx.get(idx, {})
        label = (f.get("label") or "")[:20]
        fb = {"id": f.get("id") or "", "name": f.get("name") or "",
              "placeholder": f.get("placeholder") or ""}
        try:
            if (f.get("type") or "").lower() == "file":
                if not attach or not Path(attach).exists():
                    results.append({"idx": idx, "ok": False, "error": "未配置 resume_attachment 附件路径"})
                    emit("fill", f"字段 {idx}（{label}）跳过：未配置附件")
                    continue
                r = session.upload_file(idx, attach, fb, total)
            else:
                r = session.fill_field(idx, val, fb, total)
            ok = isinstance(r, str) and r.startswith("ok")
            via = r.split(":", 1)[1] if ok and ":" in r else ""
            results.append({"idx": idx, "ok": ok, "error": "" if ok else str(r), "via": via})
            emit("fill", f"字段 {idx}（{label}）" + (f"写入成功（{via}）" if ok else f"写入失败：{r}"))
        except Exception as e:
            results.append({"idx": idx, "ok": False, "error": repr(e)})
            emit("fill", f"字段 {idx}（{label}）异常：{e!r}")
    reports = load_json("fill_reports.json", [])
    reports.append({"ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "page": session.current_url(),
                    "results": results})
    save_json("fill_reports.json", reports)
    ok_n = sum(1 for r in results if r["ok"])
    emit("fill", f"本轮写入完成 {ok_n}/{len(results)}；请在浏览器逐项核对后手动提交")
    notify_mod.notify("jobapply 写入完成",
                      f"已写入 {ok_n}/{len(results)} 个字段；页面保持不动，请到浏览器逐项核对后手动提交（系统绝不自动提交）")
    chat_say(f"已把 {ok_n}/{len(results)} 个字段写入页面。页面保持不动，请到浏览器逐项核对后手动提交；"
             "有填错或填不进去的告诉我，我来处理。")
    return results


@app.post("/api/session/fill")
async def session_fill(request: Request):
    body = await request.json()
    fills = body.get("fills", [])
    try:
        results = await asyncio.to_thread(_do_fill, fills)
    except RuntimeError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    return {"ok": True, "results": results}


@app.post("/api/session/eval")
async def session_eval(request: Request):
    if session is None:
        return JSONResponse({"ok": False, "error": "浏览器未启动"}, status_code=400)
    body = await request.json()
    js = body.get("js") or ""
    arg = body.get("arg")
    if not js:
        return JSONResponse({"ok": False, "error": "缺少 js"}, status_code=400)
    try:
        r = await asyncio.to_thread(session.eval_js, js, arg)
        return {"ok": True, "result": r}
    except Exception as e:
        return JSONResponse({"ok": False, "error": repr(e)[:300]}, status_code=500)


@app.post("/api/session/close")
def session_close():
    global session
    with session_lock:
        if session is not None:
            session.close()
            session = None
    emit("session", "浏览器已关闭")
    return {"ok": True}


# ---------------- 登录态管理（browser_profile 持久化，登录一次长期有效） ----------------
@app.post("/api/session/login")
async def session_login(request: Request):
    """打开持久化浏览器到指定站点供手动登录（微信 / QQ 扫码等）；登录态存 browser_profile。"""
    body = await request.json()
    url = (body.get("url") or "").strip()
    if not url:
        return JSONResponse({"ok": False, "error": "缺少 url"}, status_code=400)
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    def _open():
        global session
        with session_lock:
            if session is None:  # 复用已开的浏览器，避免反复拉起
                session = BrowserSession(str(BASE / "browser_profile"), headless=False)
                session.start()
            return session.goto(url)

    final_url = await asyncio.to_thread(_open)
    dom = _domain_of(final_url)
    logins = load_json("logins.json", [])
    logins = [x for x in logins if x.get("domain") != dom]
    logins.insert(0, {"domain": dom, "url": url, "last_at": time.strftime("%Y-%m-%d %H:%M")})
    save_json("logins.json", logins[:20])
    emit("session", f"已打开 {dom} ——请在浏览器里完成登录（扫码/验证码），登录态会自动保存，下次投递直接复用")
    return {"ok": True, "url": final_url, "domain": dom}


@app.get("/api/session/logins")
def login_list():
    return {"ok": True, "logins": load_json("logins.json", [])}


@app.post("/api/session/logins/forget")
async def login_forget(request: Request):
    body = await request.json()
    dom = (body.get("domain") or "").strip().lower()
    if not dom:
        return JSONResponse({"ok": False, "error": "缺少 domain"}, status_code=400)
    logins = load_json("logins.json", [])
    logins2 = [x for x in logins if x.get("domain") != dom]
    save_json("logins.json", logins2)
    cleared = False
    if session is not None:
        try:
            r = await asyncio.to_thread(session.clear_cookies, dom)
            cleared = (r == "ok")
        except Exception:
            cleared = False
    emit("session", f"已移除 {dom} 的登录记录" + ("，并清理了该站点 Cookie" if cleared else "（Cookie 未清理或浏览器未启动）"))
    return {"ok": True, "removed": len(logins) - len(logins2), "cookies_cleared": cleared}


@app.get("/api/status")
def status():
    return {"ok": True,
            "session": session is not None,
            "profile": load_json("profile.json", None) is not None,
            "jobs": len(load_json("jobs.json", [])),
            "pool": len(load_json("pool.json", [])),
            "logins": len(load_json("logins.json", []))}


@app.websocket("/ws")
async def ws(websocket: WebSocket):
    await hub.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        hub.disconnect(websocket)
    except Exception:
        hub.disconnect(websocket)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=CFG.get("port", 8899), log_level="warning")
