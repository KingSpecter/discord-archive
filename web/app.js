'use strict';

// ---- Helpers ----

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

const ESCAPES = {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'};
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ESCAPES[c]);

const amount = n => Number(n || 0).toLocaleString();
const plural = (n, word) => `${amount(n)} ${word}${n === 1 ? '' : 's'}`;

function bytes(n) {
  if (n >= 1073741824) return (n / 1073741824).toFixed(1) + ' GB';
  if (n >= 1048576) return (n / 1048576).toFixed(1) + ' MB';
  if (n >= 1024) return (n / 1024).toFixed(1) + ' KB';
  return n + ' B';
}

function when(value) {
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? value : d.toLocaleString([], {dateStyle: 'medium', timeStyle: 'short'});
}

function dayLabel(day) {
  const d = new Date(day + 'T00:00:00Z');
  if (Number.isNaN(d.getTime())) return day;
  return d.toLocaleDateString([], {weekday: 'short', day: 'numeric', month: 'short', year: 'numeric', timeZone: 'UTC'});
}

function safeURL(value) {
  try {
    const u = new URL(value);
    return ['https:', 'http:'].includes(u.protocol) ? u.href : '';
  } catch {
    return '';
  }
}

const badge = status => `<span class="badge ${esc(status)}">${esc(status)}</span>`;
const empty = text => `<p class="empty">${esc(text)}</p>`;
const loading = text => `<p class="empty loading">${esc(text)}</p>`;

function notify(message) {
  clearTimeout(state.toastTimer);
  $('#toast').textContent = message;
  $('#toast').hidden = false;
  state.toastTimer = setTimeout(() => { $('#toast').hidden = true; }, 6500);
}

async function api(path, data, extra = {}) {
  const post = data !== undefined;
  const response = await fetch('/api/' + path, {
    method: post ? 'POST' : 'GET',
    headers: {...(post ? {'Content-Type': 'application/json'} : {}), ...extra},
    body: post ? JSON.stringify(data) : undefined,
    cache: 'no-store',
  });
  let result;
  try {
    result = await response.json();
  } catch {
    throw new Error('The app returned an unreadable response.');
  }
  if (!response.ok) throw new Error(result.error || 'Request failed');
  return result;
}

// wraps a handler so a failed request shows up as a toast
function handle(fn) {
  return async event => {
    try {
      await fn(event);
    } catch (e) {
      notify(e.message);
    }
  };
}

function saved(key, fallback) {
  try {
    return JSON.parse(localStorage.getItem(key)) ?? fallback;
  } catch {
    return fallback;
  }
}

function save(key, value) {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    notify('Browser storage is unavailable. Settings will only last this session.');
  }
}

// ---- State ----

const state = {
  view: 'export',
  user: null,
  guilds: [],
  guild: null,
  channels: [],
  selected: new Map(),   // channel id -> {name, guild}
  whole: new Set(),      // server ids exported in full
  jobs: [],
  active: null,
  library: null,
  detail: null,
  tab: 'messages',
  offset: 0,
  day: '',
  q: '',
  author: '',
  channel: '',
  logOpen: null,
  logs: {},
  request: 0,
  toastTimer: null,
  timer: null,
  busy: false,
  alive: true,
};

// channel types that have a message history
const READABLE = new Set([0, 2, 5, 10, 11, 12, 13, 15, 16]);
const FORUMS = [15, 16];
const VOICE = [2, 13];
const THREADS = [10, 11, 12];
const VIEWS = ['export', 'library', 'activity', 'settings'];
const PAGE = 50;

// ---- Screens ----

function showConnect() {
  $('#workspace').hidden = true;
  $('#login').hidden = false;
  $('#token').value = '';
  $('#token').type = 'password';
  $('#show-token').textContent = 'Show';
  $('#show-token').setAttribute('aria-label', 'Show token');
  $('#login-error').textContent = '';
  $('#token').focus();
}

function showWorkspace(view = 'export') {
  $('#login').hidden = true;
  $('#workspace').hidden = false;
  switchView(view);
}

function switchView(view) {
  state.view = view;
  $$('.view').forEach(el => { el.hidden = el.id !== 'view-' + view; });
  $$('.tab').forEach(el => el.classList.toggle('active', el.dataset.view === view));
  if (view === 'library') renderLibraryList();
  if (view === 'activity') renderActivity();
}

function applyTheme(mode) {
  document.documentElement.dataset.theme = mode;
  $('#theme').textContent = mode === 'dark' ? 'Light' : 'Dark';
}

// ---- Session ----

function renderSession() {
  const user = state.user;
  $('#connection').textContent = user ? user.name : 'Offline';
  $('#connection').classList.toggle('connected', !!user);
  $('#account-action').textContent = user ? 'Disconnect' : 'Connect';
  $('#offline-banner').hidden = !!user;
  renderSelection();
}

