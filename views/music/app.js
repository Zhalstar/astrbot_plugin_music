import { bridge, readAll, safeUrl, songKey, songPayload, storage } from "./api.js";
import { icon, setIcon } from "./icons.js";
import { MusicPlayer } from "./player.js";

let locale = "zh-CN";
const t = (key) => bridge.t(`views.music.${key}`, key);
const $ = (id) => document.getElementById(id);
const elements = Object.fromEntries(
  [
    "library-nav", "playlist-nav", "new-playlist", "source-count", "search-form",
    "query", "search-button", "source-tabs", "catalog", "collection-art", "view-eyebrow",
    "view-title", "view-meta", "collection-actions", "list-actions", "list-count",
    "list-status", "song-list", "player-cover", "player-title", "player-artist",
    "player-status", "player-favorite", "play-mode", "previous", "toggle-play", "next",
    "player-lyrics", "elapsed", "progress", "total-time", "mute", "volume",
    "show-queue", "queue-count", "close-player", "toast", "action-menu", "dialog",
    "dialog-title", "dialog-body", "dialog-footer", "dialog-close", "mobile-menu",
    "sidebar-scrim", "sidebar", "collapse-sidebar", "audio",
    "source-rail", "sources-left", "sources-right",
    "full-player", "full-player-close", "full-player-queue", "full-player-cover",
    "full-player-title", "full-player-artist", "full-player-lyrics", "full-player-elapsed",
    "full-player-progress", "full-player-total", "full-player-mode", "full-player-previous",
    "full-player-toggle", "full-player-next", "full-player-favorite",
  ].map((id) => [id.replace(/-([a-z])/g, (_, letter) => letter.toUpperCase()), $(id)]),
);

const state = {
  view: { type: "favorites" },
  items: [],
  playlists: [],
  sources: [],
  source: storage.get("astrbot.music.source") || "",
  query: "",
  requestToken: 0,
  loading: false,
  error: "",
};
const favoriteRequests = new Set();
const mobileLayout = window.matchMedia("(max-width: 680px)");
let fullPlayerLyrics = { key: "", lines: [], current: -1 };
const lyricRequests = new Set();

function setSidebarOpen(open) {
  if (mobileLayout.matches) {
    document.body.classList.toggle("sidebar-open", open);
  } else {
    document.body.classList.remove("sidebar-open");
    document.body.classList.toggle("sidebar-collapsed", !open);
  }
  if (!open && elements.sidebar.contains(document.activeElement)) elements.mobileMenu.focus();
  elements.sidebar.inert = !open;
  elements.sidebarScrim.inert = !mobileLayout.matches || !open;
  elements.sidebar.setAttribute("aria-hidden", String(!open));
  elements.mobileMenu.setAttribute("aria-expanded", String(open));
  elements.collapseSidebar.setAttribute("aria-expanded", String(open));
  requestAnimationFrame(updateSourceScroll);
}

function updateSourceScroll() {
  const tabs = elements.sourceTabs;
  const maximum = Math.max(0, tabs.scrollWidth - tabs.clientWidth);
  elements.sourceRail.classList.toggle("is-overflowing", maximum > 1);
  elements.sourcesLeft.disabled = tabs.scrollLeft <= 1;
  elements.sourcesRight.disabled = tabs.scrollLeft >= maximum - 1;
}

let toastTimer;
function toast(message) {
  clearTimeout(toastTimer);
  elements.toast.textContent = message;
  elements.toast.classList.add("visible");
  toastTimer = setTimeout(() => elements.toast.classList.remove("visible"), 2600);
}

function formatTime(value) {
  const seconds = Number.isFinite(Number(value)) ? Math.max(Math.floor(Number(value)), 0) : 0;
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
}

function itemSong(item) {
  return item.song || item;
}

function makeButton(label, iconName, className = "icon-button") {
  const button = document.createElement("button");
  button.type = "button";
  button.className = className;
  button.title = label;
  button.setAttribute("aria-label", label);
  button.innerHTML = icon(iconName);
  return button;
}

function coverContent(container, song) {
  container.replaceChildren();
  const cover = safeUrl(song?.cover_url);
  if (cover) {
    const image = document.createElement("img");
    image.src = cover;
    image.alt = "";
    image.loading = "lazy";
    image.referrerPolicy = "no-referrer";
    image.addEventListener("error", () => {
      container.innerHTML = icon("music");
    }, { once: true });
    container.append(image);
  } else {
    container.innerHTML = icon("music");
  }
}

function parseLyrics(value) {
  if (typeof value !== "string" || !value.trim()) return [];
  const lines = [];
  const plainLines = [];
  for (const raw of value.split(/\r?\n/)) {
    const tags = [...raw.matchAll(/\[(\d{1,3}):(\d{2})(?:[.:](\d{1,3}))?\]/g)];
    const text = raw.replace(/\[[^\]]+\]/g, "").trim();
    if (!tags.length && text) plainLines.push(text);
    for (const tag of tags) {
      const fraction = Number(tag[3] || 0);
      lines.push({
        time: Number(tag[1]) * 60 + Number(tag[2]) + (tag[3]?.length === 3 ? fraction / 1000 : fraction / 100),
        text: text || "♪",
      });
    }
  }
  if (!lines.length) {
    return plainLines.map((text, index) => ({
      time: index ? Number.POSITIVE_INFINITY : 0,
      text,
    }));
  }
  return lines.sort((left, right) => left.time - right.time);
}

