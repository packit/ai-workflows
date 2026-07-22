'use strict';

// ============================================================
// Utilities
// ============================================================

function getVal(value) {
  if (!value || typeof value !== 'object') return null;
  for (const k of ['stringValue', 'intValue', 'boolValue', 'doubleValue']) {
    if (k in value) return value[k];
  }
  if ('arrayValue' in value && value.arrayValue && value.arrayValue.values) {
    return value.arrayValue.values.map(getVal);
  }
  return null;
}

function escHtml(s) {
  if (typeof s !== 'string') s = String(s ?? '');
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function lazyDetails(summaryText, buildFn, open) {
  const d = document.createElement('details');
  const s = document.createElement('summary');
  s.textContent = summaryText;
  d.appendChild(s);
  let populated = open;
  d.addEventListener('toggle', () => {
    if (d.open) {
      if (!populated) d.appendChild(buildFn());
      populated = true;
    } else {
      while (d.lastChild !== s) d.removeChild(d.lastChild);
      populated = false;
    }
  });
  if (open) {
    d.appendChild(buildFn());
    d.open = true;
  }
  return d;
}

function el(tag, attrs) {
  const e = document.createElement(tag);
  if (attrs) {
    for (const [k, v] of Object.entries(attrs)) {
      if (k === 'className') e.className = v;
      else if (k === 'textContent') e.textContent = v;
      else if (k === 'innerHTML') e.innerHTML = v;
      else if (k.startsWith('on')) e.addEventListener(k.slice(2).toLowerCase(), v);
      else e.setAttribute(k, v);
    }
  }
  for (let i = 2; i < arguments.length; i++) {
    const child = arguments[i];
    if (child == null) continue;
    if (typeof child === 'string') e.appendChild(document.createTextNode(child));
    else if (child instanceof Node) e.appendChild(child);
  }
  return e;
}

function fmtTime(nanos) {
  if (!nanos) return '-';
  const d = new Date(nanos / 1e6);
  const pad = n => String(n).padStart(2, '0');
  return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate()) + ' '
    + pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds());
}

function fmtTimeShort(nanos) {
  if (!nanos) return '-';
  const d = new Date(nanos / 1e6);
  const pad = n => String(n).padStart(2, '0');
  return pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds());
}

function fmtDuration(startNanos, endNanos) {
  if (!endNanos || !startNanos) return '-';
  const ms = (endNanos - startNanos) / 1e6;
  if (ms < 1000) return ms.toFixed(0) + 'ms';
  if (ms < 60000) return (ms / 1000).toFixed(1) + 's';
  return (ms / 60000).toFixed(1) + 'm';
}

function fmtTokens(n) {
  if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M';
  if (n >= 1e3) return (n / 1e3).toFixed(1) + 'k';
  return String(n);
}

function aggregateLlmStats(spans) {
  let calls = 0, promptTokens = 0, completionTokens = 0, cost = 0, cacheRead = 0, cacheWrite = 0;
  const models = new Set();
  for (const s of spans) {
    const attrs = s.attributes || {};
    const kind = getVal(attrs['openinference.span.kind']);
    if (kind !== 'LLM' && !s.name.endsWith('ChatModel')) continue;
    calls++;
    const pt = getVal(attrs['llm.token_count.prompt']);
    const ct = getVal(attrs['llm.token_count.completion']);
    const tc = getVal(attrs['llm.cost.total']);
    const cr = getVal(attrs['metadata.usage.cached_prompt_tokens']);
    const cw = getVal(attrs['metadata.usage.cached_creation_tokens']);
    const model = getVal(attrs['llm.model_name']);
    if (pt) promptTokens += Number(pt);
    if (ct) completionTokens += Number(ct);
    if (tc) cost += Number(tc);
    if (cr) cacheRead += Number(cr);
    if (cw) cacheWrite += Number(cw);
    if (model) models.add(model);
  }
  return {calls, promptTokens, completionTokens, cost, cacheRead, cacheWrite, models};
}

function isTraceComplete(spans) {
  return spans.some(s => (!s.parent_span_id || s.parent_span_id === '') && s.end_time);
}

function fmtAgo(nanos) {
  if (!nanos) return '';
  const ms = Date.now() - nanos / 1e6;
  if (ms < 60000) return 'just now';
  if (ms < 3600000) return Math.floor(ms / 60000) + 'm ago';
  if (ms < 86400000) return Math.floor(ms / 3600000) + 'h ago';
  return Math.floor(ms / 86400000) + 'd ago';
}

function fmtToolCall(name, inputValue) {
  try {
    const parsed = JSON.parse(inputValue);
    const args = parsed.input || parsed;
    const parts = [];
    for (const [k, v] of Object.entries(args)) {
      let vs;
      if (v === null) vs = 'null';
      else if (typeof v === 'string') {
        vs = '"' + (v.length > 60 ? v.slice(0, 57) + '…' : v) + '"';
      } else vs = JSON.stringify(v);
      parts.push(k + '=' + vs);
    }
    const argsStr = parts.join(', ');
    const full = name + '(' + argsStr + ')';
    if (full.length > 120) return {name, args: argsStr.slice(0, 120 - name.length - 2) + '…'};
    return {name, args: argsStr};
  } catch (e) {
    return {name, args: null};
  }
}

function statusClass(code) {
  if (code === 1) return 'ok';
  if (code === 2) return 'error';
  return '';
}

function statusLabel(code) {
  if (code === 1) return 'Ok';
  if (code === 2) return 'Error';
  return 'Unset';
}

// ============================================================
// API Client
// ============================================================

const api = {
  async recentTraces(opts) {
    const p = new URLSearchParams();
    if (opts.since) p.set('since', opts.since);
    if (opts.workflow) p.set('workflow', opts.workflow);
    if (opts.limit) p.set('limit', opts.limit);
    const qs = p.toString();
    const res = await fetch('/traces/recent' + (qs ? '?' + qs : ''));
    if (!res.ok) throw new Error(await res.text());
    return res.json();
  },

  async issues() {
    const res = await fetch('/traces/');
    if (!res.ok) throw new Error(await res.text());
    return res.json();
  },

  async spans(issue, opts) {
    const p = new URLSearchParams();
    if (opts.traceId) p.set('trace_id', opts.traceId);
    if (opts.agentType) p.set('agent_type', opts.agentType);
    if (opts.name) p.set('name', opts.name);
    if (opts.last) p.set('last', opts.last);
    if (opts.since) p.set('since', opts.since);
    const qs = p.toString();
    const res = await fetch('/traces/' + encodeURIComponent(issue) + (qs ? '?' + qs : ''));
    if (!res.ok) throw new Error(await res.text());
    return res.json();
  },
};

