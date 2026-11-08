export const bridge = window.AstrBotPluginView;

// Plugin iframes can have an opaque origin that disallows browser storage.
export const storage = {
  get(key) {
    try {
      return localStorage.getItem(key);
    } catch {
      return null;
    }
  },
  set(key, value) {
    try {
      if (value === null) localStorage.removeItem(key);
      else localStorage.setItem(key, value);
    } catch {
      // Storage availability must never affect playback.
    }
  },
};

export function songKey(song) {
  return JSON.stringify([
    song.player || "",
    song.source || song.player || "",
    String(song.id),
  ]);
}

export async function readAll(endpoint) {
  const items = [];
  let total = 0;
  do {
    const page = await bridge.apiGet(endpoint, {
      limit: 200,
      offset: items.length,
    });
    total = page.total;
    items.push(...page.items);
    if (!page.items.length) break;
  } while (items.length < total);
  return items;
}

export function safeUrl(value) {
  if (!value) return "";
  try {
    const url = new URL(value);
    return ["http:", "https:"].includes(url.protocol) ? url.href : "";
  } catch {
    return "";
  }
}

export function songPayload(item) {
  return item.song_id ? { stored_song_id: item.song_id } : { song: item.song || item };
}