function mergeJobs(recent) {
  const ids = new Set(recent.map(j => j.id));
  state.jobs = [...recent, ...state.jobs.filter(j => !ids.has(j.id))];
}

function applyState(result) {
  state.user = result.user;
  mergeJobs(result.jobs);
  state.active = result.active;
  renderSession();
  if (state.view === 'activity') renderActivity();
  if (state.view === 'library') renderLibraryList();
}

async function refresh() {
  applyState(await api('state'));
}

async function poll() {
  try {
    const result = await api('state');
    const previous = state.jobs;
    applyState(result);

    $('#storage-path').textContent = result.export_path;
    $('#storage-free').textContent = bytes(result.free_bytes);
    $('#storage-db').textContent = bytes(result.database_bytes);
    const total = state.jobs.reduce((n, j) => n + j.messages, 0);
    $('#footer-stats').textContent = state.jobs.length
      ? `${plural(state.jobs.length, 'export')}, ${plural(total, 'message')} saved`
      : 'No exports yet';
    $('#network-note').hidden = !result.network;
    $('#network-note').textContent = result.network;

    const active = state.jobs.find(j => j.id === state.active);
    $('#active-job').hidden = !active;
    if (active) {
      $('#active-job').innerHTML =
        `<b>${esc(active.name)}</b> ${esc(active.note)} ` +
        `<button class="link-button" data-view="activity" type="button">Show</button>`;
    }

    // tell the user when a job ends while they are on another view
    for (const job of state.jobs) {
      const old = previous.find(x => x.id === job.id);
      if (old && old.status !== job.status && ['completed', 'partial', 'failed'].includes(job.status)) {
        notify(job.status === 'completed' ? `${job.name}: export ready.` : `${job.name}: ${job.status}. See Activity.`);
      }
    }
  } catch (e) {
    $('#network-note').hidden = false;
    $('#network-note').textContent = 'The app is not answering. Keep its window open, or start it again. ' + e.message;
  }
  if (state.alive) state.timer = setTimeout(poll, document.hidden ? 8000 : 2500);
}

// ---- Export: servers ----

async function loadGuilds() {
  if (!state.user) return;
  $('#server-list').innerHTML = loading('Loading servers');
  try {
    state.guilds = await api('guilds');
    renderGuilds();
  } catch (e) {
    $('#server-list').innerHTML = empty('Could not load servers. ' + e.message);
    throw e;
  }
}

function renderGuilds() {
  const q = $('#server-search').value.toLowerCase();
  const list = state.guilds.filter(g => g.name.toLowerCase().includes(q));
  $('#server-count').textContent = state.guilds.length;
  if (!list.length) {
    $('#server-list').innerHTML = empty(state.guilds.length ? 'No server matches.' : 'The bot is not in any server yet.');
    return;
  }
  $('#server-list').innerHTML = list.map(g => `
    <div class="row server-row ${state.guild === g.id ? 'active' : ''}">
      <button data-guild="${esc(g.id)}" title="${esc(g.name)}" type="button">${esc(g.name)}</button>
      <label title="Export every channel in this server">
        <input type="checkbox" data-whole="${esc(g.id)}" ${state.whole.has(g.id) ? 'checked' : ''}> All
      </label>
    </div>`).join('');
}

async function chooseGuild(id) {
  state.guild = id;
  renderGuilds();
  const guild = state.guilds.find(g => g.id === id);
  $('#channel-title').textContent = guild?.name || 'Channels';
  $('#channel-subtitle').textContent = 'Loading channels';
  $('#channel-list').innerHTML = loading('Loading channels');
  $('#channel-search').value = '';
  const request = ++state.request;
  try {
    const channels = await api('channels?guild=' + encodeURIComponent(id));
    if (request !== state.request) return;
    state.channels = channels;
    $('#channel-subtitle').textContent = plural(channels.filter(c => READABLE.has(c.type)).length, 'channel');
    renderChannels();
  } catch (e) {
    if (request === state.request) {
      state.channels = [];
      $('#channel-subtitle').textContent = '';
      $('#channel-list').innerHTML = empty('Channels unavailable. ' + e.message);
    }
    throw e;
  }
}

// ---- Export: channels ----

function visibleChannels() {
  const q = $('#channel-search').value.toLowerCase();
  return state.channels.filter(c => READABLE.has(c.type) && c.name?.toLowerCase().includes(q));
}

function channelHint(c) {
  if (FORUMS.includes(c.type)) return 'Forum, posts are exported as threads';
  if (VOICE.includes(c.type)) return 'Voice channel text chat';
  if (THREADS.includes(c.type)) return 'Thread';
  return c.topic || '';
}

