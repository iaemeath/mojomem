/* ============================================================
   QMem GUI 共享工具 —— 列表页 + 图谱页通用
   ============================================================ */

// HTML 转义防注入
function esc(s) {
  if (s == null) return '';
  return String(s).replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[c]));
}

// 防抖
function debounce(fn, ms) {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

// JSON fetch 封装
async function fetchJSON(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error('HTTP ' + r.status);
  return r.json();
}

/**
 * 渲染顶部导航栏（组件化，两页共用）
 * @param {string} current - 'list' 或 'graph'，标记当前激活页
 */
function renderNav(current) {
  const links = [
    { id: 'list',  href: '/',        icon: '☰', label: '记忆列表' },
    { id: 'graph', href: '/graph',   icon: '🕸', label: '引用图谱' },
  ];
  const html = links.map(l =>
    `<a href="${l.href}" class="${l.id === current ? 'active' : ''}">${l.icon} ${l.label}</a>`
  ).join('');
  return `<nav class="tab-nav">${html}</nav>`;
}

// type 标签 CSS 类名映射
function typeTagClass(type) {
  return 'tag-' + (type || 'manual');
}

// 格式化时间 (取前16位 "YYYY-MM-DD HH:MM")
function fmtTime(t) {
  return (t || '').toString().slice(0, 16);
}