function renderFullPlayerLyrics(song, current = 0) {
  const key = song ? songKey(song) : "";
  if (key !== fullPlayerLyrics.key) {
    fullPlayerLyrics = { key, lines: parseLyrics(song?.lyrics), current: -1 };
    elements.fullPlayerLyrics.replaceChildren();
    if (!fullPlayerLyrics.lines.length) {
      const empty = document.createElement("div");
      empty.className = "full-player-lyrics-empty";
      empty.textContent = song ? t("lyricsLoading") : t("noSongs");
      elements.fullPlayerLyrics.append(empty);
      return;
    }
    fullPlayerLyrics.lines.forEach((line, index) => {
      const element = document.createElement("button");
      element.type = "button";
      element.className = "lyric-line";
      element.textContent = line.text;
      element.addEventListener("click", () => player.seek(line.time));
      element.dataset.index = String(index);
      elements.fullPlayerLyrics.append(element);
    });
  }
  let active = -1;
  fullPlayerLyrics.lines.forEach((line, index) => {
    if (line.time <= current) active = index;
  });
  if (active === fullPlayerLyrics.current) return;
  fullPlayerLyrics.current = active;
  const lines = elements.fullPlayerLyrics.querySelectorAll(".lyric-line");
  lines.forEach((line, index) => {
    line.classList.toggle("active", index === active);
    if (index === active) line.scrollIntoView?.({ block: "center", behavior: "smooth" });
  });
}

function setFullPlayer(open) {
  elements.fullPlayer.hidden = !open;
  document.body.classList.toggle("full-player-open", open);
  if (open) {
    renderFullPlayerLyrics(player.current ? itemSong(player.current) : null, player.audio.currentTime || 0);
    loadFullPlayerLyrics(player.current);
    elements.fullPlayerClose.focus();
  } else {
    elements.playerCover.focus();
  }
}

async function loadFullPlayerLyrics(item) {
  const song = item ? itemSong(item) : null;
  if (!song || song.lyrics || lyricRequests.has(songKey(song))) return;
  const key = songKey(song);
  lyricRequests.add(key);
  try {
    const result = await bridge.apiPost("lyrics", {
      query: song.name || "",
      artist: song.artists || "",
      platform: song.player || "",
    });
    if (result?.lyrics && player.current && songKey(itemSong(player.current)) === key) {
      song.lyrics = result.lyrics;
      // The song identity stays the same after the async response; invalidate
      // the rendered lyric cache so the newly fetched lines are mounted.
      fullPlayerLyrics.key = "";
      renderFullPlayerLyrics(song, player.audio.currentTime || 0);
    }
    if (!result?.lyrics && player.current && songKey(itemSong(player.current)) === key) {
      const fallback = await bridge.apiPost("lyrics", {
        query: song.name || "",
        artist: song.artists || "",
        platform: "",
      });
      if (fallback?.lyrics && player.current && songKey(itemSong(player.current)) === key) {
        song.lyrics = fallback.lyrics;
        fullPlayerLyrics.key = "";
        renderFullPlayerLyrics(song, player.audio.currentTime || 0);
      } else {
        const empty = elements.fullPlayerLyrics.querySelector(".full-player-lyrics-empty");
        if (empty) empty.textContent = t("lyricsMissing");
      }
    }
  } catch {
    // Lyrics are optional; keep the player usable when the source is unavailable.
  } finally {
    lyricRequests.delete(key);
  }
}

function applyStaticText() {
  document.documentElement.lang = locale;
  document.querySelectorAll("[data-text]").forEach((element) => {
    element.textContent = t(element.dataset.text);
  });
  elements.query.placeholder = t("search");
  elements.query.setAttribute("aria-label", t("search"));
  elements.searchButton.title = t("searchButton");
  elements.searchButton.setAttribute("aria-label", t("searchButton"));
  for (const [key, iconName, label] of [
    ["newPlaylist", "plus", "createPlaylist"], ["mobileMenu", "menu", "menu"],
    ["collapseSidebar", "collapse", "collapseSidebar"],
    ["sourcesLeft", "arrowLeft", "sourcesLeft"], ["sourcesRight", "arrowRight", "sourcesRight"],
    ["previous", "previous", "previous"], ["next", "next", "next"],
    ["playerLyrics", "lyrics", "lyrics"], ["showQueue", "queue", "queue"],
    ["closePlayer", "close", "close"], ["dialogClose", "close", "dismiss"],
    ["playerCover", "expand", "openFullPlayer"], ["fullPlayerClose", "down", "closeFullPlayer"],
    ["fullPlayerQueue", "queue", "queue"], ["fullPlayerPrevious", "previous", "previous"],
    ["fullPlayerNext", "next", "next"], ["fullPlayerToggle", "play", "play"],
    ["fullPlayerMode", "sequence", "sequence"], ["fullPlayerFavorite", "heart", "favorite"],
  ]) {
    setButtonIcon(elements[key], iconName, t(label));
  }
  elements.sidebarScrim.setAttribute("aria-label", t("dismiss"));
  elements.volume.setAttribute("aria-label", t("volume"));
  elements.progress.setAttribute("aria-label", t("progress"));
}

