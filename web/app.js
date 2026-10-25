/* omadesk viewer — phone-first PWA client */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const landing = $("landing");
  const viewer = $("viewer");
  const video = $("video");
  const audioEl = $("audio");
  const connStatus = $("connStatus");
  const statsEl = $("stats");
  const ime = $("ime");
  const cursor = $("cursor");
  const banner = $("banner");
  const bannerText = $("bannerText");
  const outputSel = $("outputSel");
  const qualitySel = $("qualitySel");
  const deskBar = $("deskBar");
  const winSheet = $("winSheet");
  const wsChips = $("wsChips");
  const winList = $("winList");

  const state = {
    token: "",
    pc: null,
    channel: null,
    connected: false,
    audioSupported: false,
    audioOn: true,
    mode: "click",            // "click" gestures | "scroll" gestures
    mods: { ctrl: false, alt: false, super: false },
    outs: [],
    current: null,
    bitrateMbs: null,
    statsTimer: null,
    ptrs: new Map(),
    lastScrollY: null,
    firstDownT: 0,
    prevStats: null,
    wsList: [],
    wsCurrent: null,
    lastTapT: 0,
    lastTapX: 0,
    lastTapY: 0,
  };

  // ---------- tiny ui helpers ----------
  let bannerTimer = null;
  function bannerShow(text, ms) {
    bannerText.textContent = text;
    banner.classList.remove("hidden");
    clearTimeout(bannerTimer);
    if (ms) bannerTimer = setTimeout(() => banner.classList.add("hidden"), ms);
  }
  function status(text, kind) {
    connStatus.textContent = text;
    connStatus.className = "status " + (kind || "");
  }
  function chSend(obj) {
    if (state.channel && state.channel.readyState === "open")
      state.channel.send(JSON.stringify(obj));
  }
  function api(path, opts) {
    const headers = Object.assign({ "X-Auth": state.token },
      (opts && opts.headers) || {});
    return fetch(path, Object.assign({}, opts, { headers })).then(r => {
      if (r.status === 401) throw new Error("bad token");
      return r;
    });
  }
  const jsonPost = (path, body) => api(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {})
  });

  // ---------- token ----------
  state.token = decodeURIComponent(location.hash.slice(1)).trim();
  if (!state.token) {
    status("no token in URL — open the link/QR shown on the desktop", "err");
    $("connectBtn").disabled = true;
  }

  // ---------- connect ----------
  async function connect() {
    status("connecting…");
    $("connectBtn").disabled = true;
    try {
      const info = await api("/api/info").then(r => r.json());
      state.outs = info.outputs || [];
      state.current = info.current;
      fillOutputs();

      const pc = new RTCPeerConnection({ iceServers: [] });
      state.pc = pc;
      const channel = pc.createDataChannel("omadesk", { ordered: true });
      state.channel = channel;

      channel.onopen = () => {
        chSend({ t: "ready" });
        chSend({ t: "quality", mbps: getCurrentMbps() });
        // audio starts ON by default — tell the server immediately
        chSend({ t: "audio", on: state.audioOn });
        state.connected = true;
        status("connected", "ok");
        landing.classList.add("hidden");
        viewer.classList.remove("hidden");
        startStats();
        keepScreenAwake();
        if (document.documentElement.requestFullscreen && !document.fullscreenElement) {
          document.documentElement.requestFullscreen().catch(() => {});
        }
      };
      channel.onmessage = (e) => {
        let msg; try { msg = JSON.parse(e.data); } catch { return; }
        onServerMessage(msg);
      };
      channel.onclose = () => dispose("disconnected");

      pc.addTransceiver("video", { direction: "recvonly" });
      pc.addTransceiver("audio", { direction: "recvonly" });
      pc.ontrack = (e) => {
        if (e.track.kind === "video") video.srcObject = e.streams[0];
        else if (e.track.kind === "audio") {
          audioEl.srcObject = e.streams[0];
          audioEl.muted = !state.audioOn;
          playAudio();   // autoplay is allowed post-gesture; retry anyway
        }
      };
      pc.onconnectionstatechange = () => {
        if (["failed", "disconnected", "closed"].includes(pc.connectionState)) {
          dispose("connection lost");
        }
      };

      const offer = await pc.createOffer();
      await pc.setLocalDescription(offer);
      await gatherIce(pc);
      const resp = await jsonPost("/api/session", {
        sdp: { type: pc.localDescription.type, sdp: pc.localDescription.sdp },
        output: outputSel.value || null
      });
      if (!resp.ok) throw new Error("server refused offer");
      const j = await resp.json();
      await pc.setRemoteDescription(j.sdp);
      playAudio();
    } catch (err) {
      status("failed: " + err.message, "err");
      dispose(null);
    }
  }

  function gatherIce(pc) {
    return new Promise(res => {
      if (pc.iceGatheringState === "complete") return res();
      const to = setTimeout(res, 2500);
      pc.onicegatheringstatechange = () => {
        if (pc.iceGatheringState === "complete") { clearTimeout(to); res(); }
      };
    });
  }

  function playAudio() {
    try { audioEl.muted = !state.audioOn; audioEl.play().catch(() => {}); } catch {}
  }

  function dispose(reason) {
    clearInterval(state.statsTimer);
    state.statsTimer = null;
    state.prevStats = null;
    statsEl.textContent = "";
    state.connected = false;
    state.ptrs.clear();
    try { audioEl.pause(); audioEl.srcObject = null; } catch {}
    if (state.pc) { try { state.pc.close(); } catch {} state.pc = null; }
    viewer.classList.add("hidden");
    landing.classList.remove("hidden");
    $("connectBtn").disabled = false;
    if (reason) status(reason + " — tap Connect to retry", "err");
    else status("idle");
  }

  function onServerMessage(msg) {
    switch (msg.t) {
      case "outputs":
        state.outs = msg.outs || [];
        state.current = msg.current || state.current;
        fillOutputs();
        if (!!msg.audio === false) $("audioBtn").classList.add("hidden");
        state.audioSupported = !!msg.audio;
        break;
      case "clip":
        if (msg.s && navigator.clipboard) navigator.clipboard.writeText(msg.s).catch(() => {});
        if (msg.s) bannerShow("clipboard synced from PC", 1200);
        break;
      case "wslist": {
        state.wsList = msg.list || [];
        state.wsCurrent = msg.current;
        if (!deskBar.classList.contains("hidden")) renderWsChips();
        break;
      }
      case "winlist": {
        state.winList = msg.list || [];
        state.winCurrent = msg.current;
        if (!winSheet.classList.contains("hidden")) renderWinList();
        break;
      }
      case "error": bannerShow(msg.msg || "server error", 2400); break;
    }
  }

  function fillOutputs() {
    outputSel.innerHTML = "";
    (state.outs || []).forEach(o => {
      const opt = document.createElement("option");
      opt.value = o.name;
      opt.textContent = `${o.name} (${o.width}×${o.height})`;
      outputSel.appendChild(opt);
    });
    if (state.current) outputSel.value = state.current;
  }
  outputSel.onchange = () => chSend({ t: "outputs", name: outputSel.value });
  function getCurrentMbps() { return parseInt(qualitySel.value, 10) || 4; }
  qualitySel.onchange = () => chSend({ t: "quality", mbps: getCurrentMbps() });

  // ---------- stats ----------
  function startStats() {
    clearInterval(state.statsTimer);
    state.statsTimer = setInterval(async () => {
      if (!state.pc) return;
      try {
        const report = await state.pc.getStats();
        let vfps = null, abytes = null, adecoded = 0, bytes = null, ts = null;
        report.forEach(s => {
          if (s.type === "inbound-rtp") {
            if (s.kind === "video") { vfps = s.framesPerSecond || 0; bytes = s.bytesReceived || 0; ts = s.timestamp; }
            if (s.kind === "audio" && state.audioOn) { if (s.bytesReceived) abytes = s.bytesReceived; if (s.jitterBufferDelay) adecoded++; }
          }
        });
        let bps = null;
        if (state.prevStats && ts && ts > state.prevStats.ts && bytes >= state.prevStats.b) {
          bps = (bytes - state.prevStats.b) * 8 / ((ts - state.prevStats.ts) / 1000) / 1e6;
        }
        state.prevStats = { bytes, ts };
        const res = video.videoWidth ? ` ${video.videoWidth}×${video.videoHeight}` : "";
        const aud = state.audioSupported ? (state.audioOn ? " 🔊" : " 🔇") : "";
        statsEl.textContent =
          `${(vfps || 0).toFixed(0)} fps${res}${bps !== null ? " · " + bps.toFixed(1) + " Mb/s" : ""}${aud}`;
      } catch {}
    }, 1000);
  }
  function keepScreenAwake() {
    if (navigator.wakeLock) navigator.wakeLock.request("screen").catch(() => {});
  }

  // ---------- audio ----------
  $("audioBtn").addEventListener("click", () => {
    state.audioOn = !state.audioOn;
    $("audioBtn").classList.toggle("muted", !state.audioOn);
    $("audioBtn").textContent = state.audioOn ? "🔊" : "🔇";
    chSend({ t: "audio", on: state.audioOn });
    playAudio();
  });

  // ---------- gesture mode ----------
  $("modeBtn").addEventListener("click", () => {
    state.mode = state.mode === "click" ? "scroll" : "click";
    $("modeBtn").classList.toggle("scroll", state.mode === "scroll");
    $("modeBtn").textContent = state.mode === "scroll" ? "✋" : "🖱️";
    if (state.ptrs.size) { try { video.releasePointerCapture?.(); } catch {} }
    state.ptrs.clear();
    state._lmbDown && btn("left", false);
    state._lmbDown = false;
    clearTimeout(state._lmbTimer);
    bannerShow(state.mode === "scroll" ? "scroll mode: drag = wheel, tap = click" :
      "mouse mode: tap = click, drag = move+drag", 1600);
  });

  // ---------- desktops bar ----------
  $("deskBtn").addEventListener("click", () => {
    winSheet.classList.add("hidden");
    const opening = deskBar.classList.contains("hidden");
    deskBar.classList.toggle("hidden", !opening);
    if (opening) chSend({ t: "wslist" });
  });
  function renderWsChips() {
    wsChips.innerHTML = "";
    const mk = (label, id, cur, wins) => {
      const b = document.createElement("button");
      b.className = "chip" + (cur ? " current" : "");
      b.textContent = label + (wins ? `·${wins}` : "");
      b.onclick = () => {
        chSend({ t: "ws", id });
        setTimeout(() => chSend({ t: "wslist" }), 420);
      };
      wsChips.appendChild(b);
    };
    (state.wsList || []).forEach(w => mk(String(w.id), w.id, w.id === state.wsCurrent, w.windows));
    if (!state.wsList.length) {
      for (let i = 1; i <= 10; i++) mk(String(i), i, i === state.wsCurrent, 0);
    }
  }
  $("wsNext").onclick = () => { chSend({ t: "wsnext" }); setTimeout(() => chSend({ t: "wslist" }), 420); };
  $("wsPrev").onclick = () => { chSend({ t: "wsprev" }); setTimeout(() => chSend({ t: "wslist" }), 420); };
  $("wsFormer").onclick = () => { chSend({ t: "wsformer" }); setTimeout(() => chSend({ t: "wslist" }), 420); };

  // ---------- windows sheet ----------
  $("winBtn").addEventListener("click", () => {
    deskBar.classList.add("hidden");
    const opening = winSheet.classList.contains("hidden");
    winSheet.classList.toggle("hidden", !opening);
    if (opening) chSend({ t: "winlist" });
  });
  function renderWinList() {
    winList.innerHTML = "";
    const rows = (state.winList || []).slice();
    rows.sort((a, b) => {
      const ca = (a.ws === state.winCurrent) ? -1 : 0;
      const cb = (b.ws === state.winCurrent) ? -1 : 0;
      if (ca !== cb) return ca - cb;
      return String(a.addr).localeCompare(String(b.addr));
    });
    rows.forEach(w => {
      if (!w.title && !w.class) return;
      const row = document.createElement("div");
      row.className = "wrow" + (w.ws === state.winCurrent ? " wcur" : "");
      const app = document.createElement("div"); app.className = "app"; app.textContent = w.class || "app";
      const ttl = document.createElement("div"); ttl.className = "ttl"; ttl.textContent = w.title || w.class;
      const b = document.createElement("div"); b.className = "wsb"; b.textContent = "ws" + w.ws;
      row.append(app, ttl, b);
      row.onclick = () => {
        chSend({ t: "wingo", addr: w.addr });
        winSheet.classList.add("hidden");
      };
      winList.appendChild(row);
    });
    if (!rows.length) {
      const empty = document.createElement("div");
      empty.className = "wrow"; empty.textContent = "(no windows)";
      winList.appendChild(empty);
    }
  }

  // ---------- keyboard mechanics ----------
  document.querySelectorAll("#keyRow .mod").forEach(btn => {
    btn.addEventListener("click", () => {
      const m = btn.dataset.mod;
      state.mods[m] = !state.mods[m];
      btn.classList.toggle("active", state.mods[m]);
    });
  });
  document.querySelectorAll("#keyRow .kbtn[data-key]").forEach(btn => {
    let downSent = false;
    btn.addEventListener("pointerdown", (e) => {
      e.preventDefault();
      downSent = true;
      chSend({ t: "key", c: btn.dataset.key, d: true, m: getMods() });
    });
    const end = () => {
      if (downSent) { downSent = false; chSend({ t: "key", c: btn.dataset.key, d: false, m: {} }); }
    };
    btn.addEventListener("pointerup", end);
    btn.addEventListener("pointercancel", end);
    btn.addEventListener("pointerleave", end);
  });
  document.querySelectorAll("#keyRow .kbtn[data-mouse]").forEach(btn => {
    btn.addEventListener("pointerdown", () => btn(btn.dataset.mouse, true));
    btn.addEventListener("pointerup", () => btn(btn.dataset.mouse, false));
  });
  $("altTabBtn").addEventListener("click", () => {
    chSend({ t: "key", c: "Tab", d: true, m: { alt: true } });
    setTimeout(() => chSend({ t: "key", c: "Tab", d: false, m: {} }), 60);
  });

  function getMods() {
    const m = {};
    for (const k of Object.keys(state.mods)) if (state.mods[k]) m[k === "super" ? "meta" : k] = true;
    return m;
  }
  function btn(b, p) { chSend({ t: "btn", b, p }); }

  // physical keyboard (desktop/preview)
  window.addEventListener("keydown", (e) => {
    if (!state.connected) return;
    if (viewer.classList.contains("hidden")) return;
    if (e.target && (e.target.tagName === "SELECT" || e.target === ime)) return;
    if (e.key === "F5") return;
    e.preventDefault();
    const m = getMods();
    if (e.ctrlKey) m.ctrl = true;
    if (e.altKey) m.alt = true;
    if (e.metaKey) m.meta = true;
    if (e.shiftKey) m.shift = true;
    chSend({ t: "key", c: e.code, d: true, m });
    if (!/^Control(Left|Right)$|^Shift(Left|Right)$|^Alt(Left|Right)$|^Meta(Left|Right)$/.test(e.code)) {
      chSend({ t: "key", c: e.code, d: false, m: {} });
    }
  }, true);

  // IME -> text channel
  function flushIme() {
    const text = ime.value;
    if (!text) return;
    ime.value = "";
    chSend({ t: "text", s: text });
  }
  ime.addEventListener("input", flushIme);
  ime.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      chSend({ t: "key", c: "Enter", d: true, m: getMods() });
      chSend({ t: "key", c: "Enter", d: false, m: {} });
    } else if (e.key === "Backspace") {
      e.preventDefault();
      chSend({ t: "key", c: "Backspace", d: true, m: {} });
      chSend({ t: "key", c: "Backspace", d: false, m: {} });
    }
  });

  $("keyboardBtn").addEventListener("click", () => {
    ime.style.opacity = "1";
    ime.focus({ preventScroll: true });
    setTimeout(() => { ime.style.opacity = "0.01"; }, 80);
  });

  $("clipPasteBtn").addEventListener("click", async () => {
    try {
      const text = await navigator.clipboard.readText();
      if (!text) return bannerShow("phone clipboard empty", 1500);
      chSend({ t: "clipSet", s: text });
      bannerShow("text sent to PC clipboard", 1200);
    } catch {
      bannerShow("clipboard permission denied", 2200);
    }
  });

  $("fullscreenBtn").addEventListener("click", () => {
    if (document.fullscreenElement) document.exitFullscreen?.();
    else document.documentElement.requestFullscreen?.();
  });
  $("backBtn").addEventListener("click", () => dispose("closed by you"));

  // ---------- pointer: touch → mouse / wheel ----------
  function relCoords(e) {
    const r = video.getBoundingClientRect();
    let nx = 0.5, ny = 0.5;
    if (video.videoWidth) {
      const scaleF = Math.min(r.width / video.videoWidth, r.height / video.videoHeight);
      const rw = video.videoWidth * scaleF, rh = video.videoHeight * scaleF;
      const ox = (r.width - rw) / 2, oy = (r.height - rh) / 2;
      nx = (e.clientX - r.left - ox) / rw;
      ny = (e.clientY - r.top - oy) / rh;
      nx = Math.max(0, Math.min(1, nx));
      ny = Math.max(0, Math.min(1, ny));
    }
    return { nx, ny };
  }
  function sendMove(nx, ny) { chSend({ t: "move", x: nx, y: ny }); }
  function showCursor(nx, ny) {
    cursor.classList.remove("hidden");
    const r = video.getBoundingClientRect();
    cursor.style.left = (nx * r.width - 2) + "px";
    cursor.style.top = (ny * r.height - 2) + "px";
  }

  function videoHeightPx() { return video.videoHeight || state.outs[0]?.height || 1050; }

  const lastScroll = { x: 0, y: 0 };
  let scrollAcc = { x: 0, y: 0, t: 0 };

  function pushScroll(dx, dy) {
    // dy in "content px" — positive = drag up = scroll down (natural)
    const outH = video.videoHeight || 1050;
    const scale = 0.9;
    chSend({ t: "scroll", dx: dx * scale, dy: -dy * scale });
  }

  video.addEventListener("pointerdown", (e) => {
    if (!state.connected) return;
    video.setPointerCapture?.(e.pointerId);
    const { nx, ny } = relCoords(e);
    const rec = { nx, ny, moved: 0, t: Date.now() };
    state.ptrs.set(e.pointerId, rec);

    if (state.ptrs.size === 1) {
      state.firstDownT = Date.now();
      sendMove(nx, ny);
      showCursor(nx, ny);
      if (state.mode === "scroll") {
        // no button press; the drag will be the wheel
        rec.isWheel = true;
      } else {
        // grace so a quick two-finger tap doesn't drag-click
        state._lmbTimer = setTimeout(() => {
          state._lmbDown = true;
          state._lmbTimerWent = true;
          btn("left", true);
        }, 70);
      }
    } else if (state.ptrs.size === 2) {
      clearTimeout(state._lmbTimer);
      if (state._lmbDown) { btn("left", false); state._lmbDown = false; }
      state._gesture = "scroll";
      state.lastScrollY = ny;
    }
  }, { passive: true });

  video.addEventListener("pointermove", (e) => {
    if (!state.connected) return;
    const rec = state.ptrs.get(e.pointerId);
    if (!rec) return;
    const { nx, ny } = relCoords(e);
    rec.moved = Math.max(rec.moved, Math.abs(nx - rec.nx) + (Math.abs(ny - rec.ny) * 1000));

    if (state.ptrs.size >= 2 && state._gesture === "scroll") {
      const dy = ny - state.lastScrollY;
      if (Math.abs(dy) > 0.0008) {
        const outH = videoHeightPx();
        chSend({ t: "scroll", dx: 0, dy: -dy * outH * 0.9 });
        state.lastScrollY = ny;
      }
      return;
    }
    if (state.ptrs.size === 1) {
      if (state.mode === "scroll" && rec.isWheel) {
        const dx = nx - rec.nx, dy = ny - rec.ny;
        scrollAcc.x += dx; scrollAcc.y += dy;
        rec.nx = nx; rec.ny = ny;
        const outH = videoHeightPx();
        if (Math.abs(scrollAcc.y) > 0.0015 || Math.abs(scrollAcc.x) > 0.0015) {
          chSend({ t: "scroll", dx: -scrollAcc.x * outH * 0.9, dy: -scrollAcc.y * outH * 0.9 });
          scrollAcc.x = 0; scrollAcc.y = 0;
        }
        return;
      }
      rec.nx = nx; rec.ny = ny;
      sendMove(nx, ny);
      showCursor(nx, ny);
    }
  }, { passive: true });

  video.addEventListener("pointerup", (e) => {
    const rec = state.ptrs.get(e.pointerId);
    state.ptrs.delete(e.pointerId);
    if (!rec) return;
    const dur = Date.now() - rec.t;
    if (state.ptrs.size === 0) {
      if (state._gesture === "scroll") {
        state._gesture = null;
        state._lmbDown = false;
        return;
      }
      if (state._lmbDown) { btn("left", false); state._lmbDown = false; }
      if (!rec.isWheel && dur < 400) {
        // released before the 70 ms press timer — send a clean click
        btn("left", true);
        setTimeout(() => btn("left", false), 25);
      } else if (rec.isWheel && dur < 300 && rec.moved < 0.02) {
        // scroll-mode tap = click at that spot
        btn("left", true);
        setTimeout(() => btn("left", false), 30);
      }

      // double-tap = double click
      if (dur < 260 && rec.moved < 0.02 && rec.isWheel !== true) {
        const now = Date.now();
        const dxT = Math.abs(relTap(e).nx - state.lastTapX);
        const dyT = Math.abs(relTap(e).ny - state.lastTapY);
        if (now - state.lastTapT < 260 && dxT < 0.02 && dyT < 0.02) {
          btn("left", true);
          setTimeout(() => btn("left", false), 25);
          state.lastTapT = 0;
        } else {
          state.lastTapT = now;
          const { nx, ny } = relCoords(e);
          state.lastTapX = nx;
          state.lastTapY = ny;
        }
      }
    } else if (state.ptrs.size === 1 && Date.now() - state.firstDownT < 400) {
      const other = [...state.ptrs.values()][0];
      if (other && other.moved < 0.01 && rec.moved < 0.01) {
        state._gesture = null;
        btn("right", true);
        setTimeout(() => btn("right", false), 40);
      }
    }
  }, { passive: true });

  function relTap(e) { return relCoords(e); }

  video.addEventListener("pointercancel", (e) => {
    state.ptrs.delete(e.pointerId);
    btn("left", false);
    state._gesture = null;
  });

  // desktop mouse support
  video.addEventListener("mousemove", (e) => {
    if (e.pointerType === "touch" || !state.connected) return;
    const { nx, ny } = relCoords(e);
    sendMove(nx, ny);
    showCursor(nx, ny);
  });
  video.addEventListener("mousedown", (e) => {
    if (e.pointerType === "touch") return;
    btn({ 0: "left", 1: "middle", 2: "right" }[e.button] || "left", true);
  });
  video.addEventListener("mouseup", (e) => {
    if (e.pointerType === "touch") return;
    btn({ 0: "left", 1: "middle", 2: "right" }[e.button] || "left", false);
  });
  video.addEventListener("wheel", (e) => {
    if (!state.connected) return;
    e.preventDefault();
    chSend({ t: "scroll", dx: Math.max(-60, Math.min(60, e.deltaX)), dy: Math.max(-60, Math.min(60, e.deltaY)) });
  }, { passive: false });
  video.addEventListener("contextmenu", (e) => e.preventDefault());

  $("connectBtn").addEventListener("click", connect);
})();
