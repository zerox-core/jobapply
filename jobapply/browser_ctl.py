"""Playwright 持久化浏览器会话（独立线程，请求驱动）。

登录态保存在 browser_profile/ 目录，每个企业只需手动登录一次。
"""
from __future__ import annotations

import queue
import threading

from .form_reader import EXTRACT_JS

# 字段定位公共段：优先 data-ja-idx；SPA（如 Moka）重渲染会清掉标记，
# 退而按 extract 时记录的 id / name / placeholder 重新定位；
# 最后兜底：可填字段总数与 extract 时一致时按枚举顺序取第 idx 个。
_LOCATE_JS = r"""
  const SKIP = ['hidden','submit','button','image','reset','password'];
  function visibleFields() {
    return [...document.querySelectorAll('input,select,textarea')].filter(e => {
      const tag = e.tagName.toLowerCase();
      const type = (tag === 'input' ? (e.getAttribute('type') || 'text') : tag).toLowerCase();
      if (SKIP.includes(type)) return false;
      const style = getComputedStyle(e);
      const rect = e.getBoundingClientRect();
      return !(style.display === 'none' || style.visibility === 'hidden' || rect.width === 0);
    });
  }
  let el = document.querySelector(`[data-ja-idx="${idx}"]`);
  let via = 'idx';
  if (!el && fb) {
    const els = visibleFields();
    if (fb.id) { el = els.find(e => e.id === fb.id) || null; if (el) via = 'id'; }
    if (!el && fb.name) { el = els.find(e => e.name === fb.name) || null; if (el) via = 'name'; }
    if (!el && fb.placeholder) { el = els.find(e => (e.placeholder || '') === fb.placeholder) || null; if (el) via = 'placeholder'; }
    if (!el && total && els.length === total && idx < els.length) { el = els[idx]; via = 'order'; }
  }
"""

FILL_JS = "([idx, value, fb, total]) => {\n" + _LOCATE_JS + r"""
  if (!el) return 'not_found';
  const tag = el.tagName.toLowerCase();
  const type = (el.getAttribute('type') || '').toLowerCase();
  if (tag === 'select') {
    const opt = [...el.options].find(o => o.text.trim() === value || o.value === value);
    if (!opt) return 'option_not_found';
    const setter = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value').set;
    setter.call(el, opt.value);
  } else if (type === 'radio' || type === 'checkbox') {
    el.checked = (value === true || value === 'true' || value === '1' || value === 1);
  } else {
    const proto = tag === 'textarea' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
    setter.call(el, value);
  }
  el.dispatchEvent(new Event('input', {bubbles: true}));
  el.dispatchEvent(new Event('change', {bubbles: true}));
  return 'ok:' + via;
}
"""

LOCATE_JS = "([idx, fb, total]) => {\n" + _LOCATE_JS + r"""
  if (!el) return 'not_found';
  el.setAttribute('data-ja-idx', String(idx));
  return 'ok:' + via;
}
"""


