'use strict';
(() => {
  const $ = (id) => document.getElementById(id);
  const state = { view: 'week', period: 'week', dashboard: null, seven: null, loading: false, refreshing: false, request: 0, page: 1, pageSize: 20, pollTimer: null, toastTimer: null, detailId: null, detailPeriod: 'week', detailDate: '', detailReturn: null, pendingFocus: new Set() };
  const fmt = new Intl.NumberFormat('zh-CN');
  const dateFmt = new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
  const views = { week: ['本周概览 / 01', '本周用量', '先看剩余额度，再看本机记录的消耗去向。'], projects: ['项目账本 / 02', '项目累计', '所有目录项目都在这里；累计用量不随周范围切换。'], threads: ['会话记录 / 03', '会话明细', '追溯具体会话，也可以在这里校正项目归属。'], methodology: ['读懂数字 / 04', '统计口径', '哪些有记录，哪些仍然未知，一并说明。'] };
  const arr = (value) => Array.isArray(value) ? value : [];
  const num = (value) => Number.isFinite(Number(value)) ? Number(value) : 0;
  const finite = (value) => value !== null && value !== undefined && value !== '' && Number.isFinite(Number(value));
  const node = (tag, className, text) => { const el = document.createElement(tag); if (className) el.className = className; if (text !== undefined) el.textContent = String(text); return el; };
  const setText = (id, text) => { $(id).textContent = text; };
  const shortName = (project) => String(project?.name || project?.id || '未知项目').replace(/^project[_\s-]*\d+[_\s-]*/i, '') || project.name;
  const exact = (value) => fmt.format(num(value)) + ' token';
  const compact = (value) => { const n = num(value); const divisor = Math.abs(n) >= 1e8 ? 1e8 : Math.abs(n) >= 1e4 ? 1e4 : 1; return { value: divisor === 1 ? fmt.format(n) : new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 2 }).format(n / divisor), unit: divisor === 1e8 ? '亿' : divisor === 1e4 ? '万' : '' }; };
  const compactText = (value) => { const c = compact(value); return c.value + c.unit; };
  function tokenNode(value, className = 'token-number') { const el = node('span', className, compactText(value)); el.title = exact(value); return el; }
  function metric(id, value) { const el = $(id); if (value === null) { el.textContent = '未知'; el.removeAttribute('title'); return; } const c = compact(value); el.replaceChildren(document.createTextNode(c.value), node('small', 'number-unit', c.unit)); el.title = exact(value); }
  function date(value) { if (value === null || value === undefined || value === '') return null; const d = new Date(typeof value === 'number' && value < 1e12 ? value * 1000 : value); return Number.isNaN(d.getTime()) ? null : d; }
  function timestamp(value) { const d = date(value); return d ? dateFmt.format(d).replaceAll('/', '-') : '未知时间'; }
  function day(value) { const d = date(value); return d ? new Intl.DateTimeFormat('sv-SE', { timeZone: 'Asia/Shanghai' }).format(d) : ''; }
  function hasWeek() { const p = state.dashboard?.week?.period; return Boolean(p?.start && p?.end && !state.dashboard.week.quota?.expired && date(state.dashboard.week.quota?.resets_at)?.getTime() > Date.now()); }
  function report(period) { return period === '7d' ? state.seven : state.dashboard?.[period]; }
  function projectIn(data, id) { return arr(data?.projects).find((p) => p.id === id); }
  function focused() { return new Set(arr(state.dashboard?.all?.preferences?.focused_projects)); }
  function catalog() { const map = new Map(); [state.dashboard?.all?.project_options, state.dashboard?.all?.projects, state.dashboard?.week?.projects, state.seven?.projects].forEach((items) => arr(items).forEach((p) => map.set(p.id, { ...map.get(p.id), ...p }))); return [...map.values()]; }
  function projectById(id) { return catalog().find((p) => p.id === id); }
  function lifetime(id) { return projectIn(state.dashboard?.all, id) || state.dashboard?.all?.lifetime?.[id]; }
  function projectValue(project, period) { if (period === 'week' && !hasWeek()) return null; const p = period === 'all' ? lifetime(project.id) : projectIn(report(period), project.id); return p && finite(p.total) ? num(p.total) : null; }
  function absent(period) { return period === 'week' && !hasWeek() ? '周窗口未知' : '本机未检出'; }
  function showError(message) { setText('error-banner', message); $('error-banner').hidden = false; }
  function toast(message, error = false) { const el = $('toast'); el.textContent = message; el.classList.toggle('error', error); el.hidden = false; clearTimeout(state.toastTimer); state.toastTimer = setTimeout(() => { el.hidden = true; }, 4800); }
  function connection(kind, message) { const el = $('connection-status'); el.className = 'connection ' + kind; el.replaceChildren(node('span', 'status-dot'), document.createTextNode(message)); }
  async function api(path, body) { const options = { cache: 'no-store' }; if (body !== undefined) { options.method = 'POST'; options.headers = { 'Content-Type': 'application/json', 'X-Usage-Dashboard': '1' }; options.body = JSON.stringify(body); } const response = await fetch(path, options); const data = await response.json().catch(() => null); if (!response.ok) throw new Error(data?.error || '本地服务返回错误（' + response.status + '）'); if (!data) throw new Error('本地服务没有返回可读取的数据'); return data; }
  function setRefreshing(value) { state.refreshing = value; $('refresh-button').disabled = value || state.loading; $('refresh-button').classList.toggle('is-refreshing', value); setText('refresh-label', value ? '读取中…' : '刷新记录'); }
  function schedulePoll() { clearTimeout(state.pollTimer); state.pollTimer = setTimeout(() => loadData(true), 1400); }
  async function loadData(quiet = false) {
    const requestId = ++state.request; state.loading = true; $('refresh-button').disabled = true;
    if (!quiet) $('main').setAttribute('aria-busy', 'true');
    try {
      const needsSeven = state.period === '7d' || $('thread-period').value === '7d';
      const data = await api('/api/dashboard');
      if (!data.week || !data.all || !Array.isArray(data.all.project_options)) throw new Error('数据接口格式不完整，请确认服务已更新');
      const seven = needsSeven ? await api('/api/data?period=7d') : null;
      if (requestId !== state.request) return;
      state.dashboard = data; state.seven = seven; $('error-banner').hidden = true; $('loading-state').hidden = true;
      render(); setRefreshing(Boolean(data.all.scan?.scanning));
      if (state.refreshing) { connection('scanning', '正在读取记录 · 保留上次结果'); schedulePoll(); }
      else { clearTimeout(state.pollTimer); if (data.all.scan?.failure) { connection('error', '扫描失败 · 保留上次结果'); showError('本次扫描失败，当前仍为上次成功结果：' + data.all.scan.failure); } else connection('', '本地服务已连接'); }
      return true;
    } catch (error) {
      if (requestId !== state.request) return;
      showError('读取失败：' + error.message + (state.dashboard ? '。已保留上次成功结果，可点击“刷新记录”重试。' : '。请确认本地服务仍在运行，再点击“刷新记录”。'));
      connection('error', '连接暂时中断'); setRefreshing(false); clearTimeout(state.pollTimer);
      if (!state.dashboard) $('loading-state').replaceChildren(node('strong', '', '暂时无法读取本机账本'), node('small', '', '连接恢复后，点击右上角“刷新记录”重试。'));
      return false;
    } finally { if (requestId === state.request) { state.loading = false; $('refresh-button').disabled = state.refreshing; $('main').setAttribute('aria-busy', 'false'); } }
  }
  async function refresh() { if (state.refreshing || state.loading) return; setRefreshing(true); connection('scanning', '正在重新读取本机日志'); $('error-banner').hidden = true; try { await api('/api/refresh', {}); schedulePoll(); } catch (error) { setRefreshing(false); connection('error', '刷新未完成'); showError('刷新失败：' + error.message + '。已有结果保持不变。'); } }
  function route() {
    const hash = location.hash.slice(1).split('?')[0];
    if (hash === 'main') { $('main').focus(); return; }
    state.view = Object.hasOwn(views, hash) ? hash : 'week';
    if (hash === 'overview' || !Object.hasOwn(views, hash)) history.replaceState(null, '', '#week');
    const [eyebrow, title, subtitle] = views[state.view]; setText('view-eyebrow', eyebrow); setText('view-title', title); setText('view-subtitle', subtitle); document.title = title + ' · Codex 用量账本';
    document.querySelectorAll('[data-view]').forEach((a) => { const active = a.dataset.view === state.view; a.classList.toggle('active', active); if (active) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current'); });
    document.querySelectorAll('.view').forEach((el) => { el.hidden = !state.dashboard || el.id !== 'view-' + state.view; }); updateExport();
  }
  function updateExport() { const period = state.view === 'projects' ? 'all' : state.view === 'threads' ? $('thread-period').value : state.period; $('export-button').href = '/api/export.csv?period=' + encodeURIComponent(period); }
  function render() { state.windowValid = hasWeek(); setText('updated-at', '记录更新于 ' + timestamp(state.dashboard.all.scan?.last_scan)); renderQuota(); renderWeek(); renderProjects(); renderThreads(); renderMethodology(); route(); if (state.detailId && $('project-dialog').open) renderDetail(); }
  function renderQuota() {
    const q = state.dashboard.week.quota, reset = date(q?.resets_at), sampled = date(q?.sampled_at), expired = !q || Boolean(q.expired) || !reset || reset.getTime() <= Date.now();
    const stale = !expired && (q.stale || !sampled || Date.now() - sampled.getTime() > 3600000);
    const valid = !expired && finite(q.used_percent) && num(q.used_percent) >= 0 && num(q.used_percent) <= 100;
    $('quota-label').firstChild.textContent = '账号周额度剩余 ';
    setText('quota-status', expired ? '等待新快照' : stale ? '过旧 · 历史快照' : '非实时快照');
    $('quota-status').classList.toggle('stale', Boolean(expired || stale));
    if (valid && !stale) { $('quota-remaining').replaceChildren(document.createTextNode(new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 1 }).format(100 - num(q.used_percent))), node('span', '', '%')); setText('quota-used', '采样时已用 ' + new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 1 }).format(num(q.used_percent)) + '%'); }
    else if (valid && stale) { setText('quota-remaining', '未知'); setText('quota-used', '历史快照：剩余 ' + new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 1 }).format(100 - num(q.used_percent)) + '% · 已用 ' + new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 1 }).format(num(q.used_percent)) + '%'); }
    else { setText('quota-remaining', '未知'); setText('quota-used', q ? '上次快照已过重置时间' : '没有可用的账号额度快照'); }
    $('quota-meter').style.width = valid && !stale ? Math.max(0, Math.min(100, 100 - num(q.used_percent))) + '%' : '0%';
    setText('quota-reset', reset ? timestamp(q.resets_at) + (expired ? '（已过）' : '') : '未知，等待快照');
    setText('quota-window', q?.window_start ? (expired ? '上次窗口始于 ' : '本周期始于 ') + timestamp(q.window_start) : '无法确定当前额度周窗口');
    setText('quota-sampled', sampled ? timestamp(q.sampled_at) : '尚未检出');
    setText('quota-note', stale ? '这是一份历史快照，不能代表现在的剩余额度。' : expired ? '重置后尚无新采样，不能推定额度已恢复。' : '刷新仅重读本机日志，不向 OpenAI 查询。');
  }
  function renderWeek() {
    const data = report(state.period), known = state.period !== 'week' || hasWeek();
    document.querySelectorAll('[data-period]').forEach((button) => button.setAttribute('aria-pressed', String(button.dataset.period === state.period)));
    setText('period-caption', known && data?.period?.start ? day(data.period.start) + ' — ' + day(data.period.end) + (state.period === '7d' ? ' · 独立日历范围，非额度周' : ' · 按账号额度周归集') : '当前额度周窗口未知');
    const notice = $('week-window-note'); notice.hidden = known;
    notice.textContent = '没有有效的当前额度周窗口，本周项目量暂不可判断。你可以主动选择“近 7 天”，或查看项目累计。';
    setText('local-total-label', state.period === '7d' ? '近 7 天已记录 token' : '本周期已记录 token');
    metric('total-tokens', known && data ? num(data.totals?.total) : null); metric('agent-tokens', known && data ? num(data.totals?.agent_tokens) : null);
    setText('active-projects', known && data ? arr(data.projects).length : '未知'); setText('ranking-period-label', state.period === '7d' ? '近 7 天 token' : '本额度周 token');
    const ranked = arr(data?.projects).slice().sort((a, b) => num(b.total) - num(a.total));
    const limit = window.matchMedia('(max-width: 600px)').matches ? 3 : 5;
    $('ranking-rows').replaceChildren(...(known && ranked.length ? ranked.slice(0, limit).map((p, i) => projectRow(p, state.period, i + 1)) : [node('div', 'empty-state', known ? '本机尚未检出这个范围内的项目用量，实际消耗未知。' : '周窗口未知，暂不生成项目排名。')]));
    renderFocus();
  }
  function renderFocus() {
    const chosen = focused(), projects = catalog().filter((p) => chosen.has(p.id)); setText('focus-count', projects.length);
    const cards = projects.slice(0, 2).map((p) => { const card = node('article', 'focus-card'), top = node('div', 'focus-card-top'), open = node('button', 'focus-name', shortName(p)); open.type = 'button'; open.dataset.projectId = p.id; open.addEventListener('click', () => openDetail(p.id, open)); top.append(open, focusButton(p)); const values = node('div', 'focus-values'); [state.period, 'all'].forEach((period) => { const cell = node('div'); cell.append(node('span', '', period === 'all' ? '累计 token' : period === '7d' ? '近 7 天 token' : '本额度周 token')); const value = projectValue(p, period); cell.append(value === null ? node('strong', 'unknown-value', absent(period)) : tokenNode(value, 'focus-number')); values.append(cell); }); card.append(top, values); return card; });
    if (!cards.length) { const empty = node('div', 'focus-empty'); empty.append(node('span', 'empty-star', '☆'), node('span', '', '把重要项目留在眼前。点击项目旁的星标，最多在这里预览 2 项。')); cards.push(empty); }
    $('focus-cards').replaceChildren(...cards);
  }
  function focusButton(project) { const selected = focused().has(project.id), button = node('button', 'focus-button' + (selected ? ' selected' : ''), selected ? '★' : '☆'); button.type = 'button'; button.setAttribute('aria-label', (selected ? '取消关注 ' : '关注 ') + shortName(project)); button.setAttribute('aria-pressed', String(selected)); button.title = (selected ? '取消关注' : '关注项目') + ' · 不改变排名'; button.disabled = state.pendingFocus.has(project.id); button.addEventListener('click', (event) => { event.stopPropagation(); toggleFocus(project.id); }); return button; }
  async function toggleFocus(id) {
    if (id === 'unassigned' || state.pendingFocus.has(id)) return;
    const chosen = focused(), selected = !chosen.has(id); state.pendingFocus.add(id); renderWeek(); renderProjects(); if (state.detailId) renderDetail();
    try { await api('/api/preferences', { project_id: id, focused: selected }); selected ? chosen.add(id) : chosen.delete(id); ['week', 'all'].forEach((key) => { state.dashboard[key].preferences = { focused_projects: [...chosen] }; }); toast(selected ? '已关注此项目，偏好已保存在本机。' : '已取消关注。'); await loadData(true); }
    catch (error) { toast('关注保存失败：' + error.message, true); }
    finally { state.pendingFocus.delete(id); renderWeek(); renderProjects(); if (state.detailId) renderDetail(); }
  }
  function projectRow(project, period, rank) {
    const row = node('article', 'project-row'), title = node('div', 'project-title-cell'), rankEl = node('span', 'project-rank', rank ? String(rank).padStart(2, '0') : '');
    const copy = node('div', 'project-copy'), open = node('button', 'project-name-button', shortName(project)); open.type = 'button'; open.title = project.name || project.id; open.dataset.projectId = project.id; open.addEventListener('click', () => openDetail(project.id, open));
    copy.append(open, node('span', 'project-meta', project.id === 'unassigned' ? '待核对归属 · 点击查看' : lifetime(project.id) ? '已检出本机记录 · 点击查看' : '目录已登记 · 用量未知')); title.append(rankEl, copy); row.append(title);
    [period, 'all'].forEach((scope) => { const cell = node('div', 'project-value'); cell.dataset.label = scope === 'all' ? '累计 token' : scope === '7d' ? '近 7 天 token' : '本额度周 token'; const value = projectValue(project, scope); cell.append(value === null ? node('span', 'unknown-value', absent(scope)) : tokenNode(value)); row.append(cell); });
    const actions = node('div', 'project-actions'); if (project.id !== 'unassigned') actions.append(focusButton(project)); else actions.append(node('span', 'focus-placeholder'));
    const arrow = node('button', 'detail-arrow', '↗'); arrow.type = 'button'; arrow.setAttribute('aria-label', '查看 ' + shortName(project) + ' 详情'); arrow.dataset.projectId = project.id; arrow.addEventListener('click', () => openDetail(project.id, arrow)); actions.append(arrow); row.append(actions);
    row.addEventListener('click', (event) => { if (!event.target.closest('button,a,input,select')) openDetail(project.id, open); }); return row;
  }
  function renderProjects() {
    if (!state.dashboard) return;
    metric('lifetime-total', state.dashboard.all.totals?.total || 0); const all = catalog(), query = $('project-search').value.trim().toLocaleLowerCase(), chosen = focused(), scope = $('project-sort').value;
    const filtered = all.filter((p) => (!$('only-focused').checked || chosen.has(p.id)) && [p.name, p.path, p.id, shortName(p)].join(' ').toLocaleLowerCase().includes(query));
    filtered.sort((a, b) => scope === 'name' ? shortName(a).localeCompare(shortName(b), 'zh-CN') : ((projectValue(b, scope) ?? -1) - (projectValue(a, scope) ?? -1)) || shortName(a).localeCompare(shortName(b), 'zh-CN'));
    setText('lifetime-summary', arr(state.dashboard.all.project_options).length + ' 个目录项目 · ' + arr(state.dashboard.all.projects).length + ' 个分组检出记录。累计包含日期未知量。');
    setText('project-count', '显示 ' + filtered.length + ' / ' + all.length + ' 个项目或分组');
    $('project-rows').replaceChildren(...(filtered.length ? filtered.map((p) => projectRow(p, 'week')) : [node('div', 'empty-state', $('only-focused').checked ? '没有匹配的关注项目。可关闭“只看关注”查看所有目录项目。' : '没有匹配项目。试试缩短名称或搜索完整路径。')]));
  }
  const threadTotal = (thread) => !thread.is_agent && finite(thread.including_agents_total) ? num(thread.including_agents_total) : num(thread.total);
  function renderThreads() {
    if (!state.dashboard) return;
    const period = $('thread-period').value, data = report(period), query = $('thread-search').value.trim().toLocaleLowerCase(), includeAgents = $('include-agents').checked;
    const all = period === 'week' && !hasWeek() ? [] : arr(data?.threads), threads = all.filter((t) => (includeAgents || !t.is_agent) && [t.title, t.id, t.project_name, t.model, t.project_id].join(' ').toLocaleLowerCase().includes(query)).sort((a, b) => threadTotal(b) - threadTotal(a));
    const pages = Math.max(1, Math.ceil(threads.length / state.pageSize)); state.page = Math.min(state.page, pages); const start = (state.page - 1) * state.pageSize;
    setText('thread-result-summary', '找到 ' + threads.length + (includeAgents ? ' 个会话与子 Agent 行' : ' 个主会话'));
    const rows = threads.slice(start, start + state.pageSize).map((t) => { const row = node('tr'), title = node('td'), heading = node('div', 'thread-title', t.title || '未命名会话'); heading.title = t.title || t.id; const metadata = node('div', 'thread-meta'); if (t.is_agent) metadata.append(node('span', 'agent-tag', '子 Agent')); const id = node('span', '', String(t.id || '').slice(0, 8) + '…'); id.title = t.id; metadata.append(id); title.append(heading, metadata, node('div', 'thread-model', t.model || '模型未知'));
      const project = node('td'); project.append(node('strong', 'thread-project', shortName({ name: t.project_name || '未归属 / 共享工作' })), node('span', 'cell-secondary', t.basis || '依据未知'));
      const total = node('td', 'numeric'); total.append(tokenNode(threadTotal(t)), node('span', 'cell-secondary', t.is_agent ? '输出 ' + compactText(t.output) : '自身 ' + compactText(t.total)), ...(!t.is_agent ? [node('span', 'cell-secondary', '子 Agent ' + compactText(t.agent_tokens) + '（已含）')] : []));
      const updated = node('td', 'thread-time', timestamp(t.updated_at)); const map = node('td'); if (t.is_agent) { const inherit = node('span', 'mapping-inherit', '继承主会话'); inherit.title = '主会话：' + t.root_thread_id; map.append(inherit); } else map.append(mappingForm(t)); row.append(title, project, total, updated, map); return row; });
    if (!rows.length) { const row = node('tr'), cell = node('td', 'empty-state', period === 'week' && !hasWeek() ? '额度周窗口未知，可主动切换到全部本地记录或近 7 天。' : data ? '没有匹配的本机会话记录。' : '正在读取这个范围的记录…'); cell.colSpan = 5; row.append(cell); rows.push(row); }
    $('thread-rows').replaceChildren(...rows); setText('page-summary', threads.length ? (start + 1) + '–' + Math.min(start + state.pageSize, threads.length) + ' / ' + threads.length + ' 条' : '共 0 条'); $('previous-page').disabled = state.page <= 1; $('next-page').disabled = state.page >= pages; updateExport();
  }
  function mappingForm(thread) {
    const form = node('form', 'mapping-form'), select = node('select', 'mapping-select'), save = node('button', 'mapping-save', '保存'); save.type = 'submit'; save.disabled = true; select.setAttribute('aria-label', '为会话“' + (thread.title || thread.id) + '”指定项目');
    const auto = node('option', '', '自动归属'); auto.value = ''; select.append(auto);
    arr(state.dashboard.all.project_options).forEach((p) => { const option = node('option', '', shortName(p)); option.value = p.id; option.title = p.path || p.name; select.append(option); });
    if (thread.manual && [...select.options].some((o) => o.value === thread.project_id)) select.value = thread.project_id;
    const initial = select.value; select.addEventListener('change', () => { save.disabled = select.value === initial; });
    form.addEventListener('submit', async (event) => { event.preventDefault(); if (save.disabled) return; save.disabled = true; select.disabled = true; save.textContent = '保存中'; try { await api('/api/mapping', { thread_id: thread.id, project_id: select.value }); toast(select.value ? '归属已保存，子 Agent 会跟随主会话。' : '已恢复自动归属。'); await loadData(true); } catch (error) { toast('保存失败：' + error.message, true); save.disabled = false; select.disabled = false; save.textContent = '保存'; } }); form.append(select, save); return form;
  }
  function openDetail(id, trigger) { const p = projectById(id); if (!p) return; state.detailId = id; state.detailPeriod = state.view === 'projects' || (state.view === 'threads' && $('thread-period').value === 'all') ? 'all' : 'week'; state.detailDate = ''; state.detailReturn = trigger || document.activeElement; renderDetail(); $('project-dialog').showModal(); $('detail-close').focus(); }
  function renderDetail() {
    const p = projectById(state.detailId); if (!p) return;
    setText('detail-title', shortName(p)); setText('detail-path', p.path || '未归属 / 共享工作：没有足够证据确定项目路径。');
    const button = $('detail-focus'), isFocused = focused().has(p.id); button.hidden = p.id === 'unassigned'; button.disabled = state.pendingFocus.has(p.id); button.textContent = isFocused ? '★ 已关注' : '☆ 关注项目'; button.setAttribute('aria-pressed', String(isFocused));
    $('detail-compare').replaceChildren(...['week', 'all'].map((period) => { const box = node('div', 'compare-card' + (period === 'week' ? ' current' : '')); box.append(node('span', '', period === 'week' ? '本额度周 token' : '累计 token')); const value = projectValue(p, period); box.append(value === null ? node('strong', 'unknown-value', absent(period)) : tokenNode(value, 'compare-value')); const note = period === 'week' ? hasWeek() ? day(state.dashboard.week.period.start) + ' 起 · 截至最近读取' : '没有有效窗口，不能判断本周用量' : '全部现存本机记录 · 含日期未知量'; box.append(node('small', '', note)); return box; }));
    document.querySelectorAll('[data-detail-period]').forEach((el) => el.setAttribute('aria-pressed', String(el.dataset.detailPeriod === state.detailPeriod)));
    const data = report(state.detailPeriod), record = projectIn(data, p.id), known = state.detailPeriod !== 'week' || hasWeek(), rows = arr(data?.threads).filter((t) => t.project_id === p.id), mainCount = rows.filter((t) => !t.is_agent).length, agentCount = rows.filter((t) => t.is_agent).length;
    setText('detail-scope-note', !known ? '本额度周窗口未知，以下构成暂不可判断。' : !record ? '本机未检出这个范围内的记录，不能据此认定实际消耗为零。' : '输入 + 输出构成总量；缓存、推理与子 Agent 均为子集，不重复相加。');
    const stats = [['输入 token', record?.input, '包含缓存输入'], ['缓存输入', record?.cached, '输入的子集'], ['输出 token', record?.output, '包含推理输出'], ['推理输出', record?.reasoning, '输出的子集'], ['主会话自身', record ? Math.max(0, num(record.total) - num(record.agent_tokens)) : null, mainCount + ' 个主会话，不含子 Agent'], ['子 Agent', record?.agent_tokens, agentCount + ' 个子 Agent，已计入总量']];
    $('detail-breakdown').replaceChildren(...stats.map(([label, value, note]) => { const box = node('div'); box.append(node('span', '', label), known && record && finite(value) ? tokenNode(value, 'breakdown-value') : node('strong', 'unknown-value', '未知'), node('small', '', note)); return box; }));
    const models = arr(record?.models).slice().sort((a, b) => num(b.total) - num(a.total));
    $('detail-models').replaceChildren(...(models.length && known ? models.map((m) => { const row = node('div', 'model-row'), label = node('div', 'model-label'), total = tokenNode(m.total), meter = node('div', 'model-meter'), fill = node('span'); label.append(node('span', '', m.name || '模型未知'), total); fill.style.width = (num(record.total) ? Math.max(0, Math.min(100, num(m.total) / num(record.total) * 100)) : 0) + '%'; meter.append(fill); row.append(label, meter); return row; }) : [node('div', 'empty-state small-empty', '未检出模型构成')]));
    renderDetailTrend(record, known);
    const undated = num(lifetime(p.id)?.undated_tokens); setText('detail-undated', undated ? '累计中有 ' + compactText(undated) + ' token 日期未知，只进入累计，未放入本周或日趋势。' : '日趋势仅使用有可信日期的记录。'); $('detail-undated').title = undated ? exact(undated) : '';
    const threads = rows.filter((t) => !t.is_agent).sort((a, b) => threadTotal(b) - threadTotal(a)); setText('detail-thread-count', threads.length);
    $('detail-threads').replaceChildren(...(threads.length && known ? threads.slice(0, 6).map((t) => { const row = node('div', 'related-thread'), copy = node('div'); copy.append(node('strong', '', t.title || t.id), node('small', '', (t.model || '模型未知') + ' · ' + timestamp(t.updated_at))); const tokens = node('div'); tokens.append(tokenNode(threadTotal(t)), node('small', '', '其中子 Agent ' + compactText(t.agent_tokens))); row.append(copy, tokens); return row; }) : [node('div', 'empty-state small-empty', '这个范围内未检出相关主会话')]));
  }
  function renderDetailTrend(record, known) {
    const daily = arr(record?.daily).slice().sort((a, b) => String(a.date).localeCompare(String(b.date))), input = $('detail-date'), chart = $('detail-trend');
    input.disabled = !known || !daily.length;
    if (!known || !daily.length) { input.value = ''; chart.replaceChildren(node('div', 'empty-state small-empty', '没有可定位的日期记录')); setText('detail-trend-selection', '日期未知的记录不进入日趋势。'); return; }
    input.min = daily[0].date; input.max = daily[daily.length - 1].date;
    if (!state.detailDate || state.detailDate < input.min || state.detailDate > input.max) state.detailDate = input.max;
    input.value = state.detailDate; const end = new Date(state.detailDate + 'T00:00:00Z'), byDate = new Map(daily.map((item) => [item.date, item])), days = [];
    for (let offset = 13; offset >= 0; offset--) { const d = new Date(end); d.setUTCDate(d.getUTCDate() - offset); const key = d.toISOString().slice(0, 10); days.push({ date: key, record: byDate.get(key) }); }
    const max = Math.max(1, ...days.map((item) => num(item.record?.total)));
    const describe = (item) => item.record ? item.date + ' · ' + exact(item.record.total) + ' · 缓存 ' + compactText(item.record.cached) + ' · 输出 ' + compactText(item.record.output) : item.date + ' · 本机未检出记录';
    chart.replaceChildren(...days.map((item, index) => { const button = node('button', 'trend-bar' + (item.record ? '' : ' no-record')); button.type = 'button'; button.title = describe(item); button.setAttribute('aria-label', describe(item)); const track = node('span', 'trend-bar-track'), fill = node('span', 'trend-bar-fill'); fill.style.height = item.record ? Math.max(2, num(item.record.total) / max * 100) + '%' : '2px'; track.append(fill); button.append(track, node('span', 'trend-bar-date', index % 3 === 0 || index === 13 ? item.date.slice(5).replace('-', '/') : '')); button.addEventListener('click', () => { chart.querySelectorAll('.trend-bar').forEach((b) => b.classList.remove('selected')); button.classList.add('selected'); setText('detail-trend-selection', describe(item)); }); return button; }));
    setText('detail-trend-selection', '显示截至 ' + state.detailDate + ' 的 14 天；点击柱条查看精确记录。');
  }
  function renderMethodology() {
    const scan = state.dashboard.all.scan || {}, pairs = [['读取文件', fmt.format(num(scan.files))], ['用量事件', fmt.format(num(scan.events))], ['排除重复', fmt.format(num(scan.duplicates))], ['跨文件复制去重', fmt.format(num(scan.inherited_copies))], ['恢复余额记录', fmt.format(num(scan.baselines))], ['读取异常', fmt.format(num(scan.errors))], ['缺失文件', fmt.format(num(scan.missing_files))], ['无用量会话', fmt.format(num(scan.zero_usage_threads))], ['日期未知 token', compactText(scan.undated_tokens)], ['日期未知事件', fmt.format(num(scan.undated_events))], ['模糊计数器', fmt.format(num(scan.ambiguous_counters))], ['不支持的用量记录', fmt.format(num(scan.unsupported_usage))], ['上次扫描', timestamp(scan.last_scan)], ['扫描状态', scan.scanning ? '正在读取' : scan.failure ? '失败，保留上次结果' : '读取结束']];
    $('scan-details').replaceChildren(...pairs.map(([label, value]) => { const row = node('div'); row.append(node('dt', '', label), node('dd', '', value)); return row; }));
    $('scan-notes').replaceChildren(...arr(scan.notes).map((note) => node('li', '', typeof note === 'string' ? note : JSON.stringify(note))));
    const warnings = [...new Set([...arr(state.dashboard.week.warnings), ...arr(state.dashboard.all.warnings)].map((w) => typeof w === 'string' ? w : w.message || JSON.stringify(w)))]; setText('warnings-detail-label', '查看 ' + warnings.length + ' 条统计边界与扫描提示'); $('warnings-detail').replaceChildren(...warnings.map((w) => node('p', 'notice', w)));
  }
  $('refresh-button').addEventListener('click', refresh);
  $('period-control').addEventListener('click', async (event) => { const button = event.target.closest('[data-period]'); if (!button || button.dataset.period === state.period) return; const previous = state.period; state.period = button.dataset.period; if (state.period === '7d' && !state.seven) { const loaded = await loadData(); if (loaded === false && state.period === '7d') { state.period = previous; renderWeek(); updateExport(); } } else { renderWeek(); updateExport(); } });
  $('project-search').addEventListener('input', renderProjects); $('project-sort').addEventListener('change', renderProjects); $('only-focused').addEventListener('change', renderProjects);
  $('view-all-focused').addEventListener('click', () => { $('only-focused').checked = true; $('project-search').value = ''; renderProjects(); });
  $('view-all-projects').addEventListener('click', () => { $('only-focused').checked = false; $('project-search').value = ''; $('project-sort').value = 'week'; renderProjects(); });
  $('thread-search').addEventListener('input', () => { state.page = 1; renderThreads(); }); $('include-agents').addEventListener('change', () => { state.page = 1; renderThreads(); });
  let previousThreadPeriod = $('thread-period').value;
  $('thread-period').addEventListener('change', async () => { state.page = 1; const selected = $('thread-period').value; if (selected === '7d' && !state.seven) { const loaded = await loadData(); if (loaded === false && $('thread-period').value === selected) { $('thread-period').value = previousThreadPeriod; renderThreads(); } } else renderThreads(); previousThreadPeriod = $('thread-period').value; });
  $('previous-page').addEventListener('click', () => { if (state.page > 1) { state.page--; renderThreads(); $('view-threads').scrollIntoView({ block: 'start' }); } }); $('next-page').addEventListener('click', () => { state.page++; renderThreads(); $('view-threads').scrollIntoView({ block: 'start' }); });
  $('detail-close').addEventListener('click', () => $('project-dialog').close());
  $('project-dialog').addEventListener('click', (event) => { if (event.target !== $('project-dialog')) return; const rect = event.target.getBoundingClientRect(); if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) event.target.close(); });
  $('project-dialog').addEventListener('close', () => { const id = state.detailId; state.detailId = null; const fallback = [...document.querySelectorAll('#view-' + state.view + ' [data-project-id]')].find((el) => el.dataset.projectId === id); const target = state.detailReturn?.isConnected ? state.detailReturn : fallback; target?.focus({ preventScroll: true }); });
  $('detail-focus').addEventListener('click', () => toggleFocus(state.detailId));
  $('detail-range').addEventListener('click', (event) => { const button = event.target.closest('[data-detail-period]'); if (!button || button.dataset.detailPeriod === state.detailPeriod) return; state.detailPeriod = button.dataset.detailPeriod; state.detailDate = ''; renderDetail(); });
  $('detail-date').addEventListener('change', () => { state.detailDate = $('detail-date').value; renderDetailTrend(projectIn(report(state.detailPeriod), state.detailId), state.detailPeriod !== 'week' || hasWeek()); });
  $('detail-view-threads').addEventListener('click', () => { const p = projectById(state.detailId); $('thread-search').value = p?.id || ''; $('thread-period').value = state.detailPeriod; previousThreadPeriod = state.detailPeriod; state.page = 1; $('project-dialog').close(); location.hash = 'threads'; renderThreads(); });
  window.addEventListener('hashchange', () => { route(); window.scrollTo({ top: 0 }); });
  window.matchMedia('(max-width: 600px)').addEventListener('change', () => { if (state.dashboard) renderWeek(); });
  // Recheck local clock freshness without fetching or disrupting an in-progress mapping edit.
  setInterval(() => { if (!state.dashboard) return; renderQuota(); const valid = hasWeek(); if (state.windowValid !== valid) { state.windowValid = valid; renderWeek(); renderProjects(); renderThreads(); if (state.detailId && $('project-dialog').open) renderDetail(); } }, 60000);
  route(); loadData().then(() => { if (state.dashboard && !state.refreshing && !state.dashboard.all.scan?.failure) refresh(); });
})();
