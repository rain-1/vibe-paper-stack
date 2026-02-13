const papersEl = document.getElementById('papers');
const template = document.getElementById('paper-template');
const projectFilter = document.getElementById('project-filter');
const projectPicker = document.getElementById('project-picker');
const tagFilter = document.getElementById('tag-filter');
const statusFilter = document.getElementById('status-filter');
const searchInput = document.getElementById('search-input');
const hideDone = document.getElementById('hide-done');
const authorResults = document.getElementById('author-results');

const authorForm = document.getElementById('author-search-form');
const authorInput = document.getElementById('author-input');
const authorMaxResults = document.getElementById('author-max-results');
const searchSource = document.getElementById('search-source');
const authorSearchBtn = document.getElementById('author-search-btn');
const authorLoading = document.getElementById('author-loading');
const authorLoadingText = document.getElementById('author-loading-text');
const batchAddBtn = document.getElementById('batch-add-btn');
const importSubmitBtn = document.getElementById('import-submit-btn');
const importStatus = document.getElementById('import-status');
const authorStatus = document.getElementById('author-status');
const tilesStatus = document.getElementById('tiles-status');
const exportDataBtn = document.getElementById('export-data-btn');
const importDataBtn = document.getElementById('import-data-btn');
const importDataFile = document.getElementById('import-data-file');
const exportStatus = document.getElementById('export-status');
const dataImportStatus = document.getElementById('data-import-status');

const tabTilesBtn = document.getElementById('tab-tiles-btn');
const tabSearchBtn = document.getElementById('tab-search-btn');
const tabDataBtn = document.getElementById('tab-data-btn');
const tabTiles = document.getElementById('tab-tiles');
const tabSearch = document.getElementById('tab-search');
const tabData = document.getElementById('tab-data');

const ARXIV_FAVICON_URL = 'https://static.arxiv.org/static/base/0.17.8/images/icons/favicon.ico';
const LESSWRONG_FAVICON_URL = 'https://www.lesswrong.com/favicon.ico';
const PDF_FAVICON_URL = '/web/pdf-icon.svg';

const state = {
  papers: [],
  projects: [],
  tags: [],
  authorResults: [],
  draggingPaperId: null,
  dragArmedPaperId: null,
  activeTab: 'tiles',
};

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `Request failed: ${res.status}`);
  }
  if (res.status === 204) return null;
  return res.json();
}

function setStatus(target, message = '', type = '') {
  if (!message) {
    target.classList.add('hidden');
    target.classList.remove('error', 'success');
    target.textContent = '';
    return;
  }
  target.textContent = message;
  target.classList.remove('hidden', 'error', 'success');
  if (type) target.classList.add(type);
}

function switchTab(tabName) {
  state.activeTab = tabName;
  const showTiles = tabName === 'tiles';
  const showSearch = tabName === 'search';
  const showData = tabName === 'data';

  tabTiles.classList.toggle('hidden', !showTiles);
  tabSearch.classList.toggle('hidden', !showSearch);
  tabData.classList.toggle('hidden', !showData);
  tabTilesBtn.classList.toggle('active', showTiles);
  tabSearchBtn.classList.toggle('active', showSearch);
  tabDataBtn.classList.toggle('active', showData);
}

function buildQuery() {
  const params = new URLSearchParams();
  if (searchInput.value.trim()) params.set('q', searchInput.value.trim());
  if (statusFilter.value) params.set('status', statusFilter.value);

  if (projectFilter.value === '__none__') {
    params.set('project_none', 'true');
  } else if (projectFilter.value) {
    params.set('project_id', projectFilter.value);
  }

  if (tagFilter.value) params.set('tag', tagFilter.value);
  params.set('include_done', hideDone.checked ? 'false' : 'true');
  return params.toString();
}

function setAuthorLoading(isLoading, label = 'Searching arXiv...') {
  authorLoading.classList.toggle('hidden', !isLoading);
  authorLoadingText.textContent = label;
  authorSearchBtn.disabled = isLoading;
  batchAddBtn.disabled = isLoading;
  authorInput.disabled = isLoading;
  authorMaxResults.disabled = isLoading;
  searchSource.disabled = isLoading;
}

function renderAuthorMessage(message, isError = false) {
  authorResults.innerHTML = '';
  const msg = document.createElement('div');
  msg.className = `author-message${isError ? ' error' : ''}`;
  msg.textContent = message;
  authorResults.append(msg);
}

