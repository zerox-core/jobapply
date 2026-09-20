# -*- coding: utf-8 -*-
"""E2E 试点验证：演示表单 + 真实 LLM 链 + 真实 Chromium（headless）。

跑通链路：启动浏览器 → 抽取字段 → LLM 映射 → 校验 → 写入页面 → 回读 DOM 断言。
日志自写文件（后台运行时 stdout 不可靠）。
"""
import json
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

LOG = BASE / "data" / "e2e_log.txt"
RESULT = BASE / "data" / "e2e_result.json"
LOG.parent.mkdir(exist_ok=True)


def log(msg):
    line = time.strftime("%H:%M:%S ") + str(msg)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def main():
    import yaml
    from jobapply.llm import LLMChain
    from jobapply.browser_ctl import BrowserSession
    from jobapply import filler as filler_mod

    log("== E2E 开始 ==")
    cfg = yaml.safe_load((BASE / "config.yaml").read_text(encoding="utf-8"))
    hub = json.loads(Path(cfg["llm"]["hub_config"]).read_text(encoding="utf-8"))
    chain = LLMChain.from_hub(hub, cfg["llm"]["chain"],
                              cooldown_seconds=cfg["llm"].get("cooldown_seconds", 300))

    profile = {
        "basic": {"name": "张三", "gender": "男", "birth": "2003-05-12",
                  "phone": "13800001111", "email": "zhangsan@example.com",
                  "political": "共青团员"},
        "education": [{"school": "山东大学", "degree": "本科", "major": "计算机科学与技术",
                       "start": "2022-09", "end": "2026-06", "gpa": "3.7"}],
        "internships": [{"company": "示例科技", "role": "后端实习生", "start": "2025-06",
                         "end": "2025-12", "description": "负责订单服务接口开发与性能优化"}],
        "projects": [{"name": "校园二手交易平台", "role": "后端负责人", "start": "2024-03",
                      "end": "2024-09", "description": "基于 FastAPI + MySQL 实现交易闭环"}],
        "skills": ["Python", "FastAPI", "MySQL", "Redis"],
        "self_eval": "扎实的后端基础，主导过完整项目交付。",
        "expected": {"city": "北京", "position": "后端开发工程师"},
    }
    job = {"company": "演示科技", "position": "后端开发工程师"}

    session = BrowserSession(str(BASE / "data" / "e2e_browser_profile"), headless=True)
    session.start()
    log("浏览器已启动（headless）")
    try:
        session.goto("file:///" + str(BASE / "static" / "demo_form.html").replace("\\", "/"))
        fields = session.extract_fields()
        log(f"抽取字段 {len(fields)} 个")
        assert len(fields) >= 14, f"字段数不足：{len(fields)}"

        t0 = time.time()
        fills = filler_mod.map_fields(fields, profile, job, chain)
        log(f"LLM 映射 {len(fills)} 项，用时 {time.time() - t0:.1f}s，渠道 {chain.last_used}")
        assert fills, "LLM 未给出任何填写项"

        label_of = {f["idx"]: f.get("label", "") for f in fields}
        name_fill = [f for f in fills if "姓名" in label_of.get(f["idx"], "")]
        assert name_fill and name_fill[0]["value"] == "张三", f"姓名映射异常：{name_fill}"

        results = []
        for item in fills:
            f0 = next(f for f in fields if f["idx"] == item["idx"])
            if (f0.get("type") or "").lower() == "file":
                results.append({"idx": item["idx"], "ok": True, "skipped": "e2e 不测附件"})
                continue
            r = session.fill_field(item["idx"], item["value"])
            results.append({"idx": item["idx"], "ok": r == "ok", "raw": r})
        ok_n = sum(1 for r in results if r["ok"])
        log(f"写入 {ok_n}/{len(results)} 项成功")

        # 回读 DOM 验证
        check_js = """
        (idx) => { const el = document.querySelector(`[data-ja-idx="${idx}"]`);
                   return el ? (el.type === 'radio' || el.type === 'checkbox' ? el.checked : el.value) : null; }
        """
        name_idx = name_fill[0]["idx"]
        dom_name = session.eval_js(check_js, name_idx)
        log(f"姓名回读：{dom_name}")
        assert dom_name == "张三", f"姓名回读不符：{dom_name!r}"

        summary = {
            "fields": len(fields), "fills": len(fills),
            "write_ok": ok_n, "write_total": len(results),
            "llm": chain.last_used,
            "name_check": str(dom_name),
            "verdict": "PASS" if (ok_n >= len(results) - 1) else "FAIL",
        }
    finally:
        session.close()
    log("== E2E 结束：" + json.dumps(summary, ensure_ascii=False) + " ==")
    RESULT.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        log("E2E 异常：\n" + traceback.format_exc())
        RESULT.write_text(json.dumps({"verdict": "ERROR"}, ensure_ascii=False), encoding="utf-8")
        raise