function setButtonIcon(button, iconName, label) {
  setIcon(button, iconName);
  if (button === elements.showQueue) button.append(elements.queueCount);
  button.title = label;
  button.setAttribute("aria-label", label);
}

function renderSources() {
  elements.sourceTabs.replaceChildren();
  [{ name: "", display_name: t("automatic") }, ...state.sources].forEach((source) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `source-tab${state.source === source.name ? " active" : ""}`;
    button.textContent = source.display_name;
    button.setAttribute("aria-pressed", String(state.source === source.name));
    button.addEventListener("click", () => {
      state.source = source.name;
      storage.set("astrbot.music.source", source.name);
      renderSources();
      if (state.query) search(state.query);
    });
    elements.sourceTabs.append(button);
  });
  elements.sourceCount.textContent = `${state.sources.length} ${t("sources")}`;
  requestAnimationFrame(() => {
    elements.sourceTabs.querySelector(".active")?.scrollIntoView({ block: "nearest", inline: "nearest" });
    updateSourceScroll();
  });
}

function renderNavigation() {
  const entries = [
    ["search", "search", t("searchResults")],
    ["favorites", "heart", t("favorites")],
    ["history", "history", t("history")],
    ["queue", "queue", t("queue")],
  ];
  elements.libraryNav.replaceChildren();
  entries.forEach(([type, iconName, label]) => {
    const button = makeButton(label, iconName, "");
    button.innerHTML += `<span>${label}</span>`;
    if (state.view.type === type) button.setAttribute("aria-current", "page");
    button.addEventListener("click", () => type === "search" ? search(elements.query.value) : loadView({ type }));
    elements.libraryNav.append(button);
  });

  elements.playlistNav.replaceChildren();
  if (!state.playlists.length) {
    const empty = document.createElement("div");
    empty.className = "source-count";
    empty.textContent = t("noPlaylists");
    elements.playlistNav.append(empty);
  }
  state.playlists.forEach((playlist) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `playlist-link${state.view.type === "playlist" && state.view.id === playlist.id ? " active" : ""}`;
    button.innerHTML = `${icon("playlist")}<span class="playlist-name"></span><span class="playlist-count"></span>`;
    button.querySelector(".playlist-name").textContent = playlist.name;
    button.querySelector(".playlist-count").textContent = playlist.song_count;
    button.addEventListener("click", () => loadView({ type: "playlist", id: playlist.id }));
    elements.playlistNav.append(button);
  });
}

function viewDetails() {
  if (state.view.type === "favorites") return [t("library"), t("favorites"), t("favoritesHint"), "heart"];
  if (state.view.type === "history") return [t("library"), t("history"), t("historyHint"), "history"];
  if (state.view.type === "queue") return [t("library"), t("queue"), t("queueHint"), "queue"];
  if (state.view.type === "search") return [t("searchResults"), state.query || t("searchResults"), state.source ? state.sources.find((source) => source.name === state.source)?.display_name || state.source : t("automatic"), "search"];
  const playlist = state.playlists.find((candidate) => candidate.id === state.view.id);
  return [t("myPlaylists"), playlist?.name || t("myPlaylists"), t("playlistHint"), "playlist"];
}

function renderHeading() {
  const [eyebrow, title, hint, artIcon] = viewDetails();
  elements.viewEyebrow.textContent = eyebrow;
  elements.viewTitle.textContent = title;
  elements.viewMeta.textContent = hint;
  elements.collectionArt.innerHTML = icon(artIcon);
  elements.collectionActions.replaceChildren();
  elements.listActions.replaceChildren();
  elements.listCount.textContent = `${state.items.length} ${t("songs")}`;

  if (state.items.length) {
    const playAll = makeButton(t("playAll"), "play", "text-button");
    playAll.append(document.createTextNode(t("playAll")));
    playAll.addEventListener("click", () => player.setQueue(state.items, 0, true));
    const shuffle = makeButton(t("shuffleAll"), "shuffle", "text-button");
    shuffle.append(document.createTextNode(t("shuffleAll")));
    shuffle.addEventListener("click", () => {
      player.mode = "shuffle";
      player.setQueue(state.items, Math.floor(Math.random() * state.items.length), true);
    });
    elements.listActions.append(playAll, shuffle);
  }

  if (state.view.type === "history" && state.items.length) {
    const clear = makeButton(t("clear"), "trash", "text-button");
    clear.append(document.createTextNode(t("clear")));
    clear.addEventListener("click", clearHistory);
    elements.collectionActions.append(clear);
  }
  if (state.view.type === "playlist") {
    const rename = makeButton(t("renamePlaylist"), "edit");
    rename.addEventListener("click", () => renamePlaylist(state.view.id));
    const remove = makeButton(t("deletePlaylist"), "trash");
    remove.addEventListener("click", () => deletePlaylist(state.view.id));
    elements.collectionActions.append(rename, remove);
  }
  // Keep management actions reachable on small screens as well.
  elements.listActions.append(...elements.collectionActions.children);
  if (state.view.type === "queue" && state.items.length) {
    const clear = makeButton(t("clear"), "trash");
    clear.addEventListener("click", () => player.clear());
    elements.listActions.append(clear);
  }
}