function clearDragClasses() {
  for (const card of papersEl.querySelectorAll('.card')) {
    card.classList.remove('dragging', 'drop-target');
    card.draggable = false;
  }
}

function sourceBadgeInfo(paper) {
  const sourceId = String(paper.arxiv_id || '');
  if (sourceId.startsWith('lw:')) {
    return { name: 'LW', icon: LESSWRONG_FAVICON_URL };
  }
  if (sourceId.startsWith('pdf:')) {
    return { name: 'PDF', icon: PDF_FAVICON_URL };
  }
  return { name: 'arXiv', icon: ARXIV_FAVICON_URL };
}

function paperOpenUrl(paper) {
  if (paper.arxiv_url) return paper.arxiv_url;

  const sourceId = String(paper.arxiv_id || '');
  if (sourceId.startsWith('lw:')) return `https://www.lesswrong.com/posts/${sourceId.slice(3)}`;
  if (sourceId.startsWith('pdf:')) return null;
  if (sourceId) return `https://arxiv.org/abs/${sourceId}`;
  return null;
}

function setMetaContent(metaEl, paper) {
  metaEl.innerHTML = '';
  const source = sourceBadgeInfo(paper);

  const badge = document.createElement('span');
  badge.className = 'source-badge';

  const icon = document.createElement('img');
  icon.className = 'source-icon';
  icon.alt = source.name;
  icon.src = source.icon;

  const label = document.createElement('span');
  label.textContent = source.name;

  badge.append(icon, label);

  const project = paper.project_name || 'No project';
  const author = paper.authors.slice(0, 2).join(', ') + (paper.authors.length > 2 ? '…' : '');
  const metaText = document.createElement('span');
  metaText.textContent = `${project} • ${paper.status} • ${author}`;

  metaEl.append(badge, metaText);
}

async function persistPaperOrder() {
  await api('/api/papers/reorder', {
    method: 'POST',
    body: JSON.stringify({ paper_ids: state.papers.map((paper) => paper.id) }),
  });
}

async function movePaper(fromPaperId, toPaperId) {
  if (!fromPaperId || !toPaperId || fromPaperId === toPaperId) return;

  const fromIndex = state.papers.findIndex((paper) => paper.id === fromPaperId);
  const toIndex = state.papers.findIndex((paper) => paper.id === toPaperId);
  if (fromIndex < 0 || toIndex < 0 || fromIndex === toIndex) return;

  const original = [...state.papers];
  const [movedPaper] = state.papers.splice(fromIndex, 1);
  state.papers.splice(toIndex, 0, movedPaper);
  render();

  try {
    await persistPaperOrder();
    setStatus(tilesStatus, 'Saved new card order.', 'success');
  } catch (err) {
    state.papers = original;
    render();
    setStatus(tilesStatus, `Reorder failed: ${err.message}`, 'error');
  }
}

async function loadProjects() {
  const selected = projectFilter.value;
  state.projects = await api('/api/projects');

  projectFilter.innerHTML = '<option value="">All projects</option>' +
    '<option value="__none__">No project</option>' +
    state.projects.map((p) => `<option value="${p.id}">${p.name}</option>`).join('');

  if ([...projectFilter.options].some((option) => option.value === selected)) {
    projectFilter.value = selected;
  }

  renderProjectPicker();
}

async function loadTags() {
  const selected = tagFilter.value;
  state.tags = await api('/api/tags');

  tagFilter.innerHTML = '<option value="">All tags</option>' +
    state.tags.map((tag) => `<option value="${tag.name}">#${tag.name}</option>`).join('');

  if ([...tagFilter.options].some((option) => option.value === selected)) {
    tagFilter.value = selected;
  }
}

function renderProjectPicker() {
  projectPicker.innerHTML = '';

  const options = [
    { value: '', label: 'All projects' },
    { value: '__none__', label: 'No project' },
    ...state.projects.map((project) => ({ value: String(project.id), label: project.name })),
  ];

  for (const option of options) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'project-chip';
    button.textContent = option.label;
    button.classList.toggle('active', projectFilter.value === option.value);
    button.addEventListener('click', () => {
      projectFilter.value = option.value;
      renderProjectPicker();
      loadPapers();
    });
    projectPicker.append(button);
  }
}

function hashString(input) {
  let hash = 0;
  for (let i = 0; i < input.length; i += 1) {
    hash = ((hash << 5) - hash + input.charCodeAt(i)) | 0;
  }
  return Math.abs(hash);
}