// ============================================================
// State
// ============================================================

const state = {
  view: 'recent',
  recentTraces: [],
  currentTraceId: null,
  currentIssue: null,
  spans: [],
  spanIds: new Set(),
  pollTimeoutId: null,
  pollGen: 0,
  filterSince: 10800,
  filterWorkflow: '',
  issueFilterSince: 86400,
  previousHash: '#/',
};

// ============================================================
// Polling
// ============================================================

function stopPolling() {
  state.pollGen++;
  if (state.pollTimeoutId) {
    clearTimeout(state.pollTimeoutId);
    state.pollTimeoutId = null;
  }
}

function startPolling(fn, intervalMs) {
  stopPolling();
  const gen = state.pollGen;
  async function tick() {
    if (state.pollGen !== gen) return;
    try { await fn(); } catch (e) {}
    if (state.pollGen !== gen) return;
    state.pollTimeoutId = setTimeout(tick, intervalMs);
  }
  state.pollTimeoutId = setTimeout(tick, intervalMs);
}

document.addEventListener('visibilitychange', () => {
  if (document.hidden) {
    stopPolling();
  } else if (state.view === 'trace' && state.currentIssue && state.currentTraceId) {
    if (!isTraceComplete(state.spans)) {
      startPolling(() => pollNewSpans(state.currentIssue, state.currentTraceId), 5000);
    }
  } else if (state.view === 'recent' || state.view === 'issues' || state.view === 'issue') {
    route();
  }
});

// ============================================================
// Router
// ============================================================

function route() {
  stopPolling();
  window.removeEventListener('scroll', onScroll);
  if (sidebarScrollHandler) { window.removeEventListener('scroll', sidebarScrollHandler); sidebarScrollHandler = null; }
  const hash = location.hash || '#/';
  const app = document.getElementById('app');
  app.innerHTML = '';

  document.querySelector('.jump-bottom')?.remove();

  const prevHash = state.previousHash;
  state.previousHash = hash;

  if (hash.startsWith('#/trace/')) {
    const parts = hash.slice(8).split('/');
    const traceId = parts.pop();
    const issue = decodeURIComponent(parts.join('/'));
    state.view = 'trace';
    state.currentIssue = issue;
    state.currentTraceId = traceId;
    renderTraceDetail(app, issue, traceId, prevHash);
  } else if (hash.startsWith('#/issues/')) {
    const issue = decodeURIComponent(hash.slice(9));
    state.view = 'issue';
    state.currentIssue = issue;
    renderIssueDetail(app, issue);
  } else if (hash === '#/issues') {
    state.view = 'issues';
    renderIssues(app);
  } else {
    state.view = 'recent';
    renderRecent(app);
  }

  updateNav();
}

function updateNav() {
  document.querySelectorAll('.header-nav a').forEach(a => {
    const href = a.getAttribute('href');
    if (state.view === 'recent' && href === '#/') a.classList.add('active');
    else if ((state.view === 'issues' || state.view === 'issue') && href === '#/issues') a.classList.add('active');
    else a.classList.remove('active');
  });
}

window.addEventListener('hashchange', route);

// ============================================================
// Header
// ============================================================

function renderHeader() {
  const header = document.getElementById('header');
  header.innerHTML = '';
  const logo = el('a', {className: 'header-logo', href: '#/'});
  logo.appendChild(el('img', {className: 'header-avatar', src: '/static/ymir-avatar.png'}));
  logo.appendChild(document.createTextNode('Ymir traces'));
  header.appendChild(logo);
  header.appendChild(el('nav', {className: 'header-nav'},
    el('a', {href: '#/'}, 'recent'),
    el('a', {href: '#/issues'}, 'issues')));
  header.appendChild(el('div', {className: 'header-spacer'}));
  header.appendChild(el('span', {className: 'header-status', id: 'header-status'}));
  header.appendChild(el('button', {
    className: 'header-btn',
    onClick: toggleTheme,
    id: 'theme-btn',
  }, themeLabel()));
}

function themeLabel() {
  return document.documentElement.classList.contains('dark') ? '[light]' : '[dark]';
}

function toggleTheme() {
  document.documentElement.classList.toggle('dark');
  const isDark = document.documentElement.classList.contains('dark');
  try { localStorage.setItem('theme', isDark ? 'dark' : 'light'); } catch(e) {}
  const btn = document.getElementById('theme-btn');
  if (btn) btn.textContent = themeLabel();
}

function setStatus(text) {
  const el = document.getElementById('header-status');
  if (el) el.textContent = text;
}

// ============================================================
// Recent Traces View
// ============================================================

async function renderRecent(container) {
  container.appendChild(el('div', {className: 'loading'}, 'loading traces...'));

  const filtersBar = el('div', {className: 'filters'});

  const sinceSelect = el('select', {className: 'filter-select', onChange: (e) => {
    state.filterSince = parseInt(e.target.value);
    refreshRecent(container);
  }});
  for (const [label, val] of [['1h', 3600], ['3h', 10800], ['12h', 43200], ['24h', 86400], ['3d', 259200], ['7d', 604800], ['30d', 2592000]]) {
    const opt = el('option', {value: val}, label);
    if (val === state.filterSince) opt.selected = true;
    sinceSelect.appendChild(opt);
  }

  const workflowInput = el('input', {
    className: 'filter-input',
    type: 'text',
    placeholder: 'workflow filter',
    value: state.filterWorkflow,
    onInput: (e) => {
      state.filterWorkflow = e.target.value;
    },
    onKeydown: (e) => {
      if (e.key === 'Enter') refreshRecent(container);
    },
  });

  filtersBar.appendChild(el('span', {className: 'filter-label'}, 'since:'));
  filtersBar.appendChild(sinceSelect);
  filtersBar.appendChild(el('span', {className: 'filter-label'}, 'workflow:'));
  filtersBar.appendChild(workflowInput);

  try {
    const data = await api.recentTraces({
      since: state.filterSince,
      workflow: state.filterWorkflow || undefined,
    });
    state.recentTraces = data.traces || [];
    container.innerHTML = '';
    container.appendChild(filtersBar);
    renderTraceCards(container, state.recentTraces);
    setStatus(state.recentTraces.length + ' traces');
  } catch (e) {
    container.innerHTML = '';
    container.appendChild(filtersBar);
    container.appendChild(el('div', {className: 'error-banner'}, 'Failed to load traces: ' + e.message));
  }

  startPolling(() => refreshRecent(container), 30000);
}

