const API = {
  async request(method, url, body = null, isForm = false) {
    const opts = { method, credentials: 'same-origin' };
    if (body && !isForm) {
      opts.headers = { 'Content-Type': 'application/json' };
      opts.body = JSON.stringify(body);
    } else if (body && isForm) {
      opts.body = body;
    }
    const res = await fetch(url, opts);
    if (!res.ok) {
      let detail = res.statusText;
      try { const j = await res.json(); detail = j.detail || detail; } catch {}
      throw { status: res.status, detail };
    }
    const ct = res.headers.get('content-type') || '';
    if (ct.includes('application/zip') || ct.includes('octet-stream')) return res;
    return res.json();
  },
  get(url) { return this.request('GET', url); },
  post(url, body, isForm = false) { return this.request('POST', url, body, isForm); },
  put(url, body) { return this.request('PUT', url, body); },
  del(url) { return this.request('DELETE', url); },
};

function esc(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

function showErr(el, msg) { el.textContent = msg; el.classList.remove('hidden'); }
function hideErr(el) { el.classList.add('hidden'); }
