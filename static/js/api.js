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
    let res;
    try {
      res = await fetch(appPath(url), opts);
    } catch (cause) {
      throw {
        status: 0,
        detail: `无法连接服务器（${cause.message || '网络请求失败'}）。请检查网络连接后重试。`,
        cause,
      };
    }
    if (!res.ok) {
      const status = res.status;
      let detail = '';
      let responseText = '';
      try {
        responseText = await res.text();
        if (responseText) {
          try { detail = JSON.parse(responseText).detail || ''; } catch {}
        }
      } catch {}
      if (!detail) {
        if (status === 400) {
          detail = '请求被网关拒绝（HTTP 400），上传内容没有完整到达服务器。请重新选择本地 YAML 文件后再试。';
        } else if (status === 502) {
          detail = '网关无法连接标注服务（HTTP 502）。请刷新页面后重试；如果仍然失败，请重新选择本地 YAML 文件。';
        } else if (status === 504) {
          detail = '网关等待标注服务超时（HTTP 504）。请稍后重试。';
        } else {
          detail = `请求失败（HTTP ${status}${res.statusText ? `: ${res.statusText}` : ''}）。`;
        }
      }
      throw { status, detail, responseText };
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