async function refreshRecent(container) {
  try {
    const data = await api.recentTraces({
      since: state.filterSince,
      workflow: state.filterWorkflow || undefined,
    });
    state.recentTraces = data.traces || [];
    const grid = container.querySelector('.trace-grid');
    const filters = container.querySelector('.filters');
    container.innerHTML = '';
    if (filters) container.appendChild(filters);
    renderTraceCards(container, state.recentTraces);
    setStatus(state.recentTraces.length + ' traces');
  } catch (e) {
    // silently skip failed polls
  }
}

function renderTraceCards(container, traces) {
  if (traces.length === 0) {
    container.appendChild(el('div', {className: 'empty-state'}, 'No traces found.'));
    return;
  }

  const grid = el('div', {className: 'trace-grid'});
  for (const t of traces) {
    const sc = statusClass(t.status_code);
    const card = el('div', {
      className: 'trace-card' + (sc ? ' status-' + sc : ''),
      onClick: () => {
        const issue = (t.issues && t.issues[0]) || '_';
        location.hash = '#/trace/' + encodeURIComponent(issue) + '/' + t.trace_id;
      },
    });

    card.appendChild(el('div', {className: 'trace-card-header'},
      el('span', {className: 'status-dot ' + sc}),
      el('span', {className: 'trace-card-workflow'}, t.workflow || 'unknown'),
      el('span', {className: 'trace-card-time'}, fmtAgo(t.start_time)),
    ));

    if (t.issues && t.issues.length > 0) {
      const badges = el('div', {className: 'trace-card-issues'});
      for (const issue of t.issues) {
        badges.appendChild(el('a', {
          className: 'issue-badge',
          href: '#/issues/' + encodeURIComponent(issue),
          onClick: (e) => e.stopPropagation(),
        }, issue));
      }
      card.appendChild(badges);
    }

    const meta = el('div', {className: 'trace-card-meta'});
    meta.appendChild(el('span', {}, fmtDuration(t.start_time, t.end_time)));
    meta.appendChild(el('span', {}, t.num_spans + ' spans'));
    if (t.error_count > 0) {
      meta.appendChild(el('span', {className: 'error-count'}, t.error_count + ' errors'));
    }
    card.appendChild(meta);

    grid.appendChild(card);
  }
  container.appendChild(grid);
}

// ============================================================
// Trace Detail View
// ============================================================

async function renderTraceDetail(container, issue, traceId, prevHash) {
  container.appendChild(el('div', {className: 'loading'}, 'loading spans...'));

  try {
    const data = await api.spans(issue, {traceId: traceId});
    state.spans = data.spans || [];
    state.spanIds = new Set(state.spans.map(s => s.span_id));

    container.innerHTML = '';

    const tree = buildSpanTree(state.spans);
    const agents = collectAgents(tree, 0);

    const hasSidebar = agents.length > 0;
    const layout = el('div', {className: 'trace-layout'});
    if (!hasSidebar) layout.style.gridTemplateColumns = '1fr';
    if (hasSidebar) layout.appendChild(renderSidebar(agents));
    const main = el('div', {className: 'trace-main'});

    let backHref = '#/';
    let backLabel = '← recent traces';
    if (prevHash && prevHash.startsWith('#/issues/')) {
      backHref = prevHash;
      backLabel = '← ' + decodeURIComponent(prevHash.slice(9));
    }
    main.appendChild(el('a', {className: 'back-link', href: backHref}, backLabel));
    const header = el('div', {className: 'trace-detail-header'});
    header.appendChild(el('h1', {}, issue + ' / ' + traceId));
    const meta = el('div', {className: 'trace-detail-meta'});
    if (state.spans.length > 0) {
      const first = state.spans[0];
      meta.appendChild(el('span', {}, 'started: ' + fmtTime(first.start_time)));
      meta.appendChild(el('span', {}, 'spans: ' + state.spans.length));
      const errors = state.spans.filter(s => s.status_code === 2).length;
      if (errors > 0) meta.appendChild(el('span', {className: 'error-count'}, 'errors: ' + errors));
      const llmStats = aggregateLlmStats(state.spans);
      if (llmStats.calls > 0) {
        meta.appendChild(el('span', {}, 'LLM calls: ' + llmStats.calls));
        let tokenSummary = 'tokens: ' + fmtTokens(llmStats.promptTokens) + ' in';
        if (llmStats.cacheRead || llmStats.cacheWrite) {
          const parts = [];
          if (llmStats.cacheRead) parts.push(fmtTokens(llmStats.cacheRead) + ' cached');
          if (llmStats.cacheWrite) parts.push(fmtTokens(llmStats.cacheWrite) + ' new');
          tokenSummary += ' (' + parts.join(', ') + ')';
        }
        tokenSummary += ' / ' + fmtTokens(llmStats.completionTokens) + ' out';
        meta.appendChild(el('span', {}, tokenSummary));
        if (llmStats.cost > 0) meta.appendChild(el('span', {}, 'cost: $' + llmStats.cost.toFixed(2)));
        if (llmStats.models.size > 0) meta.appendChild(el('span', {}, 'model: ' + [...llmStats.models].join(', ')));
      }
    }
    header.appendChild(meta);
    main.appendChild(header);

    const spanList = el('div', {className: 'span-list', id: 'span-list'});
    renderSpanTree(spanList, tree, 0);
    main.appendChild(spanList);
    layout.appendChild(main);
    container.appendChild(layout);

    setupAutoScroll();
    if (isTraceComplete(state.spans)) {
      setStatus('completed · ' + state.spans.length + ' spans');
    } else {
      startPolling(() => pollNewSpans(issue, traceId), 5000);
      setStatus('live');
    }
  } catch (e) {
    container.innerHTML = '';
    container.appendChild(el('div', {className: 'error-banner'}, 'Failed to load spans: ' + e.message));
  }
}

