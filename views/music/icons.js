const paths = {
  add: '<path d="M12 5v14M5 12h14"/><path d="M19 5v4h-4"/>',
  arrowRight: '<path d="m9 18 6-6-6-6"/>',
  arrowLeft: '<path d="m15 18-6-6 6-6"/>',
  check: '<path d="m5 12 4 4L19 6"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  close: '<path d="m6 6 12 12M18 6 6 18"/>',
  collapse: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M9 4v16m7-11-3 3 3 3"/>',
  download: '<path d="M12 3v12m0 0 5-5m-5 5-5-5M5 21h14"/>',
  edit: '<path d="M4 20h4L19 9l-4-4L4 16v4Z"/><path d="m13.5 6.5 4 4"/>',
  external: '<path d="M14 4h6v6M20 4l-9 9"/><path d="M18 13v6H5V6h6"/>',
  heart: '<path d="M20.8 4.6a5.5 5.5 0 0 0-7.8 0L12 5.7l-1.1-1.1a5.5 5.5 0 0 0-7.8 7.8l1.1 1.1L12 21l7.7-7.5 1.1-1.1a5.5 5.5 0 0 0 0-7.8Z"/>',
  history: '<path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5M12 7v5l3 2"/>',
  lyrics: '<path d="M5 5h14v11H9l-4 4V5Z"/><path d="M9 9h6M9 12h4"/>',
  menu: '<path d="M4 6h16M4 12h16M4 18h16"/>',
  more: '<circle cx="5" cy="12" r="1" fill="currentColor" stroke="none"/><circle cx="12" cy="12" r="1" fill="currentColor" stroke="none"/><circle cx="19" cy="12" r="1" fill="currentColor" stroke="none"/>',
  music: '<path d="M9 18V5l10-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="16" cy="16" r="3"/>',
  muted: '<path d="M11 5 6 9H3v6h3l5 4V5ZM16 9l5 6m0-6-5 6"/>',
  next: '<path d="m6 5 10 7L6 19V5ZM18 5v14"/>',
  pause: '<path d="M8 5v14M16 5v14"/>',
  play: '<path d="m8 5 11 7-11 7V5Z"/>',
  playlist: '<path d="M4 6h10M4 11h10M4 16h7"/><path d="M18 10v8M15 13h6"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  previous: '<path d="m18 5-10 7 10 7V5ZM6 5v14"/>',
  queue: '<path d="M4 6h12M4 11h12M4 16h8"/><path d="m17 15 4 3-4 3v-6Z"/>',
  repeat: '<path d="m17 2 3 3-3 3"/><path d="M3 11V9a4 4 0 0 1 4-4h13M7 22l-3-3 3-3"/><path d="M21 13v2a4 4 0 0 1-4 4H4M11 10h2v5"/>',
  search: '<circle cx="11" cy="11" r="7"/><path d="m20 20-4-4"/>',
  sequence: '<path d="M4 7h13l3 3-3 3M4 17h13"/>',
  shuffle: '<path d="M4 7h3c4 0 6 10 10 10h3M17 14l3 3-3 3M4 17h3c1.4 0 2.6-1.2 3.7-2.8M14 8.4C15 7.6 16 7 17 7h3M17 4l3 3-3 3"/>',
  trash: '<path d="M4 7h16M9 7V4h6v3M7 7l1 13h8l1-13M10 11v5M14 11v5"/>',
  up: '<path d="m6 15 6-6 6 6"/>',
  down: '<path d="m6 9 6 6 6-6"/>',
  expand: '<path d="M8 3H5a2 2 0 0 0-2 2v3M16 3h3a2 2 0 0 1 2 2v3M8 21H5a2 2 0 0 1-2-2v-3M16 21h3a2 2 0 0 0 2-2v-3"/>',
  volume: '<path d="M11 5 6 9H3v6h3l5 4V5ZM15 9a4 4 0 0 1 0 6M18 6a8 8 0 0 1 0 12"/>',
};

export function icon(name, className = "") {
  const path = paths[name] || paths.music;
  return `<svg class="icon ${className}" viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">${path}</svg>`;
}

export function setIcon(element, name) {
  element.dataset.icon = name;
  element.innerHTML = icon(name);
}
