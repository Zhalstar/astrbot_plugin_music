import { bridge, songKey, songPayload, safeUrl, storage } from "./api.js";

const STORAGE_KEY = "astrbot.music.player.v1";
const MODES = ["sequence", "shuffle", "repeat"];

function publicItem(item) {
  const song = { ...(item.song || item) };
  delete song.audio_url;
  delete song.path;
  delete song.lyrics;
  delete song.comments;
  return {
    ...(item.song_id ? { song_id: item.song_id } : {}),
    song,
  };
}

export class MusicPlayer {
  constructor(audio, callbacks = {}) {
    this.audio = audio;
    this.callbacks = callbacks;
    this.queue = [];
    this.index = -1;
    this.mode = "sequence";
    this.shuffleBag = [];
    this.shuffleHistory = [];
    this.loadToken = 0;
    this.reportedToken = -1;
    this.failedIndexes = new Set();
    this.status = "idle";
    this.readyToken = -1;
    this.failedToken = -1;
    this.lastPersistTime = 0;
    this.audio.volume = 0.8;

    audio.addEventListener("playing", () => this.handlePlaying());
    audio.addEventListener("pause", () => {
      if (this.status === "playing") this.status = "paused";
      this.persist();
      this.emit();
    });
    audio.addEventListener("timeupdate", () => {
      this.callbacks.onTime?.(audio.currentTime || 0, audio.duration || 0);
      if (Date.now() - this.lastPersistTime > 5000) this.persist();
    });
    audio.addEventListener("durationchange", () => {
      this.callbacks.onTime?.(audio.currentTime || 0, audio.duration || 0);
    });
    audio.addEventListener("ended", () => this.handleEnded());
    audio.addEventListener("error", () => {
      if (audio.src && this.readyToken === this.loadToken) {
        this.skipFailed("playbackFailed", this.readyToken);
      }
    });
  }

  restore() {
    try {
      const saved = JSON.parse(storage.get(STORAGE_KEY) || "null");
      if (!saved || !Array.isArray(saved.queue)) return;
      this.queue = saved.queue.filter((item) => item?.song?.id && item.song.player).map(publicItem);
      this.index = Math.min(Math.max(Math.trunc(Number(saved.index) || 0), 0), this.queue.length - 1);
      this.mode = MODES.includes(saved.mode) ? saved.mode : "sequence";
      this.audio.volume = Number.isFinite(saved.volume) ? Math.min(Math.max(saved.volume, 0), 1) : 0.8;
      this.audio.muted = Boolean(saved.muted);
      this.resumePosition = Math.max(Number(saved.position) || 0, 0);
      this.status = this.queue.length ? "paused" : "idle";
      this.callbacks.onTime?.(this.resumePosition, (this.current?.song.duration || 0) / 1000);
      this.emit();
    } catch {
      storage.set(STORAGE_KEY, null);
    }
  }

  persist() {
    this.lastPersistTime = Date.now();
    storage.set(
      STORAGE_KEY,
      JSON.stringify({
        queue: this.queue.map(publicItem),
        index: this.index,
        position: this.audio.currentTime || this.resumePosition || 0,
        mode: this.mode,
        volume: this.audio.volume,
        muted: this.audio.muted,
      }),
    );
  }

  get current() {
    return this.queue[this.index] || null;
  }

  setQueue(items, startIndex = 0, autoplay = true) {
    this.queue = items.map(publicItem);
    this.index = Math.min(Math.max(startIndex, 0), this.queue.length - 1);
    this.shuffleBag = [];
    this.shuffleHistory = [];
    this.failedIndexes.clear();
    this.resumePosition = 0;
    if (this.index >= 0) return this.loadCurrent(autoplay);
    else this.clear();
  }