async function pollNewSpans(issue, traceId) {
  try {
    const data = await api.spans(issue, {traceId: traceId});
    const allSpans = data.spans || [];
    let changed = false;

    const existingById = new Map();
    for (const s of state.spans) existingById.set(s.span_id, s);

    for (const s of allSpans) {
      const existing = existingById.get(s.span_id);
      if (!existing) {
        state.spans.push(s);
        state.spanIds.add(s.span_id);
        changed = true;
      } else if (existing.end_time !== s.end_time || existing.status_code !== s.status_code) {
        Object.assign(existing, s);
        changed = true;
      }
    }
    if (!changed) return;
    state.spans.sort((a, b) => (a.start_time || 0) - (b.start_time || 0));

    // Re-render full tree so placeholders and hierarchy stay correct
    const spanList = document.getElementById('span-list');
    if (!spanList) return;
    const scrollY = window.scrollY;
    spanList.innerHTML = '';
    const tree = buildSpanTree(state.spans);
    renderSpanTree(spanList, tree, 0);
    window.scrollTo({top: scrollY});

    const agents = collectAgents(tree, 0);
    const oldSidebar = document.getElementById('trace-sidebar');
    if (agents.length > 0 && oldSidebar) {
      oldSidebar.replaceWith(renderSidebar(agents));
    } else if (agents.length > 0 && !oldSidebar) {
      const layout = spanList.closest('.trace-layout');
      if (layout) layout.insertBefore(renderSidebar(agents), layout.firstChild);
    }

    const meta = document.querySelector('.trace-detail-meta');
    if (meta) {
      meta.innerHTML = '';
      const first = state.spans[0];
      if (first) meta.appendChild(el('span', {}, 'started: ' + fmtTime(first.start_time)));
      meta.appendChild(el('span', {}, 'spans: ' + state.spans.length));
      const errors = state.spans.filter(s => s.status_code === 2).length;
      if (errors > 0) meta.appendChild(el('span', {className: 'error-count'}, 'errors: ' + errors));
      const llmStats = aggregateLlmStats(state.spans);
      if (llmStats.calls > 0) {
        meta.appendChild(el('span', {}, 'LLM calls: ' + llmStats.calls));
        let tokenSummary = 'tokens: ' + fmtTokens(llmStats.promptTokens) + ' in';
        if (llmStats.cacheRead || llmStats.cacheWrite) {
          const parts = [];
          if (llmStats.cacheRead) parts.push(fmtTokens(llmStats.cacheRead) + ' cached');
          if (llmStats.cacheWrite) parts.push(fmtTokens(llmStats.cacheWrite) + ' new');
          tokenSummary += ' (' + parts.join(', ') + ')';
        }
        tokenSummary += ' / ' + fmtTokens(llmStats.completionTokens) + ' out';
        meta.appendChild(el('span', {}, tokenSummary));
        if (llmStats.cost > 0) meta.appendChild(el('span', {}, 'cost: $' + llmStats.cost.toFixed(2)));
        if (llmStats.models.size > 0) meta.appendChild(el('span', {}, 'model: ' + [...llmStats.models].join(', ')));
      }
    }

    maybeAutoScroll();
    if (isTraceComplete(state.spans)) {
      stopPolling();
      setStatus('completed · ' + state.spans.length + ' spans');
    } else {
      setStatus('live · ' + state.spans.length + ' spans');
    }
  } catch (e) {
    // silently skip failed polls
  }
}

// ============================================================
// Span Tree
// ============================================================

function buildSpanTree(spans) {
  const byId = new Map();
  const roots = [];
  for (const span of spans) {
    byId.set(span.span_id, {...span, children: []});
  }
  // Synthesize placeholders for missing parents
  const missingParents = new Map();
  for (const span of spans) {
    const pid = span.parent_span_id;
    if (pid && !byId.has(pid)) {
      if (!missingParents.has(pid)) missingParents.set(pid, []);
      missingParents.get(pid).push(span);
    }
  }
  const placeholders = [];
  for (const [pid, children] of missingParents) {
    const earliest = Math.min(...children.map(c => c.start_time));
    const name = children.map(c => getVal((c.attributes || {})['agent.name'])).find(Boolean) || null;
    const placeholder = {
      trace_id: children[0].trace_id, span_id: pid, parent_span_id: '',
      name, start_time: earliest, end_time: null, status_code: 0,
      jira_issue: null, agent_type: null, attributes: {}, _placeholder: true,
    };
    placeholders.push(placeholder);
    byId.set(pid, {...placeholder, children: []});
  }
  // Nest placeholders in the same trace: earliest is the root
  const byTrace = new Map();
  for (const p of placeholders) {
    if (!byTrace.has(p.trace_id)) byTrace.set(p.trace_id, []);
    byTrace.get(p.trace_id).push(p);
  }
  for (const group of byTrace.values()) {
    if (group.length > 1) {
      group.sort((a, b) => a.start_time - b.start_time);
      const rootPid = group[0].span_id;
      for (let i = 1; i < group.length; i++) {
        group[i].parent_span_id = rootPid;
        byId.get(group[i].span_id).parent_span_id = rootPid;
      }
    }
  }
  // Name placeholders: root always from workflow.name, non-root from agent.name only
  for (const p of placeholders) {
    const node = byId.get(p.span_id);
    const isRoot = !p.parent_span_id;
    if (isRoot) {
      const wfAttr = (missingParents.get(p.span_id) || [])
        .map(c => getVal((c.attributes || {})['workflow.name'])).find(Boolean);
      if (wfAttr) {
        node.name = wfAttr[0].toUpperCase() + wfAttr.slice(1) + 'Workflow';
      } else if (!node.name) {
        node.name = '(in progress)';
      }
    } else if (!node.name) {
      node.name = '(agent)';
    }
  }
  for (const p of placeholders) {
    const node = byId.get(p.span_id);
    const parent = byId.get(node.parent_span_id);
    if (parent) {
      parent.children.push(node);
    } else {
      roots.push(node);
    }
  }
  // Build tree for real spans
  for (const span of spans) {
    const node = byId.get(span.span_id);
    const parent = byId.get(span.parent_span_id);
    if (parent) {
      parent.children.push(node);
    } else {
      roots.push(node);
    }
  }
  // Sort children by start_time
  for (const node of byId.values()) {
    node.children.sort((a, b) => a.start_time - b.start_time);
  }
  roots.sort((a, b) => a.start_time - b.start_time);
  return roots;
}

