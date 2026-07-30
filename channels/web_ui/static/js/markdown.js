(() => {
  'use strict';

  const escapeHtml = value => String(value ?? '').replace(/[&<>'"]/g, character => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;'
  })[character]);

  const safeHref = value => {
    const href = String(value || '').trim();
    return /^(https?:\/\/|mailto:|\/|#)/i.test(href) ? escapeHtml(href) : '#';
  };

  const renderInline = source => {
    const tokens = [];
    const token = html => {
      const index = tokens.push(html) - 1;
      return `\u0000INLINE${index}\u0000`;
    };
    let value = String(source ?? '').replace(/`([^`\n]+)`/g, (_match, code) =>
      token(`<code>${escapeHtml(code)}</code>`));
    value = value.replace(/\[([^\]\n]+)\]\(([^)\s]+)\)/g, (_match, label, href) =>
      token(`<a href="${safeHref(href)}" target="_blank" rel="noopener noreferrer">${escapeHtml(label)}</a>`));
    value = escapeHtml(value)
      .replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>')
      .replace(/__([^_\n]+)__/g, '<strong>$1</strong>')
      .replace(/~~([^~\n]+)~~/g, '<del>$1</del>')
      .replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<em>$2</em>')
      .replace(/(^|[^_])_([^_\n]+)_/g, '$1<em>$2</em>');
    return value.replace(/\u0000INLINE(\d+)\u0000/g, (_match, index) => tokens[Number(index)] || '');
  };

  const isTableDivider = line => /^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$/.test(line);
  const tableCells = line => line.trim().replace(/^\||\|$/g, '').split('|').map(cell => cell.trim());
  const startsBlock = (lines, index) => {
    const line = lines[index] || '';
    return !line.trim() || /^#{1,6}\s+/.test(line) || /^>\s?/.test(line)
      || /^\s*([-*+] |\d+\. )/.test(line) || /^---+$/.test(line.trim())
      || (index + 1 < lines.length && line.includes('|') && isTableDivider(lines[index + 1]));
  };

  const render = markdown => {
    const codeBlocks = [];
    const codeToken = html => {
      const index = codeBlocks.push(html) - 1;
      return `\u0000CODE${index}\u0000`;
    };
    const prepared = String(markdown ?? '').replace(/\r\n?/g, '\n').replace(
      /^```([^\n`]*)\n([\s\S]*?)^```\s*$/gm,
      (_match, language, code) => codeToken(`<pre><code${language.trim() ? ` class="language-${escapeHtml(language.trim().replace(/[^\w-]/g, ''))}"` : ''}>${escapeHtml(code.replace(/\n$/, ''))}</code></pre>`)
    );
    const lines = prepared.split('\n');
    const html = [];
    for (let index = 0; index < lines.length;) {
      const line = lines[index];
      if (!line.trim()) { index += 1; continue; }
      const codeMatch = line.match(/^\u0000CODE(\d+)\u0000$/);
      if (codeMatch) { html.push(codeBlocks[Number(codeMatch[1])]); index += 1; continue; }
      const heading = line.match(/^(#{1,6})\s+(.+)$/);
      if (heading) { const level = heading[1].length; html.push(`<h${level}>${renderInline(heading[2])}</h${level}>`); index += 1; continue; }
      if (/^---+$/.test(line.trim())) { html.push('<hr>'); index += 1; continue; }
      if (index + 1 < lines.length && line.includes('|') && isTableDivider(lines[index + 1])) {
        const headers = tableCells(line);
        index += 2;
        const rows = [];
        while (index < lines.length && lines[index].includes('|') && lines[index].trim()) {
          rows.push(tableCells(lines[index])); index += 1;
        }
        html.push(`<div class="markdown-table-wrap"><table><thead><tr>${headers.map(cell => `<th>${renderInline(cell)}</th>`).join('')}</tr></thead><tbody>${rows.map(row => `<tr>${headers.map((_header, cellIndex) => `<td>${renderInline(row[cellIndex] || '')}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`);
        continue;
      }
      if (/^>\s?/.test(line)) {
        const quote = [];
        while (index < lines.length && /^>\s?/.test(lines[index])) quote.push(lines[index++].replace(/^>\s?/, ''));
        html.push(`<blockquote>${quote.map(renderInline).join('<br>')}</blockquote>`); continue;
      }
      const list = line.match(/^\s*([-*+] |\d+\. )(.+)$/);
      if (list) {
        const ordered = /\d+\. /.test(list[1]);
        const items = [];
        const pattern = ordered ? /^\s*\d+\.\s+(.+)$/ : /^\s*[-*+]\s+(.+)$/;
        while (index < lines.length) {
          const item = lines[index].match(pattern);
          if (!item) break;
          items.push(`<li>${renderInline(item[1])}</li>`); index += 1;
        }
        html.push(`<${ordered ? 'ol' : 'ul'}>${items.join('')}</${ordered ? 'ol' : 'ul'}>`); continue;
      }
      const paragraph = [line];
      index += 1;
      while (index < lines.length && !startsBlock(lines, index) && !/^\u0000CODE\d+\u0000$/.test(lines[index])) paragraph.push(lines[index++]);
      html.push(`<p>${paragraph.map(renderInline).join('<br>')}</p>`);
    }
    return html.join('');
  };

  window.NanoClawMarkdown = Object.freeze({ render, escapeHtml });
})();