function renderList() {
  elements.songList.replaceChildren();
  elements.listStatus.className = "list-status";
  elements.listStatus.hidden = false;
  if (state.loading) {
    elements.listStatus.textContent = state.view.type === "search" ? t("searching") : t("loading");
    for (let index = 0; index < 5; index += 1) {
      const skeleton = document.createElement("div");
      skeleton.className = "skeleton";
      elements.songList.append(skeleton);
    }
    return;
  }
  if (state.error) {
    elements.listStatus.classList.add("error");
    elements.listStatus.textContent = state.error;
    const retry = makeButton(t("retry"), "history", "text-button");
    retry.append(document.createTextNode(t("retry")));
    retry.addEventListener("click", () => state.view.type === "search" ? search(elements.query.value) : loadView(state.view));
    elements.listStatus.append(retry);
    return;
  }
  elements.listStatus.textContent = state.items.length
    ? ""
    : state.view.type === "search" && state.query ? t("searchEmpty") : state.view.type === "search" ? t("searchPrompt") : t("noSongs");
  elements.listStatus.hidden = Boolean(state.items.length);
  const currentKey = player.current ? songKey(itemSong(player.current)) : "";
  state.items.forEach((item, index) => {
    const song = itemSong(item);
    const row = document.createElement("div");
    const isCurrent = state.view.type === "queue" ? index === player.index : songKey(song) === currentKey;
    row.className = `song-row${isCurrent ? " current" : ""}`;
    row.dataset.key = songKey(song);
    row.innerHTML = '<span class="song-index"></span><button class="cover-button" type="button"><span class="cover-fallback"></span><span class="play-overlay"></span></button><button class="icon-button heart-button" type="button"></button><div class="song-title"></div><div class="song-artist"></div><div class="song-source"></div><div class="song-duration"></div><div class="row-actions"></div>';
    row.querySelector(".song-index").textContent = String(index + 1);
    const cover = row.querySelector(".cover-fallback");
    coverContent(cover, song);
    const coverButton = row.querySelector(".cover-button");
    coverButton.title = t("play");
    coverButton.setAttribute("aria-label", `${t("play")}: ${song.name || ""}`);
    coverButton.querySelector(".play-overlay").innerHTML = icon("play");
    coverButton.addEventListener("click", () => state.view.type === "queue" ? player.playIndex(index) : player.playItem(item, state.items));
    const heart = row.querySelector(".heart-button");
    setButtonIcon(heart, "heart", song.is_favorite ? t("unfavorite") : t("favorite"));
    heart.classList.toggle("active", Boolean(song.is_favorite));
    heart.setAttribute("aria-pressed", String(Boolean(song.is_favorite)));
    heart.addEventListener("click", () => toggleFavorite(item));
    row.querySelector(".song-title").textContent = song.name || "-";
    row.querySelector(".song-title").title = song.name || "";
    const subtitle = document.createElement("span");
    subtitle.className = "mobile-song-artist";
    subtitle.textContent = song.artists || "—";
    row.querySelector(".song-title").append(subtitle);
    row.querySelector(".song-artist").textContent = song.artists || "-";
    row.querySelector(".song-source").textContent = song.source || song.player || "-";
    row.querySelector(".song-duration").textContent = song.duration ? formatTime(song.duration / 1000) : "—";
    const add = makeButton(t("addPlaylist"), "add", "icon-button secondary-action");
    add.addEventListener("click", () => showPlaylistPicker(item));
    const more = makeButton(t("more"), "more");
    more.dataset.actionMenuTrigger = "true";
    more.setAttribute("aria-haspopup", "menu");
    more.addEventListener("click", (event) => showActionMenu(event.currentTarget, item, index));
    row.querySelector(".row-actions").append(add, more);
    elements.songList.append(row);
  });
}

async function loadView(view, options = {}) {
  state.view = view;
  state.loading = true;
  state.error = "";
  state.items = view.type === "queue" ? player.queue : [];
  renderNavigation();
  renderHeading();
  renderList();
  if (mobileLayout.matches) setSidebarOpen(false);
  const token = ++state.requestToken;
  try {
    let items = [];
    if (view.type === "favorites") items = await readAll("favorites");
    else if (view.type === "history") items = await readAll("history");
    else if (view.type === "playlist") items = await readAll(`playlists/${view.id}/songs`);
    else if (view.type === "queue") items = player.queue;
    if (token !== state.requestToken) return;
    state.items = items;
    state.loading = false;
    renderHeading();
    renderList();
    if (!options.preserveScroll) elements.catalog.scrollTop = 0;
  } catch (error) {
    if (token !== state.requestToken) return;
    state.loading = false;
    state.error = error?.message || t("loadError");
    renderList();
  }
}

