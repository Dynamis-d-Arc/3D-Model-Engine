// @ts-check
/** Thin fetch wrapper. Errors carry the backend's {code, message, detail}. */

export class ApiError extends Error {
  /** @param {number} status @param {string} code @param {string} message @param {any} detail */
  constructor(status, code, message, detail) {
    super(message);
    this.status = status;
    this.code = code;
    this.detail = detail;
  }
}

/** @param {string} path @param {RequestInit & {json?: any}} [opts] */
export async function api(path, opts = {}) {
  const init = { ...opts };
  if (opts.json !== undefined) {
    init.body = JSON.stringify(opts.json);
    init.headers = { "Content-Type": "application/json", ...(opts.headers || {}) };
    delete init.json;
  }
  let res;
  try {
    res = await fetch(path, init);
  } catch (e) {
    throw new ApiError(0, "NETWORK", "The backend is not reachable. Is `python -m twin.api` running?", String(e));
  }
  const text = await res.text();
  let body = null;
  try { body = text ? JSON.parse(text) : null; } catch { body = text; }
  if (!res.ok) {
    const err = body && body.error ? body.error : { code: `HTTP_${res.status}`, message: String(text).slice(0, 300) };
    throw new ApiError(res.status, err.code, err.message, err.detail);
  }
  return body;
}

export const get = (p) => api(p);
export const post = (p, json) => api(p, { method: "POST", json: json ?? {} });
export const patch = (p, json) => api(p, { method: "PATCH", json });
export const put = (p, json) => api(p, { method: "PUT", json });

/** @param {string} path @param {FormData} form */
export async function upload(path, form) {
  return api(path, { method: "POST", body: form });
}
