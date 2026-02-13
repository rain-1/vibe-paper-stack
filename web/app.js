const papersEl = document.getElementById('papers');
const template = document.getElementById('paper-template');
const projectFilter = document.getElementById('project-filter');
const statusFilter = document.getElementById('status-filter');
const searchInput = document.getElementById('search-input');
const hideDone = document.getElementById('hide-done');
const authorResults = document.getElementById('author-results');

const state = { papers: [], projects: [], authorResults: [] };

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

function buildQuery() {
  const params = new URLSearchParams();
  if (searchInput.value.trim()) params.set('q', searchInput.value.trim());
  if (statusFilter.value) params.set('status', statusFilter.value);
  if (projectFilter.value) params.set('project_id', projectFilter.value);
  params.set('include_done', hideDone.checked ? 'false' : 'true');
  return params.toString();
}

async function loadProjects() {
  state.projects = await api('/api/projects');
  projectFilter.innerHTML = '<option value="">All projects</option>' +
    state.projects.map((p) => `<option value="${p.id}">${p.name}</option>`).join('');
}

async function loadPapers() {
  state.papers = await api(`/api/papers?${buildQuery()}`);
  render();
}

function metaLine(p) {
  const project = p.project_name || 'No project';
  const author = p.authors.slice(0, 2).join(', ') + (p.authors.length > 2 ? '…' : '');
  return `${project} • ${p.status} • ${author}`;
}

function render() {
  papersEl.innerHTML = '';
  for (const paper of state.papers) {
    const node = template.content.firstElementChild.cloneNode(true);
    node.querySelector('.title').textContent = paper.title;
    node.querySelector('.title').title = paper.title;
    node.querySelector('.meta').textContent = metaLine(paper);
    node.querySelector('.abstract').textContent = paper.abstract;

    const starBtn = node.querySelector('.star-btn');
    starBtn.textContent = paper.starred ? '★' : '☆';
    starBtn.classList.toggle('on', paper.starred);
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
      chip.className = 'tag';
      chip.textContent = `#${tag.name}`;
      chip.title = 'Double click to remove';
      chip.addEventListener('dblclick', async () => {
        await api(`/api/papers/${paper.id}/tags/${tag.id}`, { method: 'DELETE' });
        loadPapers();
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
        loadPapers();
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
    authorResults.textContent = 'No results yet. Search for an author above.';
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

async function searchByAuthor(author, maxResults) {
  state.authorResults = await api(`/api/arxiv/search-by-author?author=${encodeURIComponent(author)}&max_results=${maxResults}`);
  renderAuthorResults();
}

async function batchAddSelected() {
  const selected = Array.from(authorResults.querySelectorAll('input[type="checkbox"]:checked'))
    .map((cb) => cb.dataset.arxivId)
    .filter(Boolean);

  if (selected.length === 0) {
    alert('Select at least one paper to add.');
    return;
  }

  await api('/api/papers/import-arxiv-batch', {
    method: 'POST',
    body: JSON.stringify({ values: selected }),
  });
  await loadPapers();

  for (const item of state.authorResults) {
    if (selected.includes(item.arxiv_id)) item.already_added = true;
  }
  renderAuthorResults();
}

async function patchPaper(id, patch, shouldReload = true) {
  await api(`/api/papers/${id}`, { method: 'PATCH', body: JSON.stringify(patch) });
  if (shouldReload) await loadPapers();
}

for (const el of [projectFilter, statusFilter, hideDone]) {
  el.addEventListener('change', loadPapers);
}
searchInput.addEventListener('input', () => {
  clearTimeout(searchInput._timer);
  searchInput._timer = setTimeout(loadPapers, 250);
});

document.getElementById('import-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const input = document.getElementById('arxiv-input');
  if (!input.value.trim()) return;
  try {
    await api('/api/papers/import-arxiv', {
      method: 'POST',
      body: JSON.stringify({ value: input.value.trim() }),
    });
    input.value = '';
    await loadPapers();
  } catch (err) {
    alert(err.message);
  }
});

document.getElementById('author-search-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const author = document.getElementById('author-input').value.trim();
  const maxResults = Number(document.getElementById('author-max-results').value || 12);
  if (!author) return;
  try {
    await searchByAuthor(author, maxResults);
  } catch (err) {
    alert(err.message);
  }
});

document.getElementById('batch-add-btn').addEventListener('click', async () => {
  try {
    await batchAddSelected();
  } catch (err) {
    alert(err.message);
  }
});

await loadProjects();
await loadPapers();
renderAuthorResults();