async function search(query) {
  const token = ++state.requestToken;
  state.query = query.trim();
  state.view = { type: "search" };
  state.items = [];
  state.loading = true;
  state.error = "";
  if (mobileLayout.matches) setSidebarOpen(false);
  renderNavigation();
  renderHeading();
  renderList();
  if (!state.query) {
    state.loading = false;
    renderList();
    return;
  }
  try {
    const result = await bridge.apiPost("search", {
      query: state.query,
      platform: state.source,
    });
    if (token !== state.requestToken) return;
    state.items = result.songs.map((song) => ({ song }));
    state.loading = false;
    renderHeading();
    renderList();
    elements.catalog.scrollTop = 0;
  } catch (error) {
    if (token !== state.requestToken) return;
    state.loading = false;
    state.error = error?.message || t("loadError");
    renderList();
  }
}

async function refreshPlaylists() {
  state.playlists = await readAll("playlists");
  renderNavigation();
}

async function toggleFavorite(item) {
  const song = itemSong(item);
  const key = songKey(song);
  if (favoriteRequests.has(key)) return;
  favoriteRequests.add(key);
  const favorite = !song.is_favorite;
  try {
    await bridge.apiPost("favorites", { ...songPayload(item), favorite });
    song.is_favorite = favorite;
    const update = (entry) => {
      if (songKey(itemSong(entry)) === key) itemSong(entry).is_favorite = favorite;
    };
    state.items.forEach(update);
    player.queue.forEach(update);
    player.persist();
    if (state.view.type === "favorites" && !favorite) {
      state.items = state.items.filter((entry) => songKey(itemSong(entry)) !== key);
      renderHeading();
    }
    renderList();
    updatePlayer();
    toast(t("favoriteSaved"));
  } catch (error) {
    toast(error?.message || t("loadError"));
  } finally {
    favoriteRequests.delete(key);
  }
}

function showDialog(title, body, actions = []) {
  elements.dialogBody.dataset.request = String(Number(elements.dialogBody.dataset.request || 0) + 1);
  elements.dialogTitle.textContent = title;
  elements.dialogBody.replaceChildren(body);
  elements.dialogFooter.replaceChildren();
  actions.forEach(({ label, primary, action }) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = primary ? "text-button" : "dialog-text-button";
    button.textContent = label;
    button.addEventListener("click", action);
    elements.dialogFooter.append(button);
  });
  if (!elements.dialog.open) elements.dialog.showModal();
}

function promptPlaylist(title, value, action, submitLabel) {
  const input = document.createElement("input");
  input.className = "dialog-input";
  input.placeholder = t("playlistName");
  input.maxLength = 200;
  input.value = value;
  const submit = async () => {
    const name = input.value.trim();
    if (!name) return input.focus();
    try {
      await action(name);
      elements.dialog.close();
    } catch (error) {
      toast(error?.message || t("loadError"));
    }
  };
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      submit();
    }
  });
  showDialog(title, input, [
    { label: t("cancel"), action: () => elements.dialog.close() },
    { label: submitLabel, primary: true, action: submit },
  ]);
  setTimeout(() => input.select());
}

function createPlaylist(item = null) {
  promptPlaylist(t("createPlaylist"), "", async (name) => {
    const playlist = await bridge.apiPost("playlists", { name });
    if (item) await bridge.apiPost(`playlists/${playlist.id}/songs`, songPayload(item));
    await refreshPlaylists();
    toast(t("playlistCreated"));
    if (!item) await loadView({ type: "playlist", id: playlist.id });
  }, t("create"));
}

function renamePlaylist(id) {
  const playlist = state.playlists.find((candidate) => candidate.id === id);
  if (!playlist) return;
  promptPlaylist(t("renamePlaylist"), playlist.name, async (name) => {
    await bridge.apiPost(`playlists/${id}/rename`, { name });
    await refreshPlaylists();
    renderHeading();
    toast(t("playlistRenamed"));
  }, t("save"));
}

function confirmDialog(title, text, action) {
  const body = document.createElement("p");
  body.textContent = text;
  showDialog(title, body, [
    { label: t("cancel"), action: () => elements.dialog.close() },
    { label: t("confirm"), primary: true, action },
  ]);
}

function deletePlaylist(id) {
  confirmDialog(t("deletePlaylistTitle"), t("deletePlaylistBody"), async () => {
    try {
      await bridge.apiPost(`playlists/${id}/delete`, {});
      elements.dialog.close();
      await refreshPlaylists();
      toast(t("playlistDeleted"));
      await loadView({ type: "favorites" });
    } catch (error) {
      toast(error?.message || t("loadError"));
    }
  });
}

function clearHistory() {
  confirmDialog(t("clearHistoryTitle"), t("clearHistoryBody"), async () => {
    try {
      await bridge.apiPost("history/clear", {});
      elements.dialog.close();
      toast(t("historyCleared"));
      await loadView({ type: "history" });
    } catch (error) {
      toast(error?.message || t("loadError"));
    }
  });
}