function renderChannels() {
  const list = visibleChannels();
  if (!list.length) {
    $('#channel-list').innerHTML = empty('No channel matches. Try another search, or add one by link.');
    return;
  }
  const categories = new Map(state.channels.filter(c => c.type === 4).map(c => [c.id, c]));
  const order = c => categories.get(c.parent_id)?.position || 0;
  const groups = new Map();
  [...list]
    .sort((a, b) => order(a) - order(b) || (a.position || 0) - (b.position || 0))
    .forEach(c => {
      const id = c.parent_id || 'other';
      if (!groups.has(id)) groups.set(id, []);
      groups.get(id).push(c);
    });

  const whole = state.whole.has(state.guild);
  $('#channel-list').innerHTML = [...groups].map(([id, channels]) => `
    <div class="group-head">
      <span>${esc(categories.get(id)?.name || 'Channels')}</span>
      <button class="link-button" data-category="${esc(id)}" type="button">Select group</button>
    </div>
    ${channels.map(c => {
      const hint = channelHint(c);
      const checked = whole || state.selected.has(c.id);
      return `
        <label class="row channel-row">
          <input type="checkbox" data-channel="${esc(c.id)}" ${checked ? 'checked' : ''} ${whole ? 'disabled' : ''}>
          <span class="hash" aria-hidden="true">#</span>
          <span class="channel-text">
            <b>${esc(c.name)}</b>
            ${hint ? `<small title="${esc(hint)}">${esc(hint)}</small>` : ''}
          </span>
        </label>`;
    }).join('')}`).join('');
  renderSelection();
}

function renderSelection() {
  const channels = state.selected.size;
  const servers = state.whole.size;
  const parts = [];
  if (servers) parts.push(plural(servers, 'whole server'));
  if (channels) parts.push(plural(channels, 'channel'));
  const text = parts.join(' and ');
  const threads = $('#include-threads').checked ? 'with threads' : 'without threads';

  $('#selection-count').textContent = text ? text + ' selected' : 'Nothing selected';
  $('#export-summary').textContent = text ? `${text}, ${threads}.` : 'Select channels to export.';
  $('#start-export').disabled = (!channels && !servers) || !state.user || state.busy;
  $('#start-export').textContent = state.active ? 'Add to queue' : 'Start export';
}

function selectChannels(channels) {
  for (const c of channels) state.selected.set(c.id, {name: c.name, guild: state.guild});
  renderChannels();
}

function clearSelection() {
  state.whole.clear();
  state.selected.clear();
  renderGuilds();
  if (state.guild) renderChannels();
  renderSelection();
}

function reviewSelection() {
  const guildName = id => state.guilds.find(g => g.id === id)?.name;
  const servers = [...state.whole].map(id => `
    <div class="row report-row">
      <div><b>${esc(guildName(id) || id)}</b><small>Whole server</small></div>
      <button class="link-button" data-remove-guild="${esc(id)}" type="button">Remove</button>
    </div>`);
  const channels = [...state.selected].map(([id, c]) => `
    <div class="row report-row">
      <div><b>#${esc(c.name || id)}</b><small>${esc(guildName(c.guild) || c.guild || 'Direct channel')}</small></div>
      <button class="link-button" data-remove-channel="${esc(id)}" type="button">Remove</button>
    </div>`);
  $('#selection-items').innerHTML = [...servers, ...channels].join('') || empty('Nothing selected.');
}

async function addByLink(event) {
  event.preventDefault();
  const button = $('button[type=submit]', event.target);
  button.disabled = true;
  try {
    const c = await api('resolve?url=' + encodeURIComponent($('#channel-url').value));
    if (!READABLE.has(c.type)) throw new Error('This channel type has no message history.');
    state.selected.set(c.id, {name: c.name, guild: c.guild_id});
    renderSelection();
    $('#link-dialog').close();
    notify(`#${c.name || c.id} added to the selection.`);
  } finally {
    button.disabled = false;
  }
}

// ---- Export: settings and start ----

function exportOptions() {
  return {
    name: $('#export-name').value,
    formats: $$('#formats input:checked').map(el => el.value),
    from: $('#date-from').value,
    to: $('#date-to').value,
    threads: $('#include-threads').checked,
    media: $('#download-media').checked,
    media_mb: Number($('#media-mb').value),
    budget_mb: Number($('#budget-mb').value),
    contains: $('#contains').value,
    authors: $('#authors').value,
    pinned: $('#pinned').checked,
    exclude_bots: $('#exclude-bots').checked,
  };
}

const PRESET_CHECKS = [['include-threads', 'threads'], ['download-media', 'media'], ['pinned', 'pinned'], ['exclude-bots', 'exclude_bots']];
const PRESET_VALUES = [['media-mb', 'media_mb'], ['budget-mb', 'budget_mb'], ['contains', 'contains'], ['authors', 'authors']];