function tagStyleFromName(tagName) {
  const hue = hashString(tagName) % 360;
  return {
    background: `hsl(${hue} 88% 94%)`,
    border: `hsl(${hue} 62% 78%)`,
    text: `hsl(${hue} 45% 26%)`,
  };
}

function applyTagColor(tagEl, tagName) {
  const style = tagStyleFromName(tagName);
  tagEl.style.setProperty('--tag-bg', style.background);
  tagEl.style.setProperty('--tag-border', style.border);
  tagEl.style.setProperty('--tag-text', style.text);
}

async function loadPapers() {
  state.papers = await api(`/api/papers?${buildQuery()}`);
  render();
}
function render() {
  papersEl.innerHTML = '';
  for (const paper of state.papers) {
    const node = template.content.firstElementChild.cloneNode(true);
    const dragHandle = node.querySelector('.drag-handle');
    node.dataset.paperId = String(paper.id);
    node.draggable = false;

    node.querySelector('.title').textContent = paper.title;
    node.querySelector('.title').title = paper.title;

    const metaEl = node.querySelector('.meta');
    setMetaContent(metaEl, paper);

    const abstractEl = node.querySelector('.abstract');
    const abstractToggle = node.querySelector('.abstract-toggle');
    abstractEl.textContent = paper.abstract;

    const needsAbstractToggle = (paper.abstract || '').length > 360;
    if (needsAbstractToggle) {
      abstractToggle.classList.remove('hidden');
      abstractToggle.textContent = 'Show more';
      abstractToggle.setAttribute('aria-expanded', 'false');
      abstractToggle.addEventListener('click', () => {
        const expanded = abstractEl.classList.toggle('expanded');
        abstractToggle.textContent = expanded ? 'Show less' : 'Show more';
        abstractToggle.setAttribute('aria-expanded', expanded ? 'true' : 'false');
      });
    } else {
      abstractToggle.classList.add('hidden');
      abstractEl.classList.remove('expanded');
    }

    const openPaperLink = node.querySelector('.open-paper-link');
    const openUrl = paperOpenUrl(paper);
    if (openUrl) {
      openPaperLink.href = openUrl;
      openPaperLink.classList.remove('hidden');
    } else {
      openPaperLink.removeAttribute('href');
      openPaperLink.classList.add('hidden');
    }

    dragHandle.addEventListener('mousedown', () => {
      state.dragArmedPaperId = paper.id;
      node.draggable = true;
    });

    dragHandle.addEventListener('touchstart', () => {
      state.dragArmedPaperId = paper.id;
      node.draggable = true;
    }, { passive: true });

    node.addEventListener('dragstart', (event) => {
      if (state.dragArmedPaperId !== paper.id) {
        event.preventDefault();
        return;
      }

      state.draggingPaperId = paper.id;
      node.classList.add('dragging');
      event.dataTransfer.effectAllowed = 'move';
      event.dataTransfer.setData('text/plain', String(paper.id));
    });

    node.addEventListener('dragover', (event) => {
      event.preventDefault();
      if (state.draggingPaperId && state.draggingPaperId !== paper.id) {
        node.classList.add('drop-target');
      }
    });

    node.addEventListener('dragleave', () => {
      node.classList.remove('drop-target');
    });

    node.addEventListener('drop', async (event) => {
      event.preventDefault();
      const draggedId = state.draggingPaperId || Number(event.dataTransfer.getData('text/plain'));
      clearDragClasses();
      state.draggingPaperId = null;
      state.dragArmedPaperId = null;
      await movePaper(Number(draggedId), paper.id);
    });

    node.addEventListener('dragend', () => {
      clearDragClasses();
      state.draggingPaperId = null;
      state.dragArmedPaperId = null;
    });

    dragHandle.addEventListener('mouseup', () => {
      node.draggable = false;
      state.dragArmedPaperId = null;
    });

    dragHandle.addEventListener('mouseleave', () => {
      node.draggable = false;
      state.dragArmedPaperId = null;
    });

    dragHandle.addEventListener('click', (event) => {
      event.preventDefault();
    });

    const starBtn = node.querySelector('.star-btn');
    starBtn.textContent = paper.starred ? '★' : '☆';
    starBtn.classList.toggle('on', paper.starred);
    starBtn.title = 'Starred papers are pinned to the top';
    starBtn.addEventListener('click', () => patchPaper(paper.id, { starred: !paper.starred }));

    const statusSelect = node.querySelector('.status-select');
    statusSelect.value = paper.status;
    statusSelect.addEventListener('change', () => patchPaper(paper.id, { status: statusSelect.value }));

    const ratingSelect = node.querySelector('.rating-select');
    ratingSelect.value = paper.rating || '';
    ratingSelect.addEventListener('change', () => {
      const value = ratingSelect.value ? Number(ratingSelect.value) : null;
      patchPaper(paper.id, { rating: value });
    });

    const tagWrap = node.querySelector('.tags');
    for (const tag of paper.tags) {
      const chip = document.createElement('span');
      chip.className = 'tag filterable';
      chip.textContent = `#${tag.name}`;
      chip.title = 'Click to filter by this tag. Double click to remove';
      applyTagColor(chip, tag.name);
      chip.classList.toggle('filter-active', tagFilter.value === tag.name);
      chip.addEventListener('click', async () => {
        tagFilter.value = tag.name;
        await loadPapers();
      });
      chip.addEventListener('dblclick', async () => {
        await api(`/api/papers/${paper.id}/tags/${tag.id}`, { method: 'DELETE' });
        await loadTags();
        await loadPapers();
      });
      tagWrap.append(chip);
    }

    const tagInput = node.querySelector('.tag-input');
    tagInput.addEventListener('keydown', async (event) => {
      if (event.key === 'Enter' && tagInput.value.trim()) {
        event.preventDefault();
        await api(`/api/papers/${paper.id}/tags`, {
          method: 'POST',
          body: JSON.stringify({ name: tagInput.value.trim() }),
        });
        tagInput.value = '';
        await loadTags();
        await loadPapers();
      }
    });
    const notes = node.querySelector('.notes');
    notes.value = paper.notes;
    let timer = null;
    notes.addEventListener('input', () => {
      clearTimeout(timer);
      timer = setTimeout(() => patchPaper(paper.id, { notes: notes.value }, false), 500);
    });

    papersEl.append(node);
  }
}