function treeLlmStats(node) {
  let promptTokens = 0, completionTokens = 0, cost = 0, cacheRead = 0, cacheWrite = 0;
  const attrs = node.attributes || {};
  const kind = getVal(attrs['openinference.span.kind']);
  if (kind === 'LLM' || node.name.endsWith('ChatModel')) {
    const pt = getVal(attrs['llm.token_count.prompt']);
    const ct = getVal(attrs['llm.token_count.completion']);
    const tc = getVal(attrs['llm.cost.total']);
    const cr = getVal(attrs['metadata.usage.cached_prompt_tokens']);
    const cw = getVal(attrs['metadata.usage.cached_creation_tokens']);
    if (pt) promptTokens += Number(pt);
    if (ct) completionTokens += Number(ct);
    if (tc) cost += Number(tc);
    if (cr) cacheRead += Number(cr);
    if (cw) cacheWrite += Number(cw);
  }
  if (node.children) {
    for (const child of node.children) {
      const s = treeLlmStats(child);
      promptTokens += s.promptTokens;
      completionTokens += s.completionTokens;
      cost += s.cost;
      cacheRead += s.cacheRead;
      cacheWrite += s.cacheWrite;
    }
  }
  return {promptTokens, completionTokens, cost, cacheRead, cacheWrite};
}

function isEmptyLlm(node) {
  const attrs = node.attributes || {};
  const kind = getVal(attrs['openinference.span.kind']);
  if (kind !== 'LLM' && !node.name.endsWith('ChatModel')) return false;
  if (getVal(attrs['llm.output_messages.0.message.contents.0.message_content.type'])) return false;
  if (getVal(attrs['llm.output_messages.0.message.tool_calls.0.tool_call.function.name'])) return false;
  return true;
}

function renderSpanTree(container, nodes, depth, parent) {
  const parentKind = parent ? getVal((parent.attributes || {})['openinference.span.kind']) : null;
  for (const node of nodes) {
    if (parentKind === 'TOOL' && node.name === 'error') continue;
    if (isEmptyLlm(node)) continue;
    container.appendChild(renderSpanRow(node, depth, parent));
    if (node.children && node.children.length > 0) {
      renderSpanTree(container, node.children, depth + 1, node);
    }
  }
}

function renderSpanRow(span, depth, parent) {
  const attrs = span.attributes || {};
  let kind = getVal(attrs['openinference.span.kind']) || '';
  if (!kind && span.name.endsWith('ChatModel')) kind = 'LLM';
  let effectiveStatus = span.status_code;
  if (kind === 'TOOL' && span.name === 'run_shell_command') {
    try {
      const out = JSON.parse(getVal(attrs['output.value']) || '{}');
      if (out.exit_code !== 0) effectiveStatus = 2;
    } catch (e) {}
  }
  const sc = statusClass(effectiveStatus);

  let kindClass = '';
  if (kind === 'LLM') kindClass = 'kind-llm';
  else if (kind === 'TOOL') kindClass = 'kind-tool';
  else if (kind === 'AGENT' || kind === 'CHAIN') kindClass = 'kind-agent';
  if (effectiveStatus === 2) kindClass = 'has-error';

  const row = el('div', {
    className: 'span-row ' + kindClass,
    id: 'span-' + span.span_id,
    style: 'padding-left: ' + (10 + depth * 20) + 'px',
  });

  const header = el('div', {className: 'span-row-header'});
  if (kind === 'TOOL' && span.name === 'final_answer') {
    header.appendChild(el('span', {className: 'span-name'}, span.name));
  } else if (kind === 'TOOL' && span.name === 'run_shell_command') {
    const inputVal = getVal(attrs['input.value']);
    let cmd = span.name;
    try {
      const parsed = JSON.parse(inputVal);
      cmd = (parsed.input || parsed).command || cmd;
    } catch (e) {}
    const nameSpan = el('span', {className: 'span-name'}, '$ ');
    nameSpan.appendChild(el('span', {className: 'span-args'}, cmd));
    header.appendChild(nameSpan);
  } else if (kind === 'TOOL') {
    const inputVal = getVal(attrs['input.value']);
    const tc = fmtToolCall(span.name, inputVal);
    const nameSpan = el('span', {className: 'span-name'}, tc.name);
    if (tc.args !== null) {
      nameSpan.appendChild(el('span', {className: 'span-args'}, '(' + tc.args + ')'));
    }
    header.appendChild(nameSpan);
  } else {
    header.appendChild(el('span', {className: 'span-name'}, span.name));
  }
  if (kind) header.appendChild(el('span', {className: 'span-kind'}, kind));
  if (sc) header.appendChild(el('span', {className: 'span-status ' + sc}, statusLabel(effectiveStatus)));

  const isLlm = kind === 'LLM' || (!kind && span.name.endsWith('ChatModel'));
  const isAgent = kind === 'AGENT' || kind === 'CHAIN' || span._placeholder;
  if (isLlm) {
    const pt = Number(getVal(attrs['llm.token_count.prompt']) || 0);
    const ct = Number(getVal(attrs['llm.token_count.completion']) || 0);
    const tc = Number(getVal(attrs['llm.cost.total']) || 0);
    const cr = Number(getVal(attrs['metadata.usage.cached_prompt_tokens']) || 0);
    const cw = Number(getVal(attrs['metadata.usage.cached_creation_tokens']) || 0);
    if (pt || ct) {
      let tokenText = fmtTokens(pt);
      if (cr || cw) {
        const parts = [];
        if (cr) parts.push(fmtTokens(cr) + ' cached');
        if (cw) parts.push(fmtTokens(cw) + ' new');
        tokenText += ' (' + parts.join(', ') + ')';
      }
      tokenText += ' → ' + fmtTokens(ct);
      header.appendChild(el('span', {className: 'span-tokens'}, tokenText));
    }
    if (tc > 0) {
      header.appendChild(el('span', {className: 'span-cost'}, '$' + tc.toFixed(4)));
    }
  } else if (isAgent && span.children && span.children.length > 0) {
    const stats = treeLlmStats(span);
    if (stats.promptTokens || stats.completionTokens) {
      let tokenText = fmtTokens(stats.promptTokens);
      if (stats.cacheRead || stats.cacheWrite) {
        const parts = [];
        if (stats.cacheRead) parts.push(fmtTokens(stats.cacheRead) + ' cached');
        if (stats.cacheWrite) parts.push(fmtTokens(stats.cacheWrite) + ' new');
        tokenText += ' (' + parts.join(', ') + ')';
      }
      tokenText += ' → ' + fmtTokens(stats.completionTokens);
      header.appendChild(el('span', {className: 'span-tokens'}, tokenText));
    }
    if (stats.cost > 0) {
      header.appendChild(el('span', {className: 'span-cost'}, '$' + stats.cost.toFixed(2)));
    }
  }

  header.appendChild(el('span', {className: 'span-duration'}, fmtDuration(span.start_time, span.end_time)));
  row.appendChild(header);

  const detail = extractDetail(attrs, span.name);
  if (detail) {
    const detailDiv = el('div', {className: 'span-detail'});
    detailDiv.appendChild(detail);
    row.appendChild(detailDiv);
  }

  const attrHtml = renderAttrs(attrs);
  if (attrHtml) row.appendChild(attrHtml);

  return row;
}

