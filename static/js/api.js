// Pages are served from the same directory whether the app is mounted at /
// or behind a reverse proxy such as /flash_labeling/. Derive that directory
// from the current page so one build works in both layouts.
const APP_BASE_PATH = (() => {
  const path = window.location.pathname;
  const slash = path.lastIndexOf('/');
  const dir = path.endsWith('/') ? path : path.slice(0, slash + 1);
  return dir === '/' ? '' : dir.replace(/\/$/, '');
})();

function appPath(path) {
  if (typeof path !== 'string' || !path) return APP_BASE_PATH || '/';
  if (/^(?:[a-z][a-z0-9+.-]*:)?\/\//i.test(path) || path.startsWith('data:') || path.startsWith('#')) return path;
  if (APP_BASE_PATH && (path === APP_BASE_PATH || path.startsWith(`${APP_BASE_PATH}/`))) return path;
  return `${APP_BASE_PATH}${path.startsWith('/') ? path : `/${path}`}` || '/';
}

const API = {
  async request(method, url, body = null, isForm = false) {
    const opts = { method, credentials: 'same-origin' };
    // FormData callers should not need to rely on a separate boolean flag.
    // This also keeps an older caller from serializing a multipart body as
    // JSON when the page and shared API script came from different revisions.
    const formBody = isForm || (typeof FormData !== 'undefined' && body instanceof FormData);
    if (body && !formBody) {
      opts.headers = { 'Content-Type': 'application/json' };
      opts.body = JSON.stringify(body);
    } else if (body && formBody) {
      opts.body = body;
    }
    const res = await fetch(appPath(url), opts);
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