function renderAuthorResults() {
  authorResults.innerHTML = '';
  if (state.authorResults.length === 0) {
    renderAuthorMessage('No results yet. Search for an author above.');
    return;
  }

  for (const paper of state.authorResults) {
    const row = document.createElement('label');
    row.className = 'author-row';

    const checkbox = document.createElement('input');
    checkbox.type = 'checkbox';
    checkbox.checked = !paper.already_added;
    checkbox.disabled = paper.already_added;
    checkbox.dataset.arxivId = paper.arxiv_id;

    const text = document.createElement('div');
    text.innerHTML = `<div class="author-title">${paper.title}</div>
      <div class="author-meta">${paper.arxiv_id} • ${paper.authors.slice(0, 3).join(', ')}${paper.authors.length > 3 ? '…' : ''}</div>`;

    const stateBadge = document.createElement('div');
    stateBadge.className = 'author-added';
    stateBadge.textContent = paper.already_added ? 'Already added' : 'New';

    row.append(checkbox, text, stateBadge);
    authorResults.append(row);
  }
}

async function searchBySource(source, query, maxResults) {
  state.authorResults = await api(`/api/search?source=${encodeURIComponent(source)}&q=${encodeURIComponent(query)}&max_results=${maxResults}`);
  renderAuthorResults();
}

async function batchAddSelected() {
  const selected = Array.from(authorResults.querySelectorAll('input[type="checkbox"]:checked'))
    .map((cb) => cb.dataset.arxivId)
    .filter(Boolean);

  if (selected.length === 0) {
    setStatus(authorStatus, 'Select at least one paper to add.', 'error');
    return;
  }

  const selectedPapers = state.authorResults
    .filter((item) => selected.includes(item.arxiv_id))
    .map((item) => ({
      arxiv_id: item.arxiv_id,
      title: item.title,
      abstract: item.abstract,
      authors: item.authors,
      categories: item.categories,
      published_at: item.published_at,
      arxiv_url: item.arxiv_url,
    }));

  await api('/api/papers/import-from-search', {
    method: 'POST',
    body: JSON.stringify({ papers: selectedPapers }),
  });
  await loadPapers();

  for (const item of state.authorResults) {
    if (selected.includes(item.arxiv_id)) item.already_added = true;
  }
  renderAuthorResults();
  setStatus(authorStatus, `Added ${selected.length} paper${selected.length === 1 ? '' : 's'}.`, 'success');
  switchTab('tiles');
}