class BrowserSession:
    """worker 线程独占 Playwright；公开方法把操作投到队列里同步等待结果。

    cdp_endpoint 非空时改为「接管模式」：连接用户以 --remote-debugging-port
    启动的日常浏览器，登录态天然与用户共享；此时 profile_dir 不生效。
    """

    def __init__(self, profile_dir: str, headless: bool = False, cdp_endpoint: str = ""):
        self.profile_dir = profile_dir
        self.headless = headless
        self.cdp_endpoint = (cdp_endpoint or "").strip()
        self._cmd = queue.Queue()
        self._ready = threading.Event()
        self._err = None
        self._thread = threading.Thread(target=self._worker, daemon=True)

    def start(self):
        self._thread.start()
        self._ready.wait(timeout=60)
        if self._err:
            raise RuntimeError(self._err)

    # ---------------- worker ----------------
    def _worker(self):
        from playwright.sync_api import sync_playwright
        try:
            self._pw = sync_playwright().start()
            if self.cdp_endpoint:
                # 接管模式：连接用户日常浏览器；关闭时只断开连接，绝不关掉用户的浏览器
                self._browser = self._pw.chromium.connect_over_cdp(self.cdp_endpoint)
                self._ctx = self._browser.contexts[0] if self._browser.contexts else self._browser.new_context()
            else:
                self._browser = None
                self._ctx = self._pw.chromium.launch_persistent_context(
                    self.profile_dir, headless=self.headless,
                    viewport={"width": 1280, "height": 900})
            self._page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
        except Exception as e:
            self._err = repr(e)
            self._ready.set()
            return
        self._ready.set()
        while True:
            op, args, ev, box = self._cmd.get()
            if op == "close":
                try:
                    if self._browser is not None:
                        self._browser.close()  # CDP 连接下 = 断开连接，不会关闭用户浏览器
                    else:
                        self._ctx.close()
                    self._pw.stop()
                except Exception:
                    pass
                ev.set()
                return
            try:
                box["r"] = getattr(self, "_op_" + op)(*args)
            except Exception as e:
                box["e"] = repr(e)
            ev.set()

    def _call(self, op, *args, timeout=120):
        ev = threading.Event()
        box = {}
        self._cmd.put((op, args, ev, box))
        if not ev.wait(timeout):
            raise RuntimeError(f"浏览器操作超时：{op}")
        if "e" in box:
            raise RuntimeError(box["e"])
        return box.get("r")

    # ---------------- ops（worker 线程内执行） ----------------
    def _safe_eval(self, fn, *args):
        """页面跳转瞬间 evaluate 会抛 'Execution context was destroyed'，等待加载后重试。"""
        last = None
        for _ in range(3):
            try:
                return fn(*args)
            except Exception as e:
                last = e
                msg = repr(e)
                retryable = ("Execution context was destroyed" in msg
                             or "navigation" in msg
                             or "context or browser has been closed" in msg)
                if not retryable:
                    raise
                try:
                    self._page.wait_for_load_state("domcontentloaded", timeout=15000)
                except Exception:
                    pass
        raise last

    def _op_goto(self, url):
        self._page.goto(url, wait_until="domcontentloaded", timeout=60000)
        try:  # 尽量等 load 完成，让站内自动跳转先走完（超时不视为失败）
            self._page.wait_for_load_state("load", timeout=15000)
        except Exception:
            pass
        return self._page.url

    def _op_extract(self):
        return self._safe_eval(self._page.evaluate, EXTRACT_JS)

    def _op_fill(self, idx, value, fb, total):
        return self._safe_eval(self._page.evaluate, FILL_JS, [idx, value, fb, total])

    def _op_upload(self, idx, path, fb, total):
        r = self._safe_eval(self._page.evaluate, LOCATE_JS, [idx, fb, total])
        if not (isinstance(r, str) and r.startswith("ok")):
            return r
        self._page.set_input_files(f'[data-ja-idx="{idx}"]', path)
        return r

    def _op_url(self):
        return self._page.url

    def _op_eval(self, js, arg):
        return self._safe_eval(self._page.evaluate, js, arg)

    def _op_clear_cookies(self, domain):
        try:
            self._ctx.clear_cookies(domain=domain)
            return "ok"
        except TypeError:
            return "unsupported"

    # ---------------- 公开方法 ----------------
    def goto(self, url):
        return self._call("goto", url)

    def eval_js(self, js, arg=None):
        return self._call("eval", js, arg)

    def extract_fields(self):
        return self._call("extract")

    def fill_field(self, idx, value, fb=None, total=0):
        return self._call("fill", idx, value, fb, total)

    def upload_file(self, idx, path, fb=None, total=0):
        return self._call("upload", idx, path, fb, total)

    def current_url(self):
        return self._call("url")

    def clear_cookies(self, domain):
        return self._call("clear_cookies", domain)

    def close(self):
        try:
            self._call("close", timeout=15)
        except Exception:
            pass