function showPlaylistPicker(item) {
  const list = document.createElement("div");
  list.className = "dialog-list";
  state.playlists.forEach((playlist) => {
    const button = document.createElement("button");
    button.type = "button";
    button.innerHTML = `${icon("playlist")}<span></span><small></small>`;
    button.querySelector("span").textContent = playlist.name;
    button.querySelector("small").textContent = playlist.song_count;
    button.addEventListener("click", async () => {
      button.disabled = true;
      try {
        const result = await bridge.apiPost(`playlists/${playlist.id}/songs`, songPayload(item));
        elements.dialog.close();
        await refreshPlaylists();
        toast(result.added ? t("addedPlaylist") : t("alreadyPlaylist"));
      } catch (error) {
        toast(error?.message || t("loadError"));
      } finally {
        button.disabled = false;
      }
    });
    list.append(button);
  });
  if (!state.playlists.length) {
    const empty = document.createElement("p");
    empty.textContent = t("noPlaylists");
    list.append(empty);
  }
  const create = makeButton(t("createPlaylist"), "plus", "text-button");
  create.append(document.createTextNode(t("createPlaylist")));
  create.addEventListener("click", () => createPlaylist(item));
  list.append(create);
  showDialog(t("addPlaylist"), list, [{ label: t("cancel"), action: () => elements.dialog.close() }]);
}

async function showLyrics(item) {
  const song = itemSong(item);
  const loading = document.createElement("div");
  loading.className = "list-status";
  loading.textContent = t("loading");
  showDialog(t("lyrics"), loading, [{ label: t("dismiss"), action: () => elements.dialog.close() }]);
  const request = elements.dialogBody.dataset.request;
  try {
    const result = await bridge.apiPost("lyrics", {
      query: song.name || "",
      artist: song.artists || "",
      platform: song.player || "",
    });
    if (!elements.dialog.open || elements.dialogBody.dataset.request !== request) return;
    const lyrics = document.createElement("pre");
    lyrics.className = "lyrics";
    lyrics.textContent = result.lyrics || t("lyricsMissing");
    elements.dialogBody.replaceChildren(lyrics);
  } catch (error) {
    loading.textContent = error?.message || t("loadError");
  }
}

function showActionMenu(anchor, item, index) {
  const menu = elements.actionMenu;
  menu.replaceChildren();
  const actions = [
    ["next", t("playNext"), () => { player.addNext(item); toast(t("nextQueued")); }],
    ["queue", t("addQueue"), () => { player.append(item); toast(t("queued")); }],
    ["playlist", t("addPlaylist"), () => showPlaylistPicker(item)],
    ["lyrics", t("lyrics"), () => showLyrics(item)],
  ];
  const pageUrl = safeUrl(itemSong(item).page_url);
  // Download stays reserved in icons.js until a unified download API exists.
  if (state.view.type === "history" || state.view.type === "playlist" || state.view.type === "queue") {
    actions.push(["trash", t("remove"), () => removeCurrentItem(item, index)]);
  }
  if ((state.view.type === "playlist" || state.view.type === "queue") && index > 0) {
    actions.push(["up", t("moveUp"), () => moveCurrentItem(index, -1)]);
  }
  if ((state.view.type === "playlist" || state.view.type === "queue") && index < state.items.length - 1) {
    actions.push(["down", t("moveDown"), () => moveCurrentItem(index, 1)]);
  }
  actions.forEach(([iconName, label, action]) => {
    const button = makeButton(label, iconName, "");
    button.setAttribute("role", "menuitem");
    button.append(document.createTextNode(label));
    button.addEventListener("click", () => {
      menu.hidden = true;
      action();
    });
    menu.append(button);
  });
  if (pageUrl) {
    const link = document.createElement("a");
    link.href = pageUrl;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.setAttribute("role", "menuitem");
    link.innerHTML = icon("external");
    link.append(document.createTextNode(t("openSource")));
    menu.append(link);
  }
  const rect = anchor.getBoundingClientRect();
  menu.hidden = false;
  const width = menu.offsetWidth;
  const height = menu.offsetHeight;
  menu.style.left = `${Math.max(8, Math.min(rect.right - width, window.innerWidth - width - 8))}px`;
  menu.style.top = `${Math.max(8, Math.min(rect.bottom + 4, window.innerHeight - height - 8))}px`;
  menu.querySelector("button")?.focus();
}

async function removeCurrentItem(item, index) {
  const view = { ...state.view };
  try {
    if (view.type === "history") await bridge.apiPost(`history/${item.song_id}/delete`, {});
    else if (view.type === "playlist") await bridge.apiPost(`playlists/${view.id}/songs/${item.song_id}/remove`, {});
    else if (view.type === "queue") player.remove(index);
    toast(t("removed"));
    if (view.type !== "queue") {
      await refreshPlaylists();
      if (state.view.type === view.type && state.view.id === view.id) {
        await loadView(view, { preserveScroll: true });
      }
    }
  } catch (error) {
    toast(error?.message || t("loadError"));
  }
}