// ============================================================
// Span Detail Extraction
// ============================================================

function extractDetail(attrs, spanName) {
  const kind = getVal(attrs['openinference.span.kind']);

  if (kind === 'LLM' || (!kind && spanName.endsWith('ChatModel'))) {
    const frag = document.createDocumentFragment();
    let found = false;

    let i = 0;
    while (true) {
      const ctype = getVal(attrs['llm.output_messages.0.message.contents.' + i + '.message_content.type']);
      if (ctype == null) break;
      if (ctype === 'reasoning') {
        const text = getVal(attrs['llm.output_messages.0.message.contents.' + i + '.message_content.text']);
        if (text) {
          frag.appendChild(lazyDetails('reasoning (' + text.length + ' chars)',
            () => el('div', {className: 'detail-reasoning', textContent: text}), true));
          found = true;
        }
      } else if (ctype === 'text') {
        const text = getVal(attrs['llm.output_messages.0.message.contents.' + i + '.message_content.text']);
        if (text) {
          frag.appendChild(el('div', {className: 'detail-text', textContent: text}));
          found = true;
        }
      }
      i++;
    }

    const toolCalls = [];
    i = 0;
    while (true) {
      const name = getVal(attrs['llm.output_messages.0.message.tool_calls.' + i + '.tool_call.function.name']);
      if (name == null) break;
      const args = getVal(attrs['llm.output_messages.0.message.tool_calls.' + i + '.tool_call.function.arguments']) || '';
      toolCalls.push({name, args});
      i++;
    }
    if (toolCalls.length > 0) {
      const label = toolCalls.map(tc => tc.name).join(', ');
      frag.appendChild(lazyDetails('tool calls: ' + label, () => {
        const f = document.createDocumentFragment();
        for (const tc of toolCalls) {
          const truncated = tc.args.length > 500 ? tc.args.slice(0, 500) + '...' : tc.args;
          const toolDiv = el('div', {className: 'detail-tool-call'},
            el('div', {className: 'detail-tool-name', textContent: tc.name}));
          if (truncated) {
            toolDiv.appendChild(el('pre', {textContent: truncated}));
          }
          f.appendChild(toolDiv);
        }
        return f;
      }));
      found = true;
    }

    return found ? frag : null;
  }

  if (spanName === 'error') {
    const output = getVal(attrs['output.value']);
    if (output) {
      const truncated = String(output).slice(0, 1000);
      return el('div', {className: 'detail-error', textContent: truncated});
    }
    return null;
  }

  if (kind === 'TOOL' && spanName === 'final_answer') {
    const inputVal = getVal(attrs['input.value']);
    if (inputVal == null) return null;
    let content;
    try {
      const parsed = JSON.parse(inputVal);
      const args = parsed.input || parsed;
      if (typeof args.response === 'string') {
        try { content = JSON.stringify(JSON.parse(args.response), null, 2); }
        catch (e) { content = args.response; }
      } else {
        content = JSON.stringify(args, null, 2);
      }
    } catch (e) {
      content = String(inputVal);
    }
    return lazyDetails('content (' + content.length + ' chars)',
      () => el('pre', {className: 'detail-tool-io', textContent: content}), true);
  }

  if (kind === 'TOOL' && spanName === 'run_shell_command') {
    const frag = document.createDocumentFragment();
    const outputVal = getVal(attrs['output.value']);
    if (outputVal == null) return null;
    let result;
    try { result = JSON.parse(outputVal); } catch (e) {
      frag.appendChild(el('pre', {className: 'detail-tool-io', textContent: outputVal}));
      return frag;
    }
    if (result.stdout) {
      frag.appendChild(lazyDetails('stdout (' + result.stdout.length + ' chars)',
        () => el('pre', {className: 'detail-tool-io', textContent: result.stdout}), true));
    }
    if (result.stderr) {
      frag.appendChild(lazyDetails('stderr (' + result.stderr.length + ' chars)',
        () => el('pre', {className: 'detail-error', textContent: result.stderr}), true));
    }
    if (result.exit_code !== 0) {
      frag.appendChild(el('div', {className: 'detail-error', textContent: 'exit code: ' + result.exit_code}));
    }
    return frag.childNodes.length ? frag : null;
  }

  if (kind === 'TOOL') {
    const frag = document.createDocumentFragment();
    let found = false;

    const inputVal = getVal(attrs['input.value']);
    if (inputVal != null) {
      let pretty;
      try {
        const parsed = JSON.parse(inputVal);
        pretty = JSON.stringify(parsed.input || parsed, null, 2);
      } catch (e) {
        pretty = String(inputVal);
      }
      frag.appendChild(lazyDetails('input (' + pretty.length + ' chars)',
        () => el('pre', {className: 'detail-tool-io', textContent: pretty})));
      found = true;
    }
    const outputVal = getVal(attrs['output.value']);
    if (outputVal != null) {
      const str = String(outputVal);
      const isError = str.startsWith('ToolError');
      const cls = isError ? 'detail-error' : 'detail-tool-io';
      let pretty = isError ? str.replace(/\n\s*Context: .*/g, '') : str;
      if (!isError) {
        try { pretty = JSON.stringify(JSON.parse(str), null, 2); } catch (e) {}
      }
      frag.appendChild(lazyDetails((isError ? 'error' : 'output') + ' (' + pretty.length + ' chars)',
        () => el('pre', {className: cls, textContent: pretty}), true));
      found = true;
    }
    return found ? frag : null;
  }

  return null;
}

