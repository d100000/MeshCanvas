(function () {
  function escapeHtml(value) {
    return String(value)
      .replaceAll('&', '&amp;')
      .replaceAll('<', '&lt;')
      .replaceAll('>', '&gt;')
      .replaceAll('"', '&quot;')
      .replaceAll("'", '&#039;');
  }

  function sanitizeHref(url) {
    const value = String(url || '').trim();
    if (/^(https?:|mailto:)/i.test(value) || value.startsWith('/') || value.startsWith('#')) {
      return value.replaceAll('"', '%22');
    }
    return '#';
  }

  function renderInline(text) {
    let value = text;
    value = value.replace(/`([^`]+)`/g, (_m, code) => `<code>${code}</code>`);
    value = value.replace(/\*\*([^*]+)\*\*/g, (_m, content) => `<strong>${content}</strong>`);
    value = value.replace(/\*([^*]+)\*/g, (_m, content) => `<em>${content}</em>`);
    value = value.replace(/\[([^\]]+)\]\(([^\s)]+)\)/g, (_m, label, url) => {
      const safeUrl = sanitizeHref(url);
      return `<a href="${safeUrl}" target="_blank" rel="noreferrer">${label}</a>`;
    });
    return value;
  }

  function isTableLine(line) {
    return /^\|(.+)\|$/.test(line.trim());
  }

  function parseTableRow(line) {
    return line
      .trim()
      .slice(1, -1)
      .split('|')
      .map((cell) => renderInline(cell.trim()));
  }

  function isTableSeparator(line) {
    return /^\|(?:\s*:?-{3,}:?\s*\|)+$/.test(line.trim());
  }

  function renderMarkdown(markdown) {
    const codeBlocks = [];
    const normalized = String(markdown || '').replace(/\r\n/g, '\n');

    let text = normalized.replace(/```([\w-]+)?\n([\s\S]*?)```/g, (_m, lang, code) => {
      const idx = codeBlocks.length;
      codeBlocks.push({ lang: lang || '', code });
      return `@@CODEBLOCK${idx}@@`;
    });

    text = escapeHtml(text);
    text = text.replace(/@@CODEBLOCK(\d+)@@/g, (_m, idxText) => {
      const block = codeBlocks[Number(idxText)];
      if (!block) return '';
      const langClass = block.lang ? ` class="language-${escapeHtml(block.lang)}"` : '';
      return `<pre><code${langClass}>${escapeHtml(block.code)}</code></pre>`;
    });

    const lines = text.split('\n');
    const out = [];
    let paragraph = [];
    let listType = null;
    let quoteBuffer = [];

    function flushParagraph() {
      if (!paragraph.length) return;
      out.push(`<p>${renderInline(paragraph.join('<br>'))}</p>`);
      paragraph = [];
    }

    function closeList() {
      if (!listType) return;
      out.push(listType === 'ol' ? '</ol>' : '</ul>');
      listType = null;
    }

    function flushQuote() {
      if (!quoteBuffer.length) return;
      out.push(`<blockquote>${quoteBuffer.map((line) => renderInline(line)).join('<br>')}</blockquote>`);
      quoteBuffer = [];
    }

    for (let index = 0; index < lines.length; index += 1) {
      const line = lines[index];
      const trimmed = line.trim();

      if (!trimmed) {
        flushParagraph();
        closeList();
        flushQuote();
        continue;
      }

      if (/^<pre><code[\s\S]*<\/code><\/pre>$/.test(trimmed)) {
        flushParagraph();
        closeList();
        flushQuote();
        out.push(line);
        continue;
      }

      if (isTableLine(trimmed) && index + 1 < lines.length && isTableSeparator(lines[index + 1])) {
        flushParagraph();
        closeList();
        flushQuote();
        const headerCells = parseTableRow(trimmed);
        const bodyRows = [];
        index += 2;
        while (index < lines.length && isTableLine(lines[index])) {
          bodyRows.push(parseTableRow(lines[index]));
          index += 1;
        }
        index -= 1;

        const headerHtml = headerCells.map((cell) => `<th>${cell}</th>`).join('');
        const bodyHtml = bodyRows
          .map((row) => `<tr>${row.map((cell) => `<td>${cell}</td>`).join('')}</tr>`)
          .join('');
        out.push(`<table><thead><tr>${headerHtml}</tr></thead><tbody>${bodyHtml}</tbody></table>`);
        continue;
      }

      const heading = trimmed.match(/^(#{1,6})\s+(.*)$/);
      if (heading) {
        flushParagraph();
        closeList();
        flushQuote();
        const level = heading[1].length;
        out.push(`<h${level}>${renderInline(heading[2])}</h${level}>`);
        continue;
      }

      const quoteMatch = trimmed.match(/^>\s?(.*)$/);
      if (quoteMatch) {
        flushParagraph();
        closeList();
        quoteBuffer.push(quoteMatch[1]);
        continue;
      }
      flushQuote();

      const unordered = trimmed.match(/^[-*]\s+(.*)$/);
      if (unordered) {
        flushParagraph();
        if (listType !== 'ul') {
          closeList();
          out.push('<ul>');
          listType = 'ul';
        }
        out.push(`<li>${renderInline(unordered[1])}</li>`);
        continue;
      }

      const ordered = trimmed.match(/^\d+\.\s+(.*)$/);
      if (ordered) {
        flushParagraph();
        if (listType !== 'ol') {
          closeList();
          out.push('<ol>');
          listType = 'ol';
        }
        out.push(`<li>${renderInline(ordered[1])}</li>`);
        continue;
      }

      closeList();
      paragraph.push(line);
    }

    flushParagraph();
    closeList();
    flushQuote();
    return out.join('\n');
  }

  window.renderMarkdown = renderMarkdown;
  window.escapeHtmlShared = escapeHtml;
})();