function savePreset() {
  const preset = exportOptions();
  // name, dates and the channel selection belong to one export, not to the default
  delete preset.name;
  delete preset.from;
  delete preset.to;
  save('archive-preset', preset);
  notify('Saved as the default settings.');
}

function loadPreset(quiet = false) {
  const preset = saved('archive-preset', null);
  if (!preset) {
    if (!quiet) notify('No default saved yet.');
    return;
  }
  $$('#formats input').forEach(el => { el.checked = (preset.formats || ['md']).includes(el.value); });
  for (const [id, key] of PRESET_CHECKS) $('#' + id).checked = !!preset[key];
  for (const [id, key] of PRESET_VALUES) $('#' + id).value = preset[key] ?? '';
  renderSelection();
  if (!quiet) notify('Default settings loaded.');
}

function setRange(button) {
  const days = button.dataset.range;
  $('#date-to').value = '';
  $('#date-from').value = days === 'all' ? '' : new Date(Date.now() - (Number(days) - 1) * 86400000).toISOString().slice(0, 10);
  $$('[data-range]').forEach(b => b.classList.toggle('active', b === button));
}

async function startExport(event) {
  event?.preventDefault();
  if ($('#start-export').disabled) return;
  const settings = exportOptions();
  if (!settings.formats.length) throw new Error('Choose at least one file format.');
  state.busy = true;
  renderSelection();
  try {
    const result = await api('jobs', {...settings, channels: [...state.selected.keys()], guilds: [...state.whole]});
    notify('Export queued.');
    state.active = result.id;
    switchView('activity');
    await refresh();
  } finally {
    state.busy = false;
    renderSelection();
  }
}

// ---- Library: list ----

function renderLibraryList() {
  const q = $('#library-search').value.toLowerCase();
  // skip the redraw when nothing changed, so the list does not flicker on every poll
  const key = JSON.stringify([q, state.library, state.jobs, state.historyEnd]);
  if (state.libraryKey === key) return;
  state.libraryKey = key;

  const list = state.jobs.filter(j => j.name.toLowerCase().includes(q));
  const rows = list.map(j => `
    <button class="row archive-row ${state.library === j.id ? 'active' : ''}" data-library="${esc(j.id)}" type="button">
      <b>${esc(j.name)}</b>
      ${badge(j.status)}
      <small>${plural(j.messages, 'message')} in ${plural(j.channels, 'channel')}</small>
      <small>${esc(when(j.created))}</small>
    </button>`).join('');
  const more = state.jobs.length >= 200 && !state.historyEnd
    ? '<button id="older-history" class="secondary wide" type="button">Load older exports</button>' : '';
  $('#library-list').innerHTML = (rows || empty(state.jobs.length ? 'No export matches.' : 'No exports yet. Start one from the Export tab.')) + more;
}

async function loadOlder(button) {
  button.disabled = true;
  try {
    const rows = await api('history?offset=' + state.jobs.length);
    state.historyEnd = rows.length < 200;
    const ids = new Set(state.jobs.map(j => j.id));
    state.jobs.push(...rows.filter(j => !ids.has(j.id)));
    renderLibraryList();
  } finally {
    button.disabled = false;
  }
}

// ---- Library: reader ----

function resetFilters() {
  state.offset = 0;
  state.day = '';
  state.q = '';
  state.channel = '';
  state.author = '';
}

async function openLibrary(id) {
  state.library = id;
  state.tab = 'messages';
  resetFilters();
  switchView('library');
  $('#library-detail').innerHTML = loading('Opening export');
  await loadDetail();
}

async function loadDetail() {
  const id = state.library;
  const detail = await api('job?id=' + id);
  if (id !== state.library) return;
  state.detail = detail;
  renderDetail();
  if (state.tab === 'messages') await loadMessages();
}

function renderDetail() {
  const {job} = state.detail;
  const tab = (id, text) => `<button data-tab="${id}" class="subtab ${state.tab === id ? 'active' : ''}" type="button">${text}</button>`;
  $('#library-detail').innerHTML = `
    <header class="reader-head">
      <div class="reader-title">
        <h2>${esc(job.name)}</h2>
        ${badge(job.status)}
      </div>
      <p>${esc(job.note || 'Export queued.')}</p>
      <nav class="subtabs">
        ${tab('messages', 'Messages')}
        ${tab('files', 'Files')}
        ${tab('channels', 'Channels')}
        <button id="refresh-library" class="link-button" type="button">Reload</button>
      </nav>
    </header>
    <div id="detail-body" class="reader-body"></div>`;
  if (state.tab === 'messages') renderMessagesTab();
  if (state.tab === 'files') renderFilesTab();
  if (state.tab === 'channels') renderChannelsTab();
}