  async loadCurrent(autoplay = true) {
    const item = this.current;
    if (!item) return;
    const token = ++this.loadToken;
    this.readyToken = -1;
    this.reportedToken = -1;
    this.status = "loading";
    this.audio.pause();
    this.audio.removeAttribute("src");
    this.audio.load();
    this.callbacks.onTime?.(this.resumePosition || 0, (item.song.duration || 0) / 1000);
    this.persist();
    this.emit();

    try {
      const result = await bridge.apiPost("play", songPayload(item));
      if (token !== this.loadToken) return;
      const url = safeUrl(result.audio_url);
      if (!url) throw new Error("Invalid audio URL");
      item.song = { ...item.song, ...result.song, audio_url: undefined };
      if (this.resumePosition) {
        const position = this.resumePosition;
        this.resumePosition = 0;
        this.audio.addEventListener(
          "loadedmetadata",
          () => {
            if (token === this.loadToken) {
              this.audio.currentTime = Math.min(position, this.audio.duration || position);
            }
          },
          { once: true },
        );
      }
      this.readyToken = token;
      this.audio.src = url;
      this.audio.load();
      this.status = autoplay ? "loading" : "paused";
      this.persist();
      this.emit();
      if (autoplay) {
        try {
          await this.audio.play();
        } catch (error) {
          if (token !== this.loadToken) return;
          if (error?.name === "NotAllowedError") {
            this.status = "paused";
            this.callbacks.onError?.("autoplayBlocked");
            this.emit();
          } else {
            await this.skipFailed("playbackFailed", token);
          }
        }
      }
    } catch (error) {
      if (token === this.loadToken) {
        await this.skipFailed("playbackFailed", token);
      }
    }
  }

  async toggle() {
    if (!this.current) return;
    if (this.status === "loading") return;
    this.failedIndexes.clear();
    if (!this.audio.src) {
      await this.loadCurrent(true);
    } else if (this.audio.paused) {
      if (this.audio.ended) {
        this.reportedToken = -1;
        this.audio.currentTime = 0;
      }
      try {
        await this.audio.play();
      } catch (error) {
        this.callbacks.onError?.("playbackFailed");
      }
    } else {
      this.audio.pause();
      this.status = "paused";
      this.emit();
    }
  }

  async handlePlaying() {
    if (!this.current || this.readyToken !== this.loadToken) return;
    this.status = "playing";
    this.failedIndexes.clear();
    this.emit();
    if (this.reportedToken === this.loadToken || !this.current) return;
    this.reportedToken = this.loadToken;
    const item = this.current;
    try {
      // Use resolved metadata so refreshed source identifiers survive replay.
      const result = await bridge.apiPost("history", { song: item.song });
      item.song_id = result.song_id;
      this.persist();
      this.callbacks.onHistory?.(item);
    } catch (error) {
      this.callbacks.onError?.("historyFailed");
    }
  }

  handleEnded() {
    if (!this.current || this.readyToken !== this.loadToken) return;
    if (this.mode === "repeat") {
      this.reportedToken = -1;
      this.audio.currentTime = 0;
      const token = this.loadToken;
      this.audio.play().catch(() => this.skipFailed("playbackFailed", token));
      return;
    }
    this.next(false);
  }

  async next(manual = true) {
    if (!this.queue.length) return;
    this.resumePosition = 0;
    if (manual) this.failedIndexes.clear();
    let nextIndex;
    if (this.mode === "shuffle") {
      if (!this.shuffleBag.length) {
        this.shuffleBag = this.queue
          .map((_, index) => index)
          .filter((index) => index !== this.index);
        for (let end = this.shuffleBag.length - 1; end > 0; end -= 1) {
          const swap = Math.floor(Math.random() * (end + 1));
          [this.shuffleBag[end], this.shuffleBag[swap]] = [this.shuffleBag[swap], this.shuffleBag[end]];
        }
      }
      nextIndex = this.shuffleBag.pop();
      if (nextIndex === undefined) nextIndex = this.index;
      this.shuffleHistory.push(this.index);
    } else {
      nextIndex = this.index + 1;
      if (nextIndex >= this.queue.length) {
        if (!manual) {
          this.status = "ended";
          this.audio.pause();
          this.emit();
        }
        if (!manual) return;
        nextIndex = 0;
      }
    }
    this.index = nextIndex;
    await this.loadCurrent(true);
  }

  async previous() {
    if (!this.queue.length) return;
    if (this.audio.currentTime > 5) {
      this.audio.currentTime = 0;
      return;
    }
    if (this.mode === "shuffle" && this.shuffleHistory.length) {
      this.shuffleBag.push(this.index);
      this.index = this.shuffleHistory.pop();
    } else if (this.index > 0) {
      this.index -= 1;
    } else {
      this.audio.currentTime = 0;
      return;
    }
    this.resumePosition = 0;
    this.failedIndexes.clear();
    await this.loadCurrent(true);
  }