function renderAttrs(attrs) {
  if (!attrs) return null;
  const keys = Object.keys(attrs).sort();
  if (keys.length === 0) return null;

  const lines = keys.map(k => {
    const v = getVal(attrs[k]);
    return '<span class="attr-key">' + escHtml(k) + '</span>: ' + escHtml(String(v));
  }).join('\n');

  return lazyDetails(keys.length + ' attributes',
    () => el('pre', {innerHTML: lines}));
}

// ============================================================
// Agent Sidebar
// ============================================================

function collectAgents(nodes, depth) {
  const agents = [];
  for (const node of nodes) {
    const attrs = node.attributes || {};
    const kind = getVal(attrs['openinference.span.kind']) || '';
    const isAgent = kind === 'AGENT' || kind === 'CHAIN' || node._placeholder
      || node.name.endsWith('Workflow') || node.name.endsWith('Agent') || node.name.endsWith('Analyst');
    if (isAgent) {
      agents.push({
        span_id: node.span_id,
        name: node.name,
        depth: depth,
        status_code: node.status_code,
        end_time: node.end_time,
        children: node.children ? collectAgents(node.children, depth + 1) : [],
      });
    } else if (node.children && node.children.length > 0) {
      agents.push(...collectAgents(node.children, depth));
    }
  }
  return agents;
}

let sidebarScrollHandler = null;
let sidebarScrollLocked = false;
let sidebarScrollLockTimer = null;

function updateSidebarActive(nav, spanIds) {
  if (sidebarScrollLocked) return;
  const headerBottom = 50;
  let active = null;
  for (const id of spanIds) {
    const elem = document.getElementById('span-' + id);
    if (!elem) continue;
    if (elem.getBoundingClientRect().top <= headerBottom) active = id;
  }
  if (active == null && spanIds.length > 0) {
    active = spanIds[0];
  }
  nav.querySelectorAll('.sidebar-item').forEach(item => {
    item.classList.toggle('active', item.getAttribute('data-span-id') === active);
  });
}

function setSidebarActive(nav, spanId) {
  nav.querySelectorAll('.sidebar-item').forEach(item => {
    item.classList.toggle('active', item.getAttribute('data-span-id') === spanId);
  });
}

function renderSidebar(agents) {
  const nav = el('nav', {className: 'trace-sidebar', id: 'trace-sidebar'});

  function addItems(list, depth) {
    for (const agent of list) {
      const sc = statusClass(agent.status_code);
      const item = el('div', {
        className: 'sidebar-item',
        style: 'padding-left: ' + (8 + depth * 16) + 'px',
        'data-span-id': agent.span_id,
        onClick: () => {
          const target = document.getElementById('span-' + agent.span_id);
          if (target) {
            setSidebarActive(nav, agent.span_id);
            sidebarScrollLocked = true;
            if (sidebarScrollLockTimer) clearTimeout(sidebarScrollLockTimer);
            target.scrollIntoView({behavior: 'smooth', block: 'start'});
          }
        },
      });
      item.appendChild(el('span', {className: 'status-dot ' + sc}));
      item.appendChild(el('span', {textContent: agent.name}));
      nav.appendChild(item);
      if (agent.children.length > 0) {
        addItems(agent.children, depth + 1);
      }
    }
  }

  addItems(agents, 0);

  const spanIds = [];
  function gatherIds(list) {
    for (const a of list) {
      spanIds.push(a.span_id);
      gatherIds(a.children);
    }
  }
  gatherIds(agents);

  if (sidebarScrollHandler) window.removeEventListener('scroll', sidebarScrollHandler);
  sidebarScrollHandler = () => {
    if (sidebarScrollLocked) {
      if (sidebarScrollLockTimer) clearTimeout(sidebarScrollLockTimer);
      sidebarScrollLockTimer = setTimeout(() => { sidebarScrollLocked = false; }, 150);
      return;
    }
    updateSidebarActive(nav, spanIds);
  };
  window.addEventListener('scroll', sidebarScrollHandler, {passive: true});
  requestAnimationFrame(() => updateSidebarActive(nav, spanIds));

  return nav;
}

// ============================================================
// Auto-scroll
// ============================================================

let autoScroll = true;
let jumpBtn = null;

function setupAutoScroll() {
  autoScroll = true;
  window.addEventListener('scroll', onScroll);
}

function onScroll() {
  if (state.view !== 'trace') {
    window.removeEventListener('scroll', onScroll);
    return;
  }
  const nearBottom = (window.innerHeight + window.scrollY) >= (document.body.scrollHeight - 100);
  if (nearBottom) {
    autoScroll = true;
    if (jumpBtn) { jumpBtn.remove(); jumpBtn = null; }
  } else {
    autoScroll = false;
  }
}

function maybeAutoScroll() {
  if (autoScroll) {
    window.scrollTo({top: document.body.scrollHeight, behavior: 'smooth'});
  } else if (!jumpBtn) {
    jumpBtn = el('button', {
      className: 'jump-bottom',
      onClick: () => {
        autoScroll = true;
        window.scrollTo({top: document.body.scrollHeight, behavior: 'smooth'});
        if (jumpBtn) { jumpBtn.remove(); jumpBtn = null; }
      },
    }, '↓ jump to bottom');
    document.body.appendChild(jumpBtn);
  }
}

// ============================================================
// Issues View
// ============================================================

async function renderIssues(container) {
  container.appendChild(el('div', {className: 'loading'}, 'loading issues...'));

  try {
    const data = await api.issues();
    const issues = data.issues || [];
    container.innerHTML = '';
    container.appendChild(el('div', {className: 'view-title'}, 'Issues (' + issues.length + ')'));

    if (issues.length === 0) {
      container.appendChild(el('div', {className: 'empty-state'}, 'No issues found.'));
      return;
    }

    const list = el('div', {className: 'issue-list'});
    for (const issue of issues) {
      list.appendChild(el('a', {
        className: 'issue-row',
        href: '#/issues/' + encodeURIComponent(issue),
      }, issue));
    }
    container.appendChild(list);
    setStatus(issues.length + ' issues');
  } catch (e) {
    container.innerHTML = '';
    container.appendChild(el('div', {className: 'error-banner'}, 'Failed to load issues: ' + e.message));
  }

  startPolling(() => refreshIssues(container), 30000);
}