function renderMessagesTab() {
  const options = state.detail.channels.map(c =>
    `<option value="${esc(c.id)}" ${state.channel === c.id ? 'selected' : ''}>#${esc(c.name)}</option>`).join('');
  $('#detail-body').innerHTML = `
    <form id="reader-form" class="reader-filters">
      <input id="reader-search" type="search" placeholder="Search messages" aria-label="Search messages" value="${esc(state.q)}">
      <select id="reader-channel" aria-label="Channel"><option value="">All channels</option>${options}</select>
      <input id="reader-author" placeholder="Author ID" aria-label="Author ID" value="${esc(state.author)}">
      <button class="secondary" type="submit">Search</button>
      <button id="clear-reader" class="link-button" type="button">Clear</button>
    </form>
    <div id="density" class="density" aria-label="Messages per day"></div>
    <div class="reader-meta">
      <span id="reader-count">Loading</span>
      <span>Oldest first, times in UTC</span>
    </div>
    <div id="messages" class="messages"></div>
    <div class="pager">
      <button id="previous" class="secondary" type="button">Previous</button>
      <span id="page-count"></span>
      <button id="next" class="secondary" type="button">Next</button>
    </div>`;
}

function renderFilesTab() {
  const {job, files} = state.detail;
  const rows = files.map(f => `
    <div class="row file-row">
      <div><b>${esc(f.path)}</b><small>${bytes(f.size)}</small></div>
      <a href="/api/file?job=${encodeURIComponent(job.id)}&path=${encodeURIComponent(f.path)}" download>Download</a>
    </div>`).join('');
  $('#detail-body').innerHTML = `
    <div class="button-row">
      <button id="open-folder" class="secondary" type="button">Open export folder</button>
      <button id="bundle" class="secondary" type="button" ${files.length ? '' : 'disabled'}>Download all as ZIP</button>
    </div>
    ${rows || empty('Files are written when the export finishes. Saved messages can already be read under Messages.')}`;
}

function renderChannelsTab() {
  const rows = state.detail.channels.map(c => `
    <div class="row report-row">
      <div>
        <b>#${esc(c.name)}</b>
        <small>${plural(c.count, 'message')} saved</small>
        ${c.note ? `<small>${esc(c.note)}</small>` : ''}
      </div>
      ${badge(c.status)}
    </div>`).join('');
  $('#detail-body').innerHTML = rows || empty('Channels are listed once the export starts.');
}

async function loadMessages() {
  const jid = state.library;
  const query = new URLSearchParams({
    job: jid, q: state.q, channel: state.channel, author: state.author, day: state.day, offset: state.offset,
  });
  const signature = query.toString();
  state.messageRequest = signature;
  $('#messages').innerHTML = loading('Loading messages');
  const result = await api('messages?' + query);
  // drop the answer if the user moved on while it was loading
  if (jid !== state.library || state.tab !== 'messages' || signature !== state.messageRequest) return;

  const last = Math.min(result.offset + PAGE, result.total);
  $('#reader-count').textContent = plural(result.total, 'message') + (state.day ? ' on ' + dayLabel(state.day) : '');
  $('#page-count').textContent = result.total ? `${amount(result.offset + 1)} to ${amount(last)} of ${amount(result.total)}` : '';
  $('#previous').disabled = state.offset === 0;
  $('#next').disabled = state.offset + PAGE >= result.total;

  renderDensity(result.days);
  let day = '';
  $('#messages').innerHTML = result.messages.map(m => {
    const current = (m.timestamp || '').slice(0, 10);
    const heading = current !== day ? `<h3 class="day">${esc(dayLabel(current))}</h3>` : '';
    day = current;
    return heading + messageHTML(m);
  }).join('') || empty('No messages match. Clear the filters, or check the Channels tab for permission problems.');
  $('#messages').scrollTop = 0;
}

// one bar per day, as tall as that day was busy
function renderDensity(days) {
  const box = $('#density');
  if (!days.length) {
    box.innerHTML = '';
    return;
  }
  const max = Math.max(...days.map(d => d.count));
  box.innerHTML = `
    <div class="density-bars">
      ${days.map(d => `<button class="bar ${state.day === d.day ? 'active' : ''}" data-day="${esc(d.day)}" type="button"
        title="${esc(dayLabel(d.day))}: ${plural(d.count, 'message')}" aria-label="${esc(d.day)}, ${plural(d.count, 'message')}"></button>`).join('')}
    </div>
    <div class="density-scale">
      <span>${esc(days[0].day)}</span>
      ${state.day ? '<button class="link-button" data-day="" type="button">Show all days</button>' : '<span>Click a day to read it</span>'}
      <span>${esc(days[days.length - 1].day)}</span>
    </div>`;
  // heights go through the style object because the page's CSP blocks inline style attributes
  $$('.bar', box).forEach((bar, i) => {
    bar.style.height = Math.max(6, Math.round(days[i].count / max * 100)) + '%';
  });
}

