// @ts-check
/** Application state shared between panels, with a tiny event bus. */

export const state = {
  health: null,
  catalog: null,
  projects: [],
  /** @type {any} */ project: null,       // full project record (files, versions, jobs, builds)
  /** @type {number|null} */ version: null, // version number open in review
  /** @type {string|null} */ buildId: null,
  activeTab: "brief",
};

const listeners = new Map();

export function on(event, fn) {
  if (!listeners.has(event)) listeners.set(event, new Set());
  listeners.get(event).add(fn);
}

export function emit(event, payload) {
  for (const fn of listeners.get(event) || []) {
    try { fn(payload); } catch (e) { console.error(event, e); }
  }
}

export function assetById(id) {
  return state.catalog?.assets.find((a) => a.id === id) || null;
}

export const ROOM_COLORS = {
  living: "#f2e3c6", dining: "#efd9b8", kitchen: "#d9e8d4", bedroom: "#dde3f2", bathroom: "#d3e9ef",
  toilet: "#d3e9ef", hallway: "#ece9e1", entry: "#ece9e1", office: "#e8def0", storage: "#e4e1da",
  laundry: "#d8e6ea", balcony: "#e0eadc", utility: "#e4e1da", other: "#eeeeee",
};