async function moveCurrentItem(index, direction) {
  if (state.view.type === "queue") {
    player.move(index, direction);
    state.items = player.queue;
    renderList();
    return;
  }
  const view = { ...state.view };
  const target = index + direction;
  [state.items[index], state.items[target]] = [state.items[target], state.items[index]];
  const songIds = state.items.map((item) => item.song_id);
  try {
    await bridge.apiPost(`playlists/${view.id}/order`, { song_ids: songIds });
    if (state.view.type === view.type && state.view.id === view.id) renderList();
  } catch (error) {
    if (state.view.type === view.type && state.view.id === view.id) {
      [state.items[index], state.items[target]] = [state.items[target], state.items[index]];
      renderList();
    }
    toast(error?.message || t("loadError"));
  }
}

function updatePlayer() {
  const item = player.current;
  const song = item ? itemSong(item) : null;
  elements.playerTitle.textContent = song?.name || "Music";
  elements.playerArtist.textContent = song?.artists || "";
  elements.playerStatus.textContent = t({ loading: "resolving", playing: "playing", paused: "paused", ended: "ended", error: "failed", idle: "ready" }[player.status] || "ready");
  coverContent(elements.playerCover, song);
  coverContent(elements.fullPlayerCover, song);
  elements.fullPlayerTitle.textContent = song?.name || "Music";
  elements.fullPlayerArtist.textContent = song?.artists || "";
  setButtonIcon(elements.togglePlay, player.audio.paused ? "play" : "pause", player.audio.paused ? t("play") : t("pause"));
  setButtonIcon(elements.playMode, player.mode, t(player.mode));
  setButtonIcon(elements.mute, player.audio.muted ? "muted" : "volume", player.audio.muted ? t("unmute") : t("mute"));
  elements.volume.value = String(player.audio.volume);
  elements.queueCount.textContent = String(player.queue.length);
  elements.playerFavorite.classList.toggle("active", Boolean(song?.is_favorite));
  elements.playerFavorite.setAttribute("aria-pressed", String(Boolean(song?.is_favorite)));
  elements.playerFavorite.disabled = !item;
  setButtonIcon(elements.playerFavorite, "heart", song?.is_favorite ? t("unfavorite") : t("favorite"));
  elements.playerLyrics.disabled = !item;
  elements.playerCover.disabled = !item;
  elements.fullPlayerFavorite.classList.toggle("active", Boolean(song?.is_favorite));
  elements.fullPlayerFavorite.disabled = !item;
  setButtonIcon(elements.fullPlayerFavorite, "heart", song?.is_favorite ? t("unfavorite") : t("favorite"));
  setButtonIcon(elements.fullPlayerToggle, player.audio.paused ? "play" : "pause", player.audio.paused ? t("play") : t("pause"));
  setButtonIcon(elements.fullPlayerMode, player.mode, t(player.mode));
  renderFullPlayerLyrics(song, player.audio.currentTime || 0);
  if (elements.fullPlayer && !elements.fullPlayer.hidden) loadFullPlayerLyrics(item);
  if (state.view.type === "queue") {
    state.items = player.queue;
    renderHeading();
    renderList();
  }
  document.querySelectorAll(".song-row").forEach((row, index) => {
    row.classList.toggle("current", state.view.type === "queue" ? index === player.index : row.dataset.key === (song ? songKey(song) : ""));
  });
}

function updateTime(current, duration) {
  elements.elapsed.textContent = formatTime(current);
  elements.totalTime.textContent = formatTime(duration);
  elements.progress.value = String(duration ? Math.round((current / duration) * 1000) : 0);
  elements.fullPlayerElapsed.textContent = formatTime(current);
  elements.fullPlayerTotal.textContent = formatTime(duration);
  elements.fullPlayerProgress.value = String(duration ? Math.round((current / duration) * 1000) : 0);
  renderFullPlayerLyrics(player.current ? itemSong(player.current) : null, current);
}

const player = new MusicPlayer(elements.audio, {
  onChange: updatePlayer,
  onTime: updateTime,
  onError: (key) => toast(t(key)),
  onHistory: () => {
    if (state.view.type === "history") loadView(state.view, { preserveScroll: true });
  },
});