function messageHTML(m) {
  const author = m.author || {};
  const name = author.global_name || author.username || 'Unknown author';
  const link = `https://discord.com/channels/${m.guild_id}/${m.channel_id}/${m.id}`;
  const time = (m.timestamp || '').slice(11, 16);

  let content = m.content || '';
  for (const u of m.mentions || []) {
    content = content.replace(new RegExp('<@!?' + u.id + '>', 'g'), '@' + (u.global_name || u.username || u.id));
  }
  const embeds = (m.embeds || [])
    .map(e => [e.title, e.description, ...(e.fields || []).map(f => f.name + ': ' + f.value)].filter(Boolean).join('\n\n'))
    .filter(Boolean);
  const attachments = (m.attachments || []).map(a => {
    const url = safeURL(a.url);
    return url ? `<a class="attachment" href="${esc(url)}" target="_blank" rel="noreferrer">${esc(a.filename)} <small>${bytes(a.size || 0)}</small></a>` : '';
  }).join('');
  if (!content && !embeds.length && !attachments) content = '[no text]';

  const flags = [];
  if (author.bot) flags.push('bot');
  if (m.pinned) flags.push('pinned');
  if (m.edited_timestamp) flags.push('edited');

  const ref = m.message_reference;
  const refLink = ref?.message_id
    ? `https://discord.com/channels/${ref.guild_id || m.guild_id}/${ref.channel_id || m.channel_id}/${ref.message_id}` : '';

  const extras = [];
  if (m.poll) {
    extras.push(`Poll: ${esc(m.poll.question?.text)} (${(m.poll.answers || []).map(a => esc(a.poll_media?.text)).join(', ')})`);
  }
  if (m.sticker_items?.length) extras.push('Stickers: ' + m.sticker_items.map(s => esc(s.name)).join(', '));
  if (m.reactions?.length) extras.push(m.reactions.map(r => `${esc(r.emoji?.name || 'emoji')} × ${r.count}`).join('  '));

  return `
    <article class="message">
      <a class="time" href="${esc(link)}" target="_blank" rel="noreferrer" title="${esc(m.timestamp)}, open in Discord">${esc(time)}</a>
      <div class="message-main">
        <div class="message-head">
          <b title="${esc(author.id || '')}">${esc(name)}</b>
          <span class="channel-tag">#${esc(m.channel_name)}</span>
          ${flags.map(f => `<span class="flag">${f}</span>`).join('')}
        </div>
        ${refLink ? `<a class="reply" href="${esc(refLink)}" target="_blank" rel="noreferrer">in reply to a message</a>` : ''}
        ${content ? `<pre>${esc(content)}</pre>` : ''}
        ${embeds.map(e => `<pre class="embed">${esc(e)}</pre>`).join('')}
        ${attachments}
        ${extras.map(x => `<p class="extra">${x}</p>`).join('')}
      </div>
    </article>`;
}

async function downloadZip(button) {
  const jid = state.library;
  button.disabled = true;
  button.textContent = 'Creating ZIP';
  try {
    const result = await api('bundle', {id: jid});
    const a = document.createElement('a');
    a.href = '/api/file?job=' + encodeURIComponent(jid) + '&path=' + encodeURIComponent(result.path);
    a.download = 'archive.zip';
    document.body.append(a);
    a.click();
    a.remove();
    notify('ZIP ready. The browser is saving it.');
  } finally {
    button.disabled = false;
    button.textContent = 'Download all as ZIP';
  }
}

// ---- Activity ----

const CAN_PAUSE = ['running', 'queued'];
const CAN_RESUME = ['paused', 'partial', 'failed', 'cancelled'];

