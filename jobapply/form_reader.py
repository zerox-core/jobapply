"""页面表单字段抽取（Playwright 注入 JS）。"""

EXTRACT_JS = r"""
(() => {
  const els = [...document.querySelectorAll('input,select,textarea')];
  const SKIP = ['hidden','submit','button','image','reset','password'];
  const out = [];
  let idx = 0;
  for (const el of els) {
    const tag = el.tagName.toLowerCase();
    const type = (tag === 'input' ? (el.getAttribute('type') || 'text') : tag).toLowerCase();
    if (SKIP.includes(type)) continue;
    const style = getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    if (style.display === 'none' || style.visibility === 'hidden' || rect.width === 0) continue;
    el.setAttribute('data-ja-idx', String(idx));
    let label = '';
    if (el.id) {
      const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (l) label = l.innerText.trim();
    }
    if (!label && el.closest('label')) label = el.closest('label').innerText.trim();
    if (!label) label = (el.getAttribute('aria-label') || '').trim();
    if (!label) {
      let p = el.parentElement;
      for (let i = 0; i < 3 && p; i++) {
        const clone = p.cloneNode(true);
        clone.querySelectorAll('input,select,textarea,button').forEach(n => n.remove());
        const t = (clone.innerText || '').trim().replace(/\s+/g, ' ');
        if (t && t.length < 80) { label = t; break; }
        p = p.parentElement;
      }
    }
    const entry = {
      idx, tag, type,
      name: el.name || '', id: el.id || '',
      label: label.slice(0, 120),
      placeholder: el.placeholder || '',
      required: !!el.required,
      value: el.value || ''
    };
    if (tag === 'select') {
      entry.options = [...el.options].map(o => ({value: o.value, text: o.text.trim()})).slice(0, 60);
    }
    out.push(entry);
    idx++;
  }
  return out;
})()
"""