async function patchPaper(id, patch, shouldReload = true) {
  await api(`/api/papers/${id}`, { method: 'PATCH', body: JSON.stringify(patch) });
  if (shouldReload) await loadPapers();
}

function buildExportFilename() {
  const stamp = new Date().toISOString().replace(/[:.]/g, '-');
  return `vibe-paper-stack-${stamp}.json`;
}

async function exportDataSnapshot() {
  setStatus(exportStatus);
  exportDataBtn.disabled = true;
  try {
    const payload = await api('/api/data/export');
    const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' });
    const href = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = href;
    link.download = buildExportFilename();
    document.body.append(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(href);
    setStatus(exportStatus, `Exported ${payload.papers.length} paper${payload.papers.length === 1 ? '' : 's'}.`, 'success');
  } catch (err) {
    setStatus(exportStatus, `Export failed: ${err.message}`, 'error');
  } finally {
    exportDataBtn.disabled = false;
  }
}

async function importDataSnapshot() {
  setStatus(dataImportStatus);
  const selected = importDataFile.files && importDataFile.files[0];
  if (!selected) {
    setStatus(dataImportStatus, 'Choose a JSON file to import.', 'error');
    return;
  }

  importDataBtn.disabled = true;
  try {
    const text = await selected.text();
    const payload = JSON.parse(text);
    const result = await api('/api/data/import', {
      method: 'POST',
      body: JSON.stringify(payload),
    });

    await loadProjects();
    await loadTags();
    await loadPapers();
    setStatus(
      dataImportStatus,
      `Imported ${result.imported_papers} paper${result.imported_papers === 1 ? '' : 's'}. Total papers: ${result.total_papers}.`,
      'success'
    );
    switchTab('tiles');
  } catch (err) {
    setStatus(dataImportStatus, `Import failed: ${err.message}`, 'error');
  } finally {
    importDataBtn.disabled = false;
  }
}

for (const el of [projectFilter, tagFilter, statusFilter, hideDone]) {
  el.addEventListener('change', async () => {
    if (el === projectFilter) renderProjectPicker();
    await loadPapers();
  });
}
searchInput.addEventListener('input', () => {
  clearTimeout(searchInput._timer);
  searchInput._timer = setTimeout(loadPapers, 250);
});

tabTilesBtn.addEventListener('click', () => switchTab('tiles'));
tabSearchBtn.addEventListener('click', () => switchTab('search'));
tabDataBtn.addEventListener('click', () => switchTab('data'));

document.getElementById('import-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const input = document.getElementById('arxiv-input');
  if (!input.value.trim()) return;
  importSubmitBtn.disabled = true;
  setStatus(importStatus);
  try {
    await api('/api/papers/import-source', {
      method: 'POST',
      body: JSON.stringify({ value: input.value.trim() }),
    });
    input.value = '';
    await loadPapers();
    setStatus(importStatus, 'Paper imported successfully.', 'success');
    switchTab('tiles');
  } catch (err) {
    setStatus(importStatus, `Import failed: ${err.message}`, 'error');
  } finally {
    importSubmitBtn.disabled = false;
  }
});

authorForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  const query = authorInput.value.trim();
  const source = searchSource.value || 'arxiv';
  const sourceLabel = source === 'lesswrong' ? 'LessWrong' : 'arXiv';
  const maxResults = Number(authorMaxResults.value || 12);
  if (!query) return;
  setAuthorLoading(true, `Searching ${sourceLabel} for ${query}...`);
  setStatus(authorStatus);
  try {
    await searchBySource(source, query, maxResults);
  } catch (err) {
    state.authorResults = [];
    renderAuthorMessage(`Search failed: ${err.message}`, true);
    setStatus(authorStatus, `Search failed: ${err.message}`, 'error');
  } finally {
    setAuthorLoading(false);
  }
});

batchAddBtn.addEventListener('click', async () => {
  setStatus(authorStatus);
  try {
    await batchAddSelected();
  } catch (err) {
    setStatus(authorStatus, `Batch add failed: ${err.message}`, 'error');
  }
});

exportDataBtn.addEventListener('click', exportDataSnapshot);
importDataBtn.addEventListener('click', importDataSnapshot);

switchTab('tiles');
await loadProjects();
await loadTags();
await loadPapers();
renderAuthorResults();