function renderActivity() {
  const key = JSON.stringify([state.jobs, state.logOpen, state.logs[state.logOpen]]);
  if (state.activityKey === key) return;
  state.activityKey = key;

  if (!state.jobs.length) {
    $('#activity-list').innerHTML = empty('Nothing here yet. Exports show up with their progress once you start one.');
    return;
  }
  $('#activity-list').innerHTML = state.jobs.map(j => {
    let actions = '';
    if (CAN_PAUSE.includes(j.status)) {
      actions = `<button class="secondary" data-control="pause" data-job="${j.id}" type="button">Pause</button>
                 <button class="link-button" data-control="cancel" data-job="${j.id}" type="button">Cancel</button>`;
    } else if (CAN_RESUME.includes(j.status)) {
      actions = `<button class="secondary" data-control="resume" data-job="${j.id}" type="button">${j.status === 'partial' ? 'Retry gaps' : 'Resume'}</button>`;
    }
    const open = state.logOpen === j.id;
    const log = open ? (state.logs[j.id] || []).map(e =>
      `<li><time>${esc(when(e.at))}</time><span>${esc(e.text)}</span></li>`).join('') : '';
    return `
      <section class="panel job">
        <div class="job-main">
          <div class="job-title"><h2>${esc(j.name)}</h2>${badge(j.status)}</div>
          <p>${esc(j.note || 'Waiting for the export ahead of it.')}</p>
          <progress value="${j.finished}" max="${Math.max(1, j.channels)}"></progress>
        </div>
        <dl class="job-numbers">
          <dt>Messages</dt><dd>${amount(j.messages)}</dd>
          <dt>Channels</dt><dd>${j.finished} of ${j.channels}</dd>
        </dl>
        <div class="job-actions">
          ${actions}
          <button class="secondary" data-library="${j.id}" type="button">Open</button>
          <button class="link-button" data-events="${j.id}" type="button">${open ? 'Hide log' : 'Log'}</button>
        </div>
        ${open ? `<ol class="log">${log || '<li><span>No log lines yet.</span></li>'}</ol>` : ''}
      </section>`;
  }).join('');
}

async function toggleLog(id) {
  if (state.logOpen === id) {
    state.logOpen = null;
  } else {
    state.logOpen = id;
    state.logs[id] = (await api('job?id=' + id)).events;
  }
  renderActivity();
}

async function control(id, action) {
  await api('control', {id, action});
  notify(action === 'resume' ? 'Export queued again.' : 'Stopping, progress is saved.');
  await refresh();
}

// ---- Events: clicks ----

document.addEventListener('click', handle(async event => {
  const target = event.target.closest('button');
  if (!target) return;
  const data = target.dataset;

  if (data.view) return switchView(data.view);
  if (data.guild) return chooseGuild(data.guild);
  if (data.category) return selectChannels(visibleChannels().filter(c => (c.parent_id || 'other') === data.category));
  if (data.library) return openLibrary(data.library);
  if (data.control) return control(data.job, data.control);
  if (data.events) return toggleLog(data.events);
  if (data.close) return $('#' + data.close).close();
  if (data.range) return setRange(target);

  if (data.tab) {
    state.tab = data.tab;
    return loadDetail();
  }
  if (data.day !== undefined) {
    state.day = data.day;
    state.offset = 0;
    return loadMessages();
  }
  if (data.removeChannel) {
    state.selected.delete(data.removeChannel);
    reviewSelection();
    if (state.guild) renderChannels();
    return renderSelection();
  }
  if (data.removeGuild) {
    state.whole.delete(data.removeGuild);
    reviewSelection();
    renderGuilds();
    if (state.guild) renderChannels();
    return renderSelection();
  }

  switch (target.id) {
    case 'refresh-library':
      return loadDetail();
    case 'older-history':
      return loadOlder(target);
    case 'open-folder':
      return notify('Export folder: ' + (await api('open-folder', {id: state.library})).path);
    case 'bundle':
      return downloadZip(target);
    case 'previous':
    case 'next':
      state.offset = Math.max(0, state.offset + (target.id === 'next' ? PAGE : -PAGE));
      return loadMessages();
    case 'clear-reader':
      resetFilters();
      renderDetail();
      return loadMessages();
  }
}));

// ---- Events: inputs ----

document.addEventListener('change', handle(async event => {
  const t = event.target;
  if (t.dataset.whole) {
    const id = t.dataset.whole;
    if (t.checked) {
      state.whole.add(id);
      // single channels of that server are covered now
      for (const [cid, c] of state.selected) if (c.guild === id) state.selected.delete(cid);
    } else {
      state.whole.delete(id);
    }
    if (state.guild) renderChannels();
    renderSelection();
  }
  if (t.dataset.channel) {
    if (t.checked) {
      const c = state.channels.find(c => c.id === t.dataset.channel);
      state.selected.set(t.dataset.channel, {name: c?.name, guild: state.guild});
    } else {
      state.selected.delete(t.dataset.channel);
    }
    renderSelection();
  }
  if (t.id === 'reader-channel') {
    state.channel = t.value;
    state.offset = 0;
    await loadMessages();
  }
  if (t.id === 'date-from' || t.id === 'date-to') $$('[data-range]').forEach(b => b.classList.remove('active'));
  if (t.id === 'include-threads') renderSelection();
}));

document.addEventListener('submit', handle(async event => {
  if (event.target.id !== 'reader-form') return;
  event.preventDefault();
  state.q = $('#reader-search').value;
  state.author = $('#reader-author').value.trim();
  state.offset = 0;
  await loadMessages();
}));

