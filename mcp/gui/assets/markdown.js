/* ============================================================
   轻量 Markdown 渲染器（零依赖，正则实现）
   支持: ##/### 标题、**加粗**、*斜体*、`行内代码`、
         - /1. 列表、```代码块```、> 引用、空行分段、GFM 表格
   ============================================================ */

function renderMarkdown(text) {
  if (!text) return '';
  // 1. 先转义 HTML 特殊字符，防止注入
  let s = String(text).replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[c]));

  const lines = s.split('\n');
  let html = '';
  let inCodeBlock = false;     // ``` 代码块
  let inList = false;          // - 列表
  let inOl = false;            // 1. 有序列表
  let inQuote = false;         // > 引用
  let inTable = false;         // | 表格 |
  let tableAlign = [];         // 表格各列对齐方式 ['left'|'center'|'right']
  let para = [];               // 普通段落缓冲

  function flushPara() {
    if (para.length) {
      html += '<p>' + para.join('<br>') + '</p>';
      para = [];
    }
  }
  function closeLists() {
    if (inList) { html += '</ul>'; inList = false; }
    if (inOl) { html += '</ol>'; inOl = false; }
  }
  function closeQuote() {
    if (inQuote) { html += '</blockquote>'; inQuote = false; }
  }
  function closeTable() {
    if (inTable) { html += '</tbody></table>'; inTable = false; tableAlign = []; }
  }

  // 行内格式：**加粗** *斜体* `代码`
  function inline(t) {
    return t
      .replace(/`([^`]+)`/g, '<code>$1</code>')
      .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
      .replace(/\*([^*]+)\*/g, '<em>$1</em>');
  }

  // 判断是否为表格分隔行（如 |---|:--|:--:|--:|---|）
  function isTableSeparator(line) {
    return /^\|?\s*:?-{2,}[:]?\s*(\|\s*:?-{2,}[:]?\s*)+\|?\s*$/.test(line);
  }
  // 解析分隔行，返回各列对齐方式
  function parseAlignment(line) {
    const cells = line.replace(/^\||\|$/g, '').split('|');
    return cells.map(c => {
      const t = c.trim();
      const left = t.startsWith(':'), right = t.endsWith(':');
      if (left && right) return 'center';
      if (right) return 'right';
      return 'left';
    });
  }
  // 解析表格数据行，返回单元格内容数组
  function parseRow(line) {
    return line.replace(/^\||\|$/g, '').split('|').map(c => c.trim());
  }

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    const trimmed = line.trim();

    // 代码块 ``` 切换
    if (trimmed.startsWith('```')) {
      flushPara(); closeLists(); closeQuote(); closeTable();
      if (inCodeBlock) {
        html += '</code></pre>';
        inCodeBlock = false;
      } else {
        html += '<pre><code>';
        inCodeBlock = true;
      }
      continue;
    }
    if (inCodeBlock) {
      html += line + '\n';
      continue;
    }

    // 标题 ### / ## / #
    const hMatch = trimmed.match(/^(#{1,3})\s+(.*)/);
    if (hMatch) {
      flushPara(); closeLists(); closeQuote(); closeTable();
      const level = hMatch[1].length;
      html += `<h${level}>${inline(hMatch[2])}</h${level}>`;
      continue;
    }

    // 引用 >
    if (trimmed.startsWith('>')) {
      flushPara(); closeLists(); closeTable();
      if (!inQuote) { html += '<blockquote>'; inQuote = true; }
      html += '<p>' + inline(trimmed.slice(1).trim()) + '</p>';
      continue;
    } else if (inQuote) {
      closeQuote();
    }

    // 无序列表 - / *
    const ulMatch = trimmed.match(/^[-*]\s+(.*)/);
    if (ulMatch) {
      flushPara(); closeTable();
      if (inOl) { html += '</ol>'; inOl = false; }
      if (!inList) { html += '<ul>'; inList = true; }
      html += '<li>' + inline(ulMatch[1]) + '</li>';
      continue;
    }

    // 有序列表 1. 2.
    const olMatch = trimmed.match(/^\d+\.\s+(.*)/);
    if (olMatch) {
      flushPara(); closeTable();
      if (inList) { html += '</ul>'; inList = false; }
      if (!inOl) { html += '<ol>'; inOl = true; }
      html += '<li>' + inline(olMatch[1]) + '</li>';
      continue;
    }

    // 表格：当前行是分隔行，且上一行是表头
    if (isTableSeparator(trimmed)) {
      const prevLine = (lines[i - 1] || '').trim();
      // 确保前一行像表格行（含 |）
      if (/\|/.test(prevLine) && !inCodeBlock) {
        closeLists(); closeQuote();
        // 表头行（上一行）在上一轮作为普通文本进了 para，弹出它
        if (para.length) para.pop();
        flushPara();
        tableAlign = parseAlignment(trimmed);
        const headerCells = parseRow(prevLine);
        let thead = '<table><thead><tr>';
        headerCells.forEach((c, idx) => {
          const al = tableAlign[idx] || 'left';
          thead += `<th style="text-align:${al}">${inline(c)}</th>`;
        });
        thead += '</tr></thead><tbody>';
        html += thead;
        inTable = true;
        continue;
      }
    }
    // 表格数据行（已在表格中）
    if (inTable) {
      if (trimmed === '') { closeTable(); continue; }
      if (/\|/.test(trimmed)) {
        const cells = parseRow(trimmed);
        let tr = '<tr>';
        cells.forEach((c, idx) => {
          const al = tableAlign[idx] || 'left';
          tr += `<td style="text-align:${al}">${inline(c)}</td>`;
        });
        tr += '</tr>';
        html += tr;
        continue;
      } else {
        // 非表格行，结束表格
        closeTable();
        // 继续往下走处理该行
      }
    }

    // 空行 → 段落分隔
    if (trimmed === '') {
      flushPara(); closeLists(); closeQuote(); closeTable();
      continue;
    }

    // 普通文本行 → 累积到段落
    closeLists(); closeQuote();
    para.push(inline(trimmed));
  }

  // 收尾
  if (inCodeBlock) html += '</code></pre>';
  flushPara(); closeLists(); closeQuote(); closeTable();
  return html;
}