async function refreshIssues(container) {
  try {
    const data = await api.issues();
    const issues = data.issues || [];
    container.innerHTML = '';
    container.appendChild(el('div', {className: 'view-title'}, 'Issues (' + issues.length + ')'));
    if (issues.length === 0) {
      container.appendChild(el('div', {className: 'empty-state'}, 'No issues found.'));
    } else {
      const list = el('div', {className: 'issue-list'});
      for (const issue of issues) {
        list.appendChild(el('a', {
          className: 'issue-row',
          href: '#/issues/' + encodeURIComponent(issue),
        }, issue));
      }
      container.appendChild(list);
    }
    setStatus(issues.length + ' issues');
  } catch (e) {}
}

// ============================================================
// Issue Detail View
// ============================================================

function sinceNanos(seconds) {
  return (Date.now() - seconds * 1000) * 1e6;
}

async function renderIssueDetail(container, issue) {
  container.appendChild(el('div', {className: 'loading'}, 'loading traces for ' + issue + '...'));

  const filtersBar = el('div', {className: 'filters'});
  const sinceSelect = el('select', {className: 'filter-select', onChange: (e) => {
    state.issueFilterSince = parseInt(e.target.value);
    refreshIssueDetail(container, issue);
  }});
  for (const [label, val] of [['1h', 3600], ['3h', 10800], ['12h', 43200], ['24h', 86400], ['3d', 259200], ['7d', 604800], ['30d', 2592000], ['all', 0]]) {
    const opt = el('option', {value: val}, label);
    if (val === state.issueFilterSince) opt.selected = true;
    sinceSelect.appendChild(opt);
  }
  filtersBar.appendChild(el('span', {className: 'filter-label'}, 'since:'));
  filtersBar.appendChild(sinceSelect);

  try {
    const opts = {};
    if (state.issueFilterSince > 0) opts.since = sinceNanos(state.issueFilterSince);
    const data = await api.spans(issue, opts);
    const spans = data.spans || [];
    container.innerHTML = '';
    container.appendChild(el('a', {className: 'back-link', href: '#/issues'}, '← issues'));
    container.appendChild(filtersBar);
    container.appendChild(el('div', {className: 'view-title'}, issue));

    if (spans.length === 0) {
      container.appendChild(el('div', {className: 'empty-state'}, 'No traces found.'));
      return;
    }

    renderIssueTraces(container, issue, spans);
  } catch (e) {
    container.innerHTML = '';
    container.appendChild(el('a', {className: 'back-link', href: '#/issues'}, '← issues'));
    container.appendChild(filtersBar);
    container.appendChild(el('div', {className: 'error-banner'}, 'Failed to load: ' + e.message));
  }

  startPolling(() => refreshIssueDetail(container, issue), 30000);
}

function traceWorkflowName(spans) {
  const root = spans.find(s => !s.parent_span_id || s.parent_span_id === '');
  if (root) return root.name;
  for (const s of spans) {
    const wf = getVal((s.attributes || {})['workflow.name']);
    if (wf) return wf[0].toUpperCase() + wf.slice(1) + 'Workflow';
  }
  return spans[0]?.name || 'trace';
}

function renderIssueTraces(container, issue, spans) {
  const byTrace = new Map();
  for (const s of spans) {
    if (!byTrace.has(s.trace_id)) byTrace.set(s.trace_id, []);
    byTrace.get(s.trace_id).push(s);
  }

  const traceIds = [...byTrace.keys()];
  traceIds.sort((a, b) => {
    const aStart = byTrace.get(a)[0].start_time || 0;
    const bStart = byTrace.get(b)[0].start_time || 0;
    return bStart - aStart;
  });

  for (const tid of traceIds) {
    const traceSpans = byTrace.get(tid);
    const label = traceWorkflowName(traceSpans);
    const first = traceSpans[0];
    const errors = traceSpans.filter(s => s.status_code === 2).length;
    const sc = errors > 0 ? 'error' : statusClass(first.status_code);

    const group = el('div', {className: 'trace-group'});
    const header = el('div', {className: 'trace-group-header', onClick: () => {
      location.hash = '#/trace/' + encodeURIComponent(issue) + '/' + tid;
    }});
    header.appendChild(el('span', {className: 'status-dot ' + sc}));
    header.appendChild(el('span', {}, label + ' — ' + tid.slice(0, 16) + '…'));
    header.appendChild(el('span', {className: 'trace-card-time'}, fmtAgo(first.start_time)));
    header.appendChild(el('span', {}, traceSpans.length + ' spans'));
    if (errors > 0) header.appendChild(el('span', {className: 'error-count'}, errors + ' errors'));
    group.appendChild(header);
    container.appendChild(group);
  }

  setStatus(traceIds.length + ' traces, ' + spans.length + ' spans');
}

async function refreshIssueDetail(container, issue) {
  try {
    const opts = {};
    if (state.issueFilterSince > 0) opts.since = sinceNanos(state.issueFilterSince);
    const data = await api.spans(issue, opts);
    const spans = data.spans || [];
    const filters = container.querySelector('.filters');
    const backLink = container.querySelector('.back-link');
    container.innerHTML = '';
    if (backLink) container.appendChild(backLink);
    if (filters) container.appendChild(filters);
    container.appendChild(el('div', {className: 'view-title'}, issue));

    if (spans.length === 0) {
      container.appendChild(el('div', {className: 'empty-state'}, 'No traces found.'));
      setStatus('0 traces');
      return;
    }

    renderIssueTraces(container, issue, spans);
  } catch (e) {}
}

// ============================================================
// Keyboard shortcuts
// ============================================================

document.addEventListener('keydown', (e) => {
  if (e.target.tagName === 'INPUT' || e.target.tagName === 'SELECT' || e.target.tagName === 'TEXTAREA') return;
  if (e.key === 'Escape') {
    if (state.view === 'trace') location.hash = '#/';
    else if (state.view === 'issue') location.hash = '#/issues';
  }
  if (e.key === 'r' && !e.ctrlKey && !e.metaKey) {
    route();
  }
});

// ============================================================
// Init
// ============================================================

renderHeader();
route();