function bindEvents() {
  document.querySelectorAll("[data-icon]").forEach((element) => setIcon(element, element.dataset.icon));
  elements.searchForm.addEventListener("submit", (event) => {
    event.preventDefault();
    search(elements.query.value);
  });
  elements.newPlaylist.addEventListener("click", () => createPlaylist());
  elements.dialogClose.addEventListener("click", () => elements.dialog.close());
  elements.mobileMenu.addEventListener("click", () => setSidebarOpen(true));
  elements.collapseSidebar.addEventListener("click", () => setSidebarOpen(false));
  elements.sidebarScrim.addEventListener("click", () => setSidebarOpen(false));
  mobileLayout.addEventListener("change", () => {
    setSidebarOpen(!mobileLayout.matches && !document.body.classList.contains("sidebar-collapsed"));
  });
  setSidebarOpen(!mobileLayout.matches);
  for (const [button, direction] of [[elements.sourcesLeft, -1], [elements.sourcesRight, 1]]) {
    button.addEventListener("click", () => elements.sourceTabs.scrollBy({
      left: direction * Math.max(120, elements.sourceTabs.clientWidth * 0.75),
      behavior: "smooth",
    }));
  }
  elements.sourceTabs.addEventListener("scroll", updateSourceScroll, { passive: true });
  window.addEventListener("resize", updateSourceScroll);
  if (typeof ResizeObserver !== "undefined") new ResizeObserver(updateSourceScroll).observe(elements.sourceTabs);
  elements.togglePlay.addEventListener("click", () => player.toggle());
  elements.previous.addEventListener("click", () => player.previous());
  elements.next.addEventListener("click", () => player.next(true));
  elements.playMode.addEventListener("click", () => player.cycleMode());
  elements.mute.addEventListener("click", () => player.toggleMute());
  elements.volume.addEventListener("input", () => player.setVolume(Number(elements.volume.value)));
  elements.progress.addEventListener("input", () => player.seek(Number(elements.progress.value) / 1000));
  elements.showQueue.addEventListener("click", () => loadView({ type: "queue" }));
  elements.closePlayer.addEventListener("click", () => player.clear());
  elements.playerFavorite.addEventListener("click", () => player.current && toggleFavorite(player.current));
  elements.playerLyrics.addEventListener("click", () => player.current && showLyrics(player.current));
  elements.playerCover.addEventListener("click", () => setFullPlayer(true));
  elements.fullPlayerClose.addEventListener("click", () => setFullPlayer(false));
  elements.fullPlayerQueue.addEventListener("click", () => { setFullPlayer(false); loadView({ type: "queue" }); });
  elements.fullPlayerToggle.addEventListener("click", () => player.toggle());
  elements.fullPlayerPrevious.addEventListener("click", () => player.previous());
  elements.fullPlayerNext.addEventListener("click", () => player.next(true));
  elements.fullPlayerMode.addEventListener("click", () => player.cycleMode());
  elements.fullPlayerFavorite.addEventListener("click", () => player.current && toggleFavorite(player.current));
  elements.fullPlayerProgress.addEventListener("input", () => player.seek(Number(elements.fullPlayerProgress.value) / 1000));
  document.addEventListener("click", (event) => {
    if (!elements.actionMenu.contains(event.target) && !event.target.closest("[data-action-menu-trigger]")) {
      elements.actionMenu.hidden = true;
    }
  });
  window.addEventListener("resize", () => { elements.actionMenu.hidden = true; });
  elements.catalog.addEventListener("scroll", () => { elements.actionMenu.hidden = true; });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      elements.actionMenu.hidden = true;
      if (mobileLayout.matches) setSidebarOpen(false);
    }
  });
  elements.actionMenu.addEventListener("keydown", (event) => {
    if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const entries = [...elements.actionMenu.querySelectorAll("[role=menuitem]")];
    let index = entries.indexOf(document.activeElement);
    if (event.key === "Home") index = 0;
    else if (event.key === "End") index = entries.length - 1;
    else index = (index + (event.key === "ArrowDown" ? 1 : -1) + entries.length) % entries.length;
    entries[index]?.focus();
  });
  setButtonIcon(elements.newPlaylist, "plus", t("createPlaylist"));
  setButtonIcon(elements.mobileMenu, "menu", t("menu"));
  setButtonIcon(elements.previous, "previous", t("previous"));
  setButtonIcon(elements.next, "next", t("next"));
  setButtonIcon(elements.playerLyrics, "lyrics", t("lyrics"));
  setButtonIcon(elements.playerCover, "expand", t("openFullPlayer"));
  setButtonIcon(elements.fullPlayerClose, "down", t("closeFullPlayer"));
  setButtonIcon(elements.fullPlayerQueue, "queue", t("queue"));
  setButtonIcon(elements.fullPlayerPrevious, "previous", t("previous"));
  setButtonIcon(elements.fullPlayerNext, "next", t("next"));
  setButtonIcon(elements.fullPlayerToggle, "play", t("play"));
  setButtonIcon(elements.fullPlayerMode, "sequence", t("sequence"));
  setButtonIcon(elements.fullPlayerFavorite, "heart", t("favorite"));
  setButtonIcon(elements.showQueue, "queue", t("queue"));
  setButtonIcon(elements.closePlayer, "close", t("close"));
  setButtonIcon(elements.dialogClose, "close", t("close"));
}

async function initialize() {
  const context = await bridge.ready();
  locale = context?.locale || "zh-CN";
  applyStaticText();
  bindEvents();
  bridge.onContext((nextContext) => {
    const nextLocale = nextContext?.locale || "zh-CN";
    if (nextLocale !== locale) {
      locale = nextLocale;
      applyStaticText();
      renderSources();
      renderNavigation();
      renderHeading();
      renderList();
      updatePlayer();
    }
  });
  try {
    const [sourceResult] = await Promise.all([
      bridge.apiGet("sources", {}),
      refreshPlaylists(),
    ]);
    state.sources = sourceResult.sources;
    if (state.source && !state.sources.some((source) => source.name === state.source)) state.source = "";
    renderSources();
    player.restore();
    updatePlayer();
    await loadView({ type: "favorites" });
  } catch (error) {
    state.loading = false;
    elements.listStatus.className = "list-status error";
    elements.listStatus.textContent = error?.message || t("loadError");
  }
}

initialize().catch((error) => {
  state.error = error?.message || t("loadError");
  state.loading = false;
  renderList();
});