$('#login-form').addEventListener('submit', async event => {
  event.preventDefault();
  $('#connect').disabled = true;
  $('#connect').textContent = 'Connecting';
  $('#login-error').textContent = '';
  // the field is cleared before the request so the token is not left on screen
  const token = $('#token').value;
  $('#token').value = '';
  try {
    state.user = await api('connect', {token});
    state.guilds = [];
    state.channels = [];
    state.guild = null;
    state.whole.clear();
    state.selected.clear();
    renderSession();
    showWorkspace('export');
    await loadGuilds();
  } catch (e) {
    $('#login-error').textContent = e.message;
    if ($('#login').hidden) notify(e.message);
  } finally {
    $('#connect').disabled = false;
    $('#connect').textContent = 'Connect';
  }
});

$('#show-token').addEventListener('click', () => {
  const show = $('#token').type === 'password';
  $('#token').type = show ? 'text' : 'password';
  $('#show-token').textContent = show ? 'Hide' : 'Show';
  $('#show-token').setAttribute('aria-label', show ? 'Hide token' : 'Show token');
});

$('#offline').addEventListener('click', () => showWorkspace('library'));
$('#export-form').addEventListener('submit', handle(startExport));
$('#refresh-guilds').addEventListener('click', handle(loadGuilds));
$('#server-search').addEventListener('input', renderGuilds);
$('#channel-search').addEventListener('input', renderChannels);
$('#library-search').addEventListener('input', renderLibraryList);
$('#select-visible').addEventListener('click', () => selectChannels(visibleChannels()));
$('#clear-selection').addEventListener('click', clearSelection);
$('#save-preset').addEventListener('click', savePreset);
$('#load-preset').addEventListener('click', () => loadPreset());
$('#link-form').addEventListener('submit', handle(addByLink));
$('#settings-connect').addEventListener('click', showConnect);
$('#connect-inline').addEventListener('click', showConnect);
$('#shortcuts').addEventListener('click', () => $('#shortcut-dialog').showModal());

$('#selection-count').addEventListener('click', () => {
  reviewSelection();
  $('#selection-dialog').showModal();
});

$('#paste-link').addEventListener('click', () => {
  $('#link-dialog').showModal();
  $('#channel-url').focus();
});

$('#account-action').addEventListener('click', handle(async () => {
  if (!state.user) return showConnect();
  await api('disconnect', {});
  state.user = null;
  renderSession();
  notify('Disconnected. Running exports are paused and the token is cleared.');
}));

$('#theme').addEventListener('click', () => {
  const mode = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
  applyTheme(mode);
  save('archive-theme', mode);
});

$('#quit').addEventListener('click', handle(async () => {
  if (!confirm('Quit the app? Running exports pause and keep their progress.')) return;
  await api('shutdown', {});
  state.alive = false;
  clearTimeout(state.timer);
  $('#network-note').hidden = false;
  $('#network-note').textContent = 'The app is closed. Run Start.cmd to open it again.';
  notify('App closed. You can close this tab.');
}));

// ---- Events: keyboard ----

document.addEventListener('keydown', event => {
  if ($('#workspace').hidden) return;
  const editing = ['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement.tagName);
  const mod = event.ctrlKey || event.metaKey;

  if (event.altKey && ['1', '2', '3', '4'].includes(event.key)) {
    event.preventDefault();
    switchView(VIEWS[Number(event.key) - 1]);
  }
  if (mod && event.key === 'Enter' && state.view === 'export') {
    event.preventDefault();
    handle(startExport)(event);
  }
  if ((mod && event.key.toLowerCase() === 'k') || (!editing && event.key === '/')) {
    event.preventDefault();
    const input = state.view === 'export' ? $('#channel-search')
      : state.view === 'library' ? $('#reader-search') || $('#library-search') : null;
    input?.focus();
  }
  if (!editing && event.key === '?') $('#shortcut-dialog').showModal();
});

// ---- Boot ----

async function boot() {
  applyTheme(saved('archive-theme', matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'));
  loadPreset(true);
  // the launcher link carries the session key in the hash; trade it for a cookie and drop it from the URL
  const key = new URLSearchParams(location.hash.slice(1)).get('key');
  history.replaceState(null, '', location.pathname);
  try {
    if (key) await api('bootstrap', {}, {'X-Archive-Key': key});
    const result = await api('state');
    state.user = result.user;
    state.jobs = result.jobs;
    renderSession();
    if (state.user) {
      showWorkspace();
      await loadGuilds();
    } else {
      showConnect();
    }
    await poll();
  } catch (e) {
    $('#login-error').textContent = e.message;
    $('#connect').disabled = true;
  }
}

boot();