  playIndex(index) {
    if (index < 0 || index >= this.queue.length) return;
    if (this.mode === "shuffle" && this.index >= 0 && index !== this.index) {
      this.shuffleHistory.push(this.index);
    }
    this.index = index;
    this.failedIndexes.clear();
    this.resumePosition = 0;
    return this.loadCurrent(true);
  }

  playItem(item, context = []) {
    const key = songKey(item.song || item);
    const queue = context.length ? context : [item];
    const index = Math.max(
      queue.findIndex((candidate) => songKey(candidate.song || candidate) === key),
      0,
    );
    return this.setQueue(queue, index, true);
  }

  addNext(item) {
    const insertion = this.index < 0 ? 0 : this.index + 1;
    this.queue.splice(insertion, 0, publicItem(item));
    if (this.index < 0) this.index = 0;
    this.shuffleBag = [];
    this.shuffleHistory = [];
    this.persist();
    this.emit();
  }

  append(item) {
    this.queue.push(publicItem(item));
    if (this.index < 0) this.index = 0;
    this.shuffleBag = [];
    this.shuffleHistory = [];
    this.persist();
    this.emit();
  }

  remove(index) {
    if (index < 0 || index >= this.queue.length) return;
    const removingCurrent = index === this.index;
    this.queue.splice(index, 1);
    if (!this.queue.length) {
      this.clear();
      return;
    }
    if (index < this.index) this.index -= 1;
    else if (this.index >= this.queue.length) this.index = this.queue.length - 1;
    this.persist();
    this.shuffleBag = [];
    this.shuffleHistory = [];
    this.failedIndexes.clear();
    if (removingCurrent) {
      this.resumePosition = 0;
      this.loadCurrent(!this.audio.paused);
    }
    else this.emit();
  }

  move(index, direction) {
    const target = index + direction;
    if (index < 0 || target < 0 || target >= this.queue.length) return;
    [this.queue[index], this.queue[target]] = [this.queue[target], this.queue[index]];
    if (this.index === index) this.index = target;
    else if (this.index === target) this.index = index;
    this.shuffleBag = [];
    this.shuffleHistory = [];
    this.failedIndexes.clear();
    this.persist();
    this.emit();
  }

  cycleMode() {
    this.mode = MODES[(MODES.indexOf(this.mode) + 1) % MODES.length];
    this.shuffleBag = [];
    this.persist();
    this.emit();
  }

  seek(ratio) {
    if (Number.isFinite(this.audio.duration)) {
      this.audio.currentTime = this.audio.duration * Math.min(Math.max(ratio, 0), 1);
    }
  }

  setVolume(value) {
    this.audio.volume = Math.min(Math.max(value, 0), 1);
    if (value > 0) this.audio.muted = false;
    this.persist();
    this.emit();
  }

  toggleMute() {
    this.audio.muted = !this.audio.muted;
    this.persist();
    this.emit();
  }

  async skipFailed(message, token = this.loadToken) {
    if (!this.current || token !== this.loadToken || this.failedToken === token) return;
    this.failedToken = token;
    this.failedIndexes.add(this.index);
    this.callbacks.onError?.(message);
    if (this.failedIndexes.size >= this.queue.length) {
      this.status = "error";
      this.audio.removeAttribute("src");
      this.audio.load();
      this.persist();
      this.emit();
      return;
    }
    let nextIndex = this.index;
    do {
      nextIndex = (nextIndex + 1) % this.queue.length;
    } while (this.failedIndexes.has(nextIndex));
    this.index = nextIndex;
    this.resumePosition = 0;
    await this.loadCurrent(true);
  }

  clear() {
    this.loadToken += 1;
    this.readyToken = -1;
    this.audio.pause();
    this.audio.removeAttribute("src");
    this.audio.load();
    this.queue = [];
    this.index = -1;
    this.status = "idle";
    this.resumePosition = 0;
    this.shuffleBag = [];
    this.shuffleHistory = [];
    this.failedIndexes.clear();
    this.persist();
    this.callbacks.onTime?.(0, 0);
    this.emit();
  }

  emit() {
    this.callbacks.onChange?.(this);
  }
}
