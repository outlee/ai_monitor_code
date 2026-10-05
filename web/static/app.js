(() => {
  const $ = (sel) => document.querySelector(sel);
  let timer = null;
  let suppressRefresh = false;
  let editMode = null;
  let channelCache = {};
  const REFRESH_MS = 8000;
  // 本地提醒：记录已见事件，避免刷新时重复吵
  let seenEventKeys = new Set();
  let alertsPrimed = false; // 首次加载只建基线，不提醒
  const LS_SOUND = "ai_monitor_sound";
  const LS_DESKTOP = "ai_monitor_desktop";
  const LS_TTS = "ai_monitor_tts";
  const LS_VIEW = "ai_monitor_view";

  // TTS + 抑制（借鉴 igmp_monitor）
  const SUPPRESSION_MS = 5 * 60 * 1000;
  const AGGREGATION_N = 5;
  const AGGREGATION_MS = 10 * 1000;
  const suppressionMap = new Map();
  let pendingTts = [];
  let aggregationTimer = null;

  const TYPE_LABELS = {
    black: "黑场",
    freeze: "静帧",
    silence: "无伴音",
    stream_down: "断流",
    no_signal: "无信号",
    no_signal_end: "信号恢复",
    black_end: "黑场恢复",
    freeze_end: "静帧恢复",
    silence_end: "伴音恢复",
    ai_mosaic: "花屏/马赛克",
    ai_green_screen: "绿屏花屏",
    ai_anomaly: "画面异常",
  };

  let dashCategories = [];
  let lastDash = null;
  let evOffset = 0;
  const EV_PAGE = 20;
  let evTotal = 0;
  let orderDirty = false;
  let lastOrderIds = [];
  let lastDashCardIds = [];
  let dragSrc = null;

  function typeLabel(t) {
    if (!t) return "异常";
    if (TYPE_LABELS[t]) return TYPE_LABELS[t];
    const s = String(t);
    if (s.startsWith("ai_")) {
      if (s.indexOf("mosaic") >= 0) return "花屏/马赛克";
      if (s.indexOf("green") >= 0) return "绿屏花屏";
      return "AI画面异常";
    }
    return t;
  }

  function formatAlarmTags(list, lastType) {
    // 只显示当前仍活动的告警；不要用历史上最后一次「断流」冒充当前状态
    const raw = (list && list.length ? list : []).filter(Boolean);
    if (!raw.length) return "正常";
    return raw.map(typeLabel).join("、");
  }

  function relativeTime(timeStr) {
    if (!timeStr) return "";
    // expect YYYY-MM-DD HH:MM:SS
    const m = String(timeStr).match(
      /(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2}):(\d{2})/
    );
    if (!m) return timeStr;
    const dt = new Date(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +m[6]);
    const sec = Math.floor((Date.now() - dt.getTime()) / 1000);
    if (sec < 0) return timeStr;
    if (sec < 60) return sec + "秒前";
    if (sec < 3600) return Math.floor(sec / 60) + "分钟前";
    if (sec < 86400) return Math.floor(sec / 3600) + "小时前";
    if (sec < 86400 * 7) return Math.floor(sec / 86400) + "天前";
    return timeStr;
  }

  function loadAlertPrefs() {
    const s = localStorage.getItem(LS_SOUND);
    const d = localStorage.getItem(LS_DESKTOP);
    const t = localStorage.getItem(LS_TTS);
    if (s !== null) $("#sw-sound").checked = s === "1";
    if (d !== null) $("#sw-desktop").checked = d === "1";
    if (t !== null && $("#sw-tts")) $("#sw-tts").checked = t === "1";
    const v = localStorage.getItem(LS_VIEW) || "dash";
    setView(v, false);
  }

  function saveAlertPrefs() {
    localStorage.setItem(LS_SOUND, $("#sw-sound").checked ? "1" : "0");
    localStorage.setItem(LS_DESKTOP, $("#sw-desktop").checked ? "1" : "0");
    if ($("#sw-tts")) localStorage.setItem(LS_TTS, $("#sw-tts").checked ? "1" : "0");
  }

  function setView(name, save) {
    const dash = $("#view-dash");
    const manage = $("#view-manage");
    if (!dash || !manage) return;
    if (name === "manage") {
      dash.classList.add("hidden");
      manage.classList.remove("hidden");
      $("#btn-view-manage") && $("#btn-view-manage").classList.add("active");
      $("#btn-view-dash") && $("#btn-view-dash").classList.remove("active");
      refreshPerf();
      refreshStorageDetail();
    } else {
      manage.classList.add("hidden");
      dash.classList.remove("hidden");
      $("#btn-view-dash") && $("#btn-view-dash").classList.add("active");
      $("#btn-view-manage") && $("#btn-view-manage").classList.remove("active");
      name = "dash";
    }
    if (save !== false) localStorage.setItem(LS_VIEW, name);
  }

  function shouldSuppress(channelId, type) {
    const key = channelId + ":" + type;
    const exp = suppressionMap.get(key);
    if (exp && Date.now() < exp) return true;
    suppressionMap.set(key, Date.now() + SUPPRESSION_MS);
    return false;
  }

  function speak(text) {
    if (!$("#sw-tts") || !$("#sw-tts").checked) return;
    if (!window.speechSynthesis) return;
    try {
      window.speechSynthesis.cancel();
      const u = new SpeechSynthesisUtterance(text);
      u.lang = "zh-CN";
      u.rate = 1.1;
      u.volume = 1.0;
      window.speechSynthesis.speak(u);
    } catch (e) {
      console.warn("tts failed", e);
    }
  }

  function flushTtsQueue() {
    if (aggregationTimer) {
      clearTimeout(aggregationTimer);
      aggregationTimer = null;
    }
    const list = pendingTts.slice();
    pendingTts = [];
    if (!list.length) return;
    if (list.length >= AGGREGATION_N) {
      speak("警告：" + list.length + "路节目同时异常，请立即检查");
      return;
    }
    for (const a of list.slice(0, 3)) {
      const name = a.channel_name || a.channel_id || "频道";
      speak(name + "发生" + typeLabel(a.type) + "告警");
    }
  }

  function enqueueTts(alarms) {
    if (!$("#sw-tts") || !$("#sw-tts").checked) return;
    for (const a of alarms) {
      if (!a.type || String(a.type).endsWith("_end")) continue;
      if (shouldSuppress(a.channel_id || "", a.type || "")) continue;
      pendingTts.push(a);
    }
    if (!pendingTts.length) return;
    if (pendingTts.length >= AGGREGATION_N) {
      flushTtsQueue();
      return;
    }
    if (aggregationTimer) clearTimeout(aggregationTimer);
    aggregationTimer = setTimeout(flushTtsQueue, AGGREGATION_MS);
  }

  function eventKey(ev) {
    return [ev.time || "", ev.type || "", ev.channel_id || "", ev.message || ev.msg || ""].join("|");
  }

  function playBeep() {
    // 连续多声，约 2 秒，比单次「嘟」更明显
    try {
      const Ctx = window.AudioContext || window.webkitAudioContext;
      if (!Ctx) return;
      const ctx = new Ctx();
      const seq = [880, 660, 880, 660, 988];
      let t = ctx.currentTime;
      seq.forEach((freq) => {
        const o = ctx.createOscillator();
        const g = ctx.createGain();
        o.type = "square";
        o.frequency.value = freq;
        g.gain.setValueAtTime(0.0001, t);
        g.gain.exponentialRampToValueAtTime(0.12, t + 0.02);
        g.gain.exponentialRampToValueAtTime(0.0001, t + 0.28);
        o.connect(g);
        g.connect(ctx.destination);
        o.start(t);
        o.stop(t + 0.3);
        t += 0.35;
      });
      setTimeout(() => {
        try {
          ctx.close();
        } catch (e) {}
      }, 2200);
    } catch (e) {
      console.warn("beep failed", e);
    }
  }

  async function ensureNotifyPermission() {
    if (!("Notification" in window)) return false;
    if (Notification.permission === "granted") return true;
    if (Notification.permission === "denied") return false;
    const p = await Notification.requestPermission();
    return p === "granted";
  }

  function desktopNotify(title, body) {
    if (!("Notification" in window)) return;
    if (Notification.permission !== "granted") return;
    try {
      const n = new Notification(title, {
        body: body,
        tag: "ai-monitor-alarm",
        renotify: true,
      });
      setTimeout(() => n.close(), 8000);
    } catch (e) {
      console.warn("notification failed", e);
    }
  }

  function handleNewEvents(events) {
    if (!Array.isArray(events)) return;
    const fresh = [];
    for (const ev of events) {
      const k = eventKey(ev);
      if (!seenEventKeys.has(k)) {
        seenEventKeys.add(k);
        fresh.push(ev);
      }
    }
    // 限制 Set 体积
    if (seenEventKeys.size > 500) {
      seenEventKeys = new Set(Array.from(seenEventKeys).slice(-300));
    }
    if (!alertsPrimed) {
      alertsPrimed = true;
      return;
    }
    if (!fresh.length) return;

    // 只对「开始」类异常提醒；恢复(*_end)不响，避免静帧/恢复来回刷
    const alarms = fresh.filter((e) => {
      if (!e.type) return false;
      if (e.phase === "end") return false;
      if (String(e.type).endsWith("_end")) return false;
      return true;
    });
    if (!alarms.length) return;

    if ($("#sw-sound") && $("#sw-sound").checked) playBeep();
    enqueueTts(alarms);

    if ($("#sw-desktop") && $("#sw-desktop").checked) {
      ensureNotifyPermission().then((ok) => {
        if (!ok) return;
        const first = alarms[0];
        const title =
          alarms.length === 1
            ? "节目异常: " + (first.type || "")
            : "节目异常 × " + alarms.length;
        const body = alarms
          .slice(0, 3)
          .map((e) => {
            const ch = e.channel_name || e.channel_id || "";
            return (ch ? ch + " " : "") + (e.type || "") + " " + (e.time || "");
          })
          .join("\n");
        desktopNotify(title, body);
      });
    }

    // 标题闪烁提示
    const base = "AI 节目监测";
    let blink = 0;
    const it = setInterval(() => {
      document.title = blink % 2 === 0 ? "【异常】" + base : base;
      blink++;
      if (blink > 8) {
        clearInterval(it);
        document.title = base;
      }
    }, 500);

    toast(
      "新异常 " +
        alarms.length +
        " 条: " +
        (alarms[0].type || "") +
        (alarms[0].channel_name ? " · " + alarms[0].channel_name : ""),
      "err"
    );
  }

  function typeClass(t) {
    if (!t) return "";
    if (t.startsWith("ai_")) return "ai_mosaic";
    return t;
  }

  function statusText(status) {
    const map = {
      ok: "正常",
      alarm: "异常",
      disabled: "禁用",
      unknown: "未知",
      offline: "离线",
      stale: "心跳超时",
      reconnecting: "重连中",
      no_signal: "无信号",
      starting: "探测中",
    };
    return map[status] || "未知";
  }

  function statusBadge(status) {
    const map = {
      ok: ["正常", "ok"],
      alarm: ["异常", "alarm"],
      disabled: ["禁用", "disabled"],
      unknown: ["未知", "unknown"],
      offline: ["离线", "offline"],
      stale: ["心跳超时", "stale"],
      reconnecting: ["重连中", "reconnecting"],
      no_signal: ["无信号", "reconnecting"],
      starting: ["探测中", "reconnecting"],
    };
    const [text, cls] = map[status] || map.unknown;
    return `<span class="badge ${cls}">${text}</span>`;
  }

  function escapeHtml(s) {
    return String(s ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function toast(msg, kind) {
    const el = $("#toast");
    el.textContent = msg;
    el.className = "toast " + (kind || "ok");
    clearTimeout(el._t);
    el._t = setTimeout(() => el.classList.add("hidden"), 3200);
  }

  async function fetchJSON(url, timeoutMs) {
    const ms = timeoutMs || 15000;
    const ctrl = typeof AbortController !== "undefined" ? new AbortController() : null;
    const timer = ctrl ? setTimeout(() => ctrl.abort(), ms) : null;
    try {
      const r = await fetch(url, ctrl ? { signal: ctrl.signal } : undefined);
      if (!r.ok) throw new Error(url + " " + r.status);
      return await r.json();
    } catch (e) {
      if (e && e.name === "AbortError") throw new Error("请求超时: " + url);
      throw e;
    } finally {
      if (timer) clearTimeout(timer);
    }
  }

  async function postJSON(url, body) {
    const r = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) {
      const detail = data.detail;
      const msg =
        typeof detail === "string"
          ? detail
          : Array.isArray(detail)
            ? detail.map((x) => x.msg || JSON.stringify(x)).join("; ")
            : data.message || r.statusText;
      throw new Error(msg);
    }
    return data;
  }

  async function delJSON(url) {
    const r = await fetch(url, { method: "DELETE" });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.detail || data.message || r.statusText);
    return data;
  }

  function fillControls(data) {
    const ai = data.ai || {};
    const d = data.defaults || {};
    $("#sw-ai-enabled").checked = !!ai.enabled;
    $("#sel-ai-mode").value = ai.mode || "auto";
    $("#inp-ai-interval").value = ai.interval_sec ?? 2;
    if ($("#inp-ai-threshold")) $("#inp-ai-threshold").value = ai.threshold ?? 0.55;
    if ($("#inp-green-th")) $("#inp-green-th").value = ai.green_ratio_th ?? 0.35;
    if ($("#inp-block-th")) $("#inp-block-th").value = ai.block_score_th ?? 0.12;
    $("#sw-save-snapshot").checked = d.save_snapshot !== false;
    $("#inp-black").value = d.black_duration ?? 2;
    $("#inp-freeze").value = d.freeze_duration ?? 12;
    const mode = d.freeze_mode === "video_silence" ? "video_silence" : "video";
    document.querySelectorAll('input[name="freeze-mode"]').forEach((el) => {
      el.checked = el.value === mode;
    });
    syncFreezeHint();
    $("#inp-silence").value = d.silence_duration ?? 3;
    if ($("#inp-silence-db")) $("#inp-silence-db").value = d.silence_threshold ?? -40;
  }

  async function loadNicOptions(selected) {
    const sel = $("#ch-iface");
    if (!sel) return;
    const cur = selected || "";
    try {
      const data = await fetchJSON("/api/system/nics");
      const nics = data.nics || [];
      sel.innerHTML = `<option value="">不指定（FFmpeg 直拉）</option>`;
      nics.forEach((n) => {
        const opt = document.createElement("option");
        opt.value = n.name;
        opt.textContent = n.label || n.name;
        if (n.name === cur) opt.selected = true;
        sel.appendChild(opt);
      });
      // 若当前配置的网卡不在列表中，仍保留选项
      if (cur && ![...sel.options].some((o) => o.value === cur)) {
        const opt = document.createElement("option");
        opt.value = cur;
        opt.textContent = cur + "（当前配置）";
        opt.selected = true;
        sel.appendChild(opt);
      }
    } catch (e) {
      sel.innerHTML = `<option value="">不指定</option>`;
      if (cur) {
        const opt = document.createElement("option");
        opt.value = cur;
        opt.textContent = cur;
        opt.selected = true;
        sel.appendChild(opt);
      }
    }
  }

  async function openChannelModal(mode, ch) {
    editMode = mode;
    $("#ch-modal-title").textContent = mode === "create" ? "新增频道" : "编辑频道";
    const idEl = $("#ch-id");
    let ifaceVal = "";
    if (mode === "create") {
      idEl.value = "";
      idEl.disabled = false;
      $("#ch-name").value = "";
      fillCategoryTags([]);
      $("#ch-url").value = "udp://@239.1.1.1:5000";
      $("#ch-program").value = "";
      $("#ch-enabled").checked = true;
      ifaceVal = "";
    } else {
      idEl.value = ch.id;
      idEl.disabled = true;
      $("#ch-name").value = ch.name || "";
      fillCategoryTags(ch.category);
      $("#ch-url").value = ch.url || "";
      $("#ch-program").value =
        ch.program !== undefined && ch.program !== null && ch.program !== ""
          ? ch.program
          : "";
      $("#ch-enabled").checked = ch.enabled !== false;
      ifaceVal = ch.iface || "";
    }
    await loadNicOptions(ifaceVal);
    $("#ch-modal").classList.remove("hidden");
  }

  function closeChannelModal() {
    $("#ch-modal").classList.add("hidden");
    editMode = null;
  }

  function openImportModal() {
    $("#import-text").value = "";
    $("#import-mode").value = "merge";
    $("#import-modal").classList.remove("hidden");
  }

  function closeImportModal() {
    $("#import-modal").classList.add("hidden");
  }

  function fmtBitrate(kbps) {
    if (kbps == null || kbps === "" || Number.isNaN(Number(kbps))) return "";
    const v = Number(kbps);
    if (v >= 1000) return (v / 1000).toFixed(1) + " Mbps";
    if (v > 0) return Math.round(v) + " kbps";
    return "0";
  }

  function streamLabel(c) {
    const parts = [];
    const br = fmtBitrate(c.bitrate_kbps);
    if (br) parts.push(br);
    if (c.pkt_rate != null && c.pkt_rate !== "") {
      parts.push(Number(c.pkt_rate).toFixed(0) + " pkt/s");
    }
    return parts.join(" · ");
  }

  function channelCardHtml(c, large) {
    const alarms = formatAlarmTags(c.active_alarms, c.last_type);
    const prog =
      c.program !== undefined && c.program !== null && c.program !== ""
        ? "P" + c.program
        : "";
    const stream = streamLabel(c);
    const thumb = c.thumb_url
      ? `<img class="ch-thumb" src="${escapeHtml(c.thumb_url)}" loading="lazy" alt="" />`
      : `<div class="ch-thumb placeholder">暂无画面</div>`;
    const tags = [];
    if (c.node_name) tags.push(c.node_name);
    cardTags(c).forEach((t) => tags.push(t));
    const tagHtml = tags.length
      ? `<div class="ch-tags">${escapeHtml(tags.join(" · "))}</div>`
      : "";
    const cls = large ? "ch-card ch-card-lg" : "ch-card";
    const snap = c.preview_base || ("/api/snapshots/" + (c.channel_id || c.id));
    return `<div class="${cls} lamp-${escapeHtml(c.lamp || "gray")}" data-id="${escapeHtml(
      c.id
    )}" data-snap="${escapeHtml(snap)}" title="${escapeHtml(c.name || c.id)}">
      ${tagHtml}
      ${thumb}
      <div class="ch-body">
        <div class="ch-name">${escapeHtml(c.name || c.id)}</div>
        <div class="ch-id">${escapeHtml(c.channel_id || c.id)}${prog ? " · " + escapeHtml(prog) : ""}</div>
        <div class="ch-rate">${stream ? escapeHtml(stream) : "码流 —"}</div>
        <div class="ch-meta">${escapeHtml(statusText(c.status))} · ${escapeHtml(alarms)}</div>
      </div>
    </div>`;
  }

  function bindChannelCardClicks(root) {
    if (!root) return;
    root.querySelectorAll(".ch-card").forEach((el) => {
      el.addEventListener("click", () => {
        if (el.dataset.justDragged === "1") {
          el.dataset.justDragged = "";
          return;
        }
        const id = el.dataset.id;
        const name =
          (el.querySelector(".ch-name") && el.querySelector(".ch-name").textContent) ||
          id;
        openPreview(id, name, el.dataset.snap || "");
      });
    });
  }

  function setOrderDirty(v) {
    orderDirty = !!v;
    const btn = $("#btn-save-order");
    if (btn) btn.disabled = !orderDirty;
    const hint = $("#ok-strip-hint");
    if (hint) {
      hint.classList.toggle("order-dirty", orderDirty);
      if (orderDirty) hint.textContent = "顺序已改，尚未保存";
      else hint.textContent = "拖动卡片调整顺序 · 点「保存排列」后全员生效";
    }
  }

  function mergeGridIntoOrder(prevOrder, gridIds, allIds) {
    const inGrid = {};
    (gridIds || []).forEach((id) => {
      if (id) inGrid[id] = true;
    });
    const q = (gridIds || []).slice();
    const out = [];
    const used = {};
    (prevOrder || []).forEach((id) => {
      if (!id || used[id]) return;
      if (inGrid[id]) {
        const n = q.shift();
        if (n && !used[n]) {
          out.push(n);
          used[n] = true;
        }
      } else {
        out.push(id);
        used[id] = true;
      }
    });
    q.forEach((id) => {
      if (id && !used[id]) {
        out.push(id);
        used[id] = true;
      }
    });
    (allIds || []).forEach((id) => {
      if (id && !used[id]) {
        out.push(id);
        used[id] = true;
      }
    });
    return out;
  }

  function bindReorder(grid) {
    if (!grid) return;
    grid.querySelectorAll(".ch-card").forEach((el) => {
      el.setAttribute("draggable", "true");
      el.addEventListener("dragstart", (e) => {
        dragSrc = el;
        el.classList.add("dragging");
        el.dataset.justDragged = "1";
        try {
          e.dataTransfer.setData("text/plain", el.dataset.id || "");
          e.dataTransfer.effectAllowed = "move";
        } catch (err) {}
      });
      el.addEventListener("dragend", () => {
        el.classList.remove("dragging");
        grid.querySelectorAll(".drag-over").forEach((x) => x.classList.remove("drag-over"));
        dragSrc = null;
      });
      el.addEventListener("dragover", (e) => {
        e.preventDefault();
        if (!dragSrc || el === dragSrc) return;
        el.classList.add("drag-over");
        const rect = el.getBoundingClientRect();
        const before = e.clientX < rect.left + rect.width / 2;
        if (before) grid.insertBefore(dragSrc, el);
        else grid.insertBefore(dragSrc, el.nextSibling);
        setOrderDirty(true);
      });
      el.addEventListener("dragleave", () => el.classList.remove("drag-over"));
    });
  }

  async function saveChannelOrder() {
    const grid = $("#channel-grid");
    if (!grid) return;
    const gridIds = [];
    grid.querySelectorAll(".ch-card").forEach((el) => {
      if (el.dataset.id) gridIds.push(el.dataset.id);
    });
    const ids = mergeGridIntoOrder(lastOrderIds, gridIds, lastDashCardIds);
    try {
      const r = await postJSON("/api/channels/order", { ids: ids });
      lastOrderIds = r.ids || ids;
      setOrderDirty(false);
      toast("排列已保存", "ok");
    } catch (e) {
      toast("保存排列失败: " + (e.message || e), "err");
    }
  }

  function syncFreezeHint() {
    const picked = document.querySelector('input[name="freeze-mode"]:checked');
    const mode = picked ? picked.value : "video";
    const n = parseFloat(($("#inp-freeze") && $("#inp-freeze").value) || "");
    const hint = $("#freeze-hint");
    if (!hint) return;
    if (mode === "video_silence") {
      hint.textContent = "画面静止且这几秒电平低于静音阈值才告警。填几秒就按几秒，不另报无伴音。";
    } else if (!Number.isNaN(n) && n < 12) {
      hint.textContent = "只报静帧最短 12 秒。当前填写 " + n + " 秒，保存后按 12 秒执行。";
    } else {
      hint.textContent = "只看画面。最短 12 秒。";
    }
  }

  const PRESET_TAGS = ["央视", "卫视", "高清", "标清"];

  function cardTags(c) {
    return CategoryFilter.cardTags(c);
  }

  function fillCategoryTags(raw) {
    const tags = cardTags({ category: raw });
    document.querySelectorAll("#ch-tags input").forEach((el) => {
      el.checked = tags.indexOf(el.value) >= 0;
    });
    const extra = tags.filter((t) => PRESET_TAGS.indexOf(t) < 0);
    if ($("#ch-category-extra")) $("#ch-category-extra").value = extra.join("、");
  }

  function readCategoryTags() {
    const tags = [];
    document.querySelectorAll("#ch-tags input:checked").forEach((el) => {
      if (el.value) tags.push(el.value);
    });
    const extra = ($("#ch-category-extra") && $("#ch-category-extra").value) || "";
    extra.split(/[、,，;；\s]+/).forEach((t) => {
      if (t) tags.push(t);
    });
    return tags;
  }

  function renderCategoryTabs(all) {
    const box = $("#cat-tabs");
    if (!box) return;
    const names = [];
    let uncat = false;
    all.forEach((c) => {
      const tags = cardTags(c);
      if (!tags.length) uncat = true;
      tags.forEach((name) => {
        if (names.indexOf(name) < 0) names.push(name);
      });
    });
    dashCategories = CategoryFilter.pruneDashCategories(dashCategories, names, uncat);
    const btn = (cat, label) => {
      const on = !cat ? dashCategories.length === 0 : dashCategories.indexOf(cat) >= 0;
      return `<button type="button" class="cat-tab${on ? " on" : ""}" data-cat="${escapeHtml(
        cat
      )}">${escapeHtml(label)}</button>`;
    };
    let html = btn("", "全部");
    names.forEach((name) => {
      html += btn(name, name);
    });
    if (uncat && names.length) html += btn("__none__", "未分类");
    box.innerHTML = html;
    box.querySelectorAll(".cat-tab").forEach((el) => {
      el.addEventListener("click", () => {
        dashCategories = CategoryFilter.toggleDashCategories(
          dashCategories,
          el.dataset.cat || ""
        );
        if (lastDash) renderDashboard(lastDash);
      });
    });
  }

  let lastPerf = null;

  function busiestNic(p) {
    let best = null;
    ((p && p.nics) || []).forEach((n) => {
      const rx = Number(n.rx_kbps) || 0;
      if (!best || rx > (Number(best.rx_kbps) || 0)) best = n;
    });
    return best;
  }

  function serverMini(label, pct, value) {
    return `<div class="server-mini"><span>${escapeHtml(label)}</span>${perfBar(pct)}<span class="num">${escapeHtml(value)}</span></div>`;
  }

  function renderServerStrip(dash, perf) {
    const box = $("#node-strip");
    if (!box) return;
    const hub = (dash && dash.hub) || {};
    const nodes = hub.nodes || (dash && dash.nodes) || [];
    if (!hub.active || !nodes.length) {
      box.classList.add("hidden");
      box.innerHTML = "";
      return;
    }
    const byId = {};
    ((perf && perf.nodes) || []).forEach((n) => {
      byId[n.id] = n;
    });
    box.classList.remove("hidden");
    box.innerHTML = nodes
      .map((n) => {
        const extra = byId[n.id];
        const p = extra && extra.perf;
        const online = n.ok !== false && (!extra || extra.ok !== false);
        const head = `<div class="server-card-top"><b><i class="dot ${online ? "green" : "red"}"></i>${escapeHtml(
          n.name || n.id
        )}</b><span>${n.ok === false ? "离线" : (n.cards || 0) + " 路"}</span></div>`;
        if (!p) {
          return `<article class="server-card${online ? "" : " bad"}">${head}<div class="hint">${
            online ? "正在读取性能" : "网页无响应"
          }</div></article>`;
        }
        const mem = p.memory || {};
        const nic = busiestNic(p);
        const nicLabel = nic ? nic.name || "网卡" : "网卡";
        const nicVal = nic ? fmtBitrate(nic.rx_kbps) || "-" : "-";
        const cpu = p.cpu_percent != null ? p.cpu_percent + "%" : "-";
        const memPct = mem.used_percent != null ? mem.used_percent + "%" : "-";
        return `<article class="server-card">${head}
          ${serverMini("CPU", p.cpu_percent, cpu)}
          ${serverMini("内存", mem.used_percent, memPct)}
          ${serverMini(nicLabel, nic && nic.occupancy_percent, nicVal)}
        </article>`;
      })
      .join("");
  }

  function renderDashboard(dash) {
    if (!dash) return;
    lastDash = dash;
    const sum = dash.summary || {};
    if ($("#sum-green")) $("#sum-green").textContent = sum.green ?? 0;
    if ($("#sum-red")) $("#sum-red").textContent = sum.red ?? 0;
    if ($("#sum-yellow")) $("#sum-yellow").textContent = sum.yellow ?? 0;
    if ($("#sum-gray")) $("#sum-gray").textContent = sum.gray ?? 0;

    const all = (dash.cards || []).filter((c) => c.enabled !== false);
    renderServerStrip(dash, lastPerf);
    renderCategoryTabs(all);
    const bad = all.filter((c) => c.lamp === "red" || c.lamp === "yellow");
    const okAll = all.filter((c) => c.lamp !== "red" && c.lamp !== "yellow");
    const ok = okAll.filter((c) => CategoryFilter.cardMatchesCategories(c, dashCategories));

    const strip = $("#alarm-strip");
    const alarmGrid = $("#alarm-grid");
    if (strip && alarmGrid) {
      if (!bad.length) {
        strip.classList.add("hidden");
        alarmGrid.innerHTML = "";
      } else {
        strip.classList.remove("hidden");
        alarmGrid.innerHTML = bad.map((c) => channelCardHtml(c, true)).join("");
        bindChannelCardClicks(alarmGrid);
      }
    }

    const evBox = $("#event-list-dash");
    if (evBox) {
      const open = [];
      all.forEach((c) => {
        const tags = (c.active_alarms || []).filter(Boolean);
        tags.forEach((t) => {
          open.push({
            type: t,
            channel_id: c.id,
            channel_name: c.name || c.id,
            message: "未恢复",
            time: dash.time || "",
          });
        });
      });
      if (!open.length) {
        evBox.innerHTML = `<div class="empty">当前没有未恢复的告警</div>`;
      } else {
        evBox.innerHTML = open.map(renderEventItem).join("");
      }
    }

    const okHead = $("#ok-strip-head");
    if (okHead) {
      okHead.style.display = all.length ? "" : "none";
    }
    lastDashCardIds = all.map((c) => c.id).filter(Boolean);
    if (!orderDirty) {
      lastOrderIds = Array.isArray(dash.card_order) && dash.card_order.length
        ? dash.card_order.slice()
        : lastDashCardIds.slice();
    }

    const grid = $("#channel-grid");
    if (grid && !orderDirty) {
      if (!all.length) {
        grid.innerHTML = `<div class="empty">暂无已启用的监测频道</div>`;
      } else if (!ok.length) {
        grid.innerHTML = `<div class="empty">${
          dashCategories.length ? "没有同时带上这些标签的正常频道" : "当前没有正常频道"
        }</div>`;
      } else {
        grid.innerHTML = ok.map((c) => channelCardHtml(c, false)).join("");
        bindChannelCardClicks(grid);
        bindReorder(grid);
      }
    }

    const st = dash.stats_24h;
    if ($("#stat-24h")) $("#stat-24h").textContent = st ? st.total : "-";
    const byType = $("#hist-by-type");
    const byCh = $("#hist-by-ch");
    if (byType) {
      if (st && st.by_type && st.by_type.length) {
        byType.innerHTML = st.by_type
          .map(
            (x) =>
              `<li><span>${escapeHtml(typeLabel(x.type))} <small>(${escapeHtml(
                x.type
              )})</small></span><b>${x.count}</b></li>`
          )
          .join("");
      } else {
        byType.innerHTML = `<li class="empty">暂无（需 Worker 双写 SQLite）</li>`;
      }
    }
    if (byCh) {
      if (st && st.by_channel && st.by_channel.length) {
        byCh.innerHTML = st.by_channel
          .map(
            (x) =>
              `<li><span>${escapeHtml(x.channel_name || x.channel_id)}</span><b>${x.count}</b></li>`
          )
          .join("");
      } else {
        byCh.innerHTML = `<li class="empty">暂无</li>`;
      }
    }
  }

  let previewPlayer = null;
  let previewThumbTimer = null;

  function closePreview() {
    const modal = $("#preview-modal");
    if (modal) modal.classList.add("hidden");
    if (previewThumbTimer) {
      clearInterval(previewThumbTimer);
      previewThumbTimer = null;
    }
    try {
      if (previewPlayer) {
        previewPlayer.pause();
        previewPlayer.unload();
        previewPlayer.detachMediaElement();
        previewPlayer.destroy();
      }
    } catch (e) {}
    previewPlayer = null;
    const v = $("#preview-video");
    if (v) {
      try {
        v.pause();
        v.removeAttribute("src");
        v.load();
        v.style.display = "block";
      } catch (e) {}
    }
    const img = document.getElementById("preview-thumb");
    if (img) img.style.display = "none";
  }

  function startThumbFallback(channelId, hint, snapBase) {
    const video = $("#preview-video");
    if (!video) return;
    // 用图片轮询代替直播（内网更稳）
    let img = document.getElementById("preview-thumb");
    if (!img) {
      img = document.createElement("img");
      img.id = "preview-thumb";
      img.style.cssText = "width:100%;max-height:70vh;object-fit:contain;background:#000";
      video.style.display = "none";
      video.parentNode.insertBefore(img, video);
    }
    img.style.display = "block";
    const base = snapBase || ("/api/snapshots/" + encodeURIComponent(channelId));
    const tick = () => {
      img.src = base.replace(/\/$/, "") + "/latest.jpg?t=" + Date.now();
    };
    tick();
    previewThumbTimer = setInterval(tick, 1000);
    if (hint) {
      hint.textContent = "当前为实时截图预览（每秒刷新）。直播播放失败时自动降级。";
    }
  }

  function openPreview(channelId, name, snapBase) {
    // 内网默认用实时截图轮询（稳）；不依赖浏览器播 TS
    const modal = $("#preview-modal");
    const video = $("#preview-video");
    const title = $("#preview-title");
    const hint = $("#preview-hint");
    if (!modal || !video) return;
    closePreview();
    if (title) title.textContent = "预览: " + (name || channelId);
    modal.classList.remove("hidden");
    startThumbFallback(channelId, hint, snapBase);
  }

  function renderOverview(data) {
    $("#stat-total").textContent = data.channel_total;
    $("#stat-enabled").textContent = data.channel_enabled;
    $("#stat-alarm").textContent = data.channel_alarm;
    const ai = data.ai || {};
    $("#stat-ai").textContent = ai.enabled ? `开启 · ${ai.mode || "auto"}` : "关闭";
    $("#clock").textContent = data.time || "";

    if (!suppressRefresh) fillControls(data);

    const tbody = $("#channel-tbody");
    const channels = data.channels || [];
    channelCache = {};
    channels.forEach((c) => {
      channelCache[c.id] = c;
    });

    if (!channels.length) {
      tbody.innerHTML = `<tr><td colspan="12" class="empty">暂无频道，点击「新增」或「导入」</td></tr>`;
    } else {
      tbody.innerHTML = channels
        .map(
          (c) => `
        <tr data-id="${escapeHtml(c.id)}">
          <td>
            <input type="checkbox" class="toggle-mini ch-enable"
              data-id="${escapeHtml(c.id)}"
              ${c.enabled ? "checked" : ""} title="启用/禁用监测" />
          </td>
          <td>${statusBadge(c.status)}</td>
          <td>${escapeHtml(c.id)}</td>
          <td>${escapeHtml(c.name)}</td>
          <td>${
            cardTags(c).length
              ? escapeHtml(cardTags(c).join("、"))
              : "<span style=\"color:var(--muted)\">-</span>"
          }</td>
          <td>${
            c.program !== undefined && c.program !== null && c.program !== ""
              ? escapeHtml(c.program)
              : "<span style=\"color:var(--muted)\">-</span>"
          }</td>
          <td class="ch-br">${
            streamLabel(c)
              ? escapeHtml(streamLabel(c))
              : "<span style=\"color:var(--muted)\">-</span>"
          }</td>
          <td>${
            c.iface
              ? escapeHtml(c.iface)
              : "<span style=\"color:var(--muted)\">-</span>"
          }</td>
          <td>${escapeHtml(c.last_type ? typeLabel(c.last_type) : "-")}${
            c.active_alarms && c.active_alarms.length
              ? `<br><span style="color:var(--alarm);font-size:11px">进行中: ${escapeHtml(
                  c.active_alarms.join(",")
                )}</span>`
              : ""
          }${
            c.last_event
              ? `<br><span style="color:var(--muted);font-size:11px">${escapeHtml(c.last_event)}</span>`
              : ""
          }${
            c.heartbeat
              ? `<br><span style="color:var(--muted);font-size:11px">心跳 ${escapeHtml(c.heartbeat)}</span>`
              : ""
          }</td>
          <td>${c.event_count || 0}</td>
          <td class="url" title="${escapeHtml(c.url)}">${escapeHtml(c.url)}</td>
          <td class="ops">
            <button type="button" class="btn-link ch-edit" data-id="${escapeHtml(c.id)}">编辑</button>
            <button type="button" class="btn-link danger ch-del" data-id="${escapeHtml(c.id)}">删除</button>
          </td>
        </tr>`
        )
        .join("");

      tbody.querySelectorAll(".ch-enable").forEach((el) => {
        el.addEventListener("change", async () => {
          const id = el.dataset.id;
          const enabled = el.checked;
          try {
            suppressRefresh = true;
            const res = await postJSON(`/api/config/channels/${encodeURIComponent(id)}`, { enabled });
            toast(res.message || "已更新", "ok");
            await refresh();
          } catch (e) {
            el.checked = !enabled;
            toast("保存失败: " + e.message, "err");
          } finally {
            suppressRefresh = false;
          }
        });
      });

      tbody.querySelectorAll(".ch-edit").forEach((el) => {
        el.addEventListener("click", () => {
          const id = el.dataset.id;
          openChannelModal("edit", channelCache[id] || { id });
        });
      });

      tbody.querySelectorAll(".ch-del").forEach((el) => {
        el.addEventListener("click", async () => {
          const id = el.dataset.id;
          if (!confirm("确定删除频道「" + id + "」？此操作写入配置。")) return;
          try {
            const res = await delJSON(`/api/config/channels/${encodeURIComponent(id)}`);
            toast(res.message || "已删除", "ok");
            await refresh();
          } catch (e) {
            toast("删除失败: " + e.message, "err");
          }
        });
      });
    }

    loadEventsPage().catch(() => {});
  }

  function renderEventItem(ev) {
    const t = ev.type || "event";
    const msg = ev.message || ev.msg || "";
    const name = ev.channel_name || ev.channel_id || "";
    const abs = ev.time || "";
    const rel = relativeTime(abs);
    return `
      <div class="event-item ${typeClass(t)}">
        <div class="event-top">
          <span class="event-type">${escapeHtml(typeLabel(t))}</span>
          <span class="event-time" title="${escapeHtml(abs)}">${escapeHtml(rel || abs)}</span>
        </div>
        <div class="event-msg"><strong>${escapeHtml(name)}</strong> ${escapeHtml(msg)}</div>
        <div class="event-time" style="margin-top:4px;font-size:11px;color:var(--muted)">${escapeHtml(abs)}</div>
      </div>`;
  }

  async function loadEventsPage() {
    const box = $("#event-list");
    if (!box) return;
    const q = ($("#ev-q") && $("#ev-q").value.trim()) || "";
    const typ = ($("#ev-type") && $("#ev-type").value) || "";
    const hours = ($("#ev-hours") && $("#ev-hours").value) || "";
    const params = new URLSearchParams();
    params.set("limit", String(EV_PAGE));
    params.set("offset", String(evOffset));
    if (q) params.set("q", q);
    if (typ) params.set("event_type", typ);
    if (hours) params.set("hours", hours);
    try {
      const data = await fetchJSON("/api/alerts/history?" + params.toString());
      const alerts = data.alerts || [];
      evTotal = data.total || alerts.length;
      if (!alerts.length) {
        box.innerHTML = `<div class="empty">没有匹配的告警</div>`;
      } else {
        box.innerHTML = alerts.map(renderEventItem).join("");
      }
      const page = Math.floor(evOffset / EV_PAGE) + 1;
      const pages = Math.max(1, Math.ceil(evTotal / EV_PAGE));
      if ($("#ev-page-info")) {
        $("#ev-page-info").textContent = `第 ${page}/${pages} 页 · 共 ${evTotal} 条 · 来源 ${data.source || "-"}`;
      }
    } catch (e) {
      box.innerHTML = `<div class="empty">加载失败: ${escapeHtml(e.message)}</div>`;
    }
  }

  function renderSnapshots(data) {
    const grid = $("#snap-grid");
    const snaps = data.snapshots || [];
    if (!snaps.length) {
      grid.innerHTML = `<div class="empty">暂无截图</div>`;
      return;
    }
    grid.innerHTML = snaps
      .map((s) => {
        const title = s.channel_name || s.channel_id;
        const cap = `${title} · ${s.filename} · ${s.mtime}`;
        return `
      <div class="snap-card" data-url="${escapeHtml(s.url)}" data-cap="${escapeHtml(cap)}">
        <img src="${escapeHtml(s.url)}" loading="lazy" alt="${escapeHtml(title)}" />
        <div class="snap-meta">
          <strong>${escapeHtml(s.node_name ? s.node_name + " · " + title : title)}</strong>
          <span style="color:var(--muted);font-size:11px"> ${escapeHtml(s.channel_id || "")}</span><br/>
          ${escapeHtml(typeLabel((s.filename || "").split("_")[0]))} · ${escapeHtml(relativeTime(s.mtime) || s.mtime)}
        </div>
      </div>`;
      })
      .join("");
    grid.querySelectorAll(".snap-card").forEach((el) => {
      el.addEventListener("click", () => openLightbox(el.dataset.url, el.dataset.cap));
    });
  }

  function openLightbox(url, cap) {
    $("#lb-img").src = url;
    $("#lb-cap").textContent = cap || "";
    $("#lightbox").classList.remove("hidden");
  }

  function closeLightbox() {
    $("#lightbox").classList.add("hidden");
    $("#lb-img").src = "";
  }

  async function refresh() {
    try {
      // 先拉核心数据；存储/性能分开，避免拖死整页
      const [overview, health, full, dash] = await Promise.all([
        fetchJSON("/api/overview", 20000),
        fetchJSON("/api/health", 8000).catch(() => ({ ok: false })),
        fetchJSON("/api/channels", 20000).catch(() => ({ channels: [] })),
        fetchJSON("/api/dashboard", 20000).catch(() => null),
      ]);
      const byId = {};
      (full.channels || []).forEach((c) => {
        byId[c.id] = c;
      });
      (overview.channels || []).forEach((c) => {
        if (byId[c.id]) {
          c.url = byId[c.id].url;
          c.name = byId[c.id].name || c.name;
          c.enabled = byId[c.id].enabled;
          if (byId[c.id].program !== undefined) c.program = byId[c.id].program;
          if (byId[c.id].iface) c.iface = byId[c.id].iface;
          if (Array.isArray(byId[c.id].category) || byId[c.id].category) {
            c.category = byId[c.id].category;
          } else if (!c.category) c.category = [];
        }
      });
      handleNewEvents(overview.recent_events || []);
      renderOverview(overview);
      if (dash) renderDashboard(dash);
      if ($("#stat-storage") && health.sqlite) {
        const mb = ((health.sqlite.db_size_bytes || 0) / 1024 / 1024).toFixed(2);
        $("#stat-storage").textContent =
          (health.sqlite.alerts_count ?? 0) + "条 · " + mb + "MB";
      } else if ($("#stat-storage")) {
        $("#stat-storage").textContent = "文件";
      }
      let h = health.ok ? "服务正常" : "服务异常";
      if (health.sqlite && health.sqlite.db_path) h += " · SQLite";
      const en = (overview.channels || []).filter((c) => c.enabled).length;
      if (en > 30) h += " · 启用" + en + "路偏多";
      $("#health").textContent = h;

      // 次要数据：失败不挡住主界面
      fetchJSON("/api/snapshots?limit=24", 12000)
        .then((snaps) => renderSnapshots(snaps))
        .catch(() => {});
      loadEventsPage().catch(() => {});
      const manageVisible =
        $("#view-manage") && !$("#view-manage").classList.contains("hidden");
      refreshPerf().catch(() => {});
      if (manageVisible) {
        refreshStorageDetail().catch(() => {});
        loadHubEditor().catch(() => {});
      }
    } catch (e) {
      console.error(e);
      $("#health").textContent = "接口请求失败: " + (e.message || e);
      const grid = $("#channel-grid");
      if (grid && grid.innerHTML.indexOf("加载中") >= 0) {
        grid.innerHTML =
          '<div class="empty">加载超时/失败。请减少启用频道数后刷新。' +
          escapeHtml(e.message || "") +
          "</div>";
      }
    }
  }

  function fmtBytes(n) {
    if (n == null || Number.isNaN(n)) return "-";
    const u = ["B", "KB", "MB", "GB", "TB"];
    let v = Number(n);
    let i = 0;
    while (v >= 1024 && i < u.length - 1) {
      v /= 1024;
      i++;
    }
    return v.toFixed(i === 0 ? 0 : 1) + u[i];
  }

  function perfLevel(pct) {
    if (pct == null || pct === "") return "";
    const n = Number(pct);
    if (Number.isNaN(n)) return "";
    if (n >= 85) return "hot";
    if (n >= 60) return "warn";
    return "ok";
  }

  function perfBar(pct) {
    const known = pct != null && pct !== "" && !Number.isNaN(Number(pct));
    const n = known ? Math.max(0, Math.min(100, Number(pct))) : 0;
    return `<div class="perf-bar"><span class="${perfLevel(pct)}" style="width:${n.toFixed(1)}%"></span></div>`;
  }

  function fmtLink(mbps) {
    const n = Number(mbps);
    if (!n || n <= 0) return "";
    if (n >= 1000 && n % 1000 === 0) return n / 1000 + " Gb/s";
    return n + " Mb/s";
  }

  function perfMeter(label, value, pct, foot) {
    return `<div class="perf-meter">
      <div class="perf-meter-top"><span>${escapeHtml(label)}</span><b>${escapeHtml(value)}</b></div>
      ${perfBar(pct)}
      <div class="perf-foot">${escapeHtml(foot)}</div>
    </div>`;
  }

  function perfDetailHtml(p) {
    const cpu = p.cpu_percent != null ? p.cpu_percent + "%" : "-";
    const mem = p.memory || {};
    const disk = p.disk || {};
    const memPct = mem.used_percent != null ? mem.used_percent + "%" : "-";
    const diskPct = disk.used_percent != null ? disk.used_percent + "%" : "-";
    const load1 = p.loadavg && p.loadavg["1"] != null ? Number(p.loadavg["1"]).toFixed(2) : "-";
    const load5 = p.loadavg && p.loadavg["5"] != null ? Number(p.loadavg["5"]).toFixed(2) : "-";
    const cores = p.cpu_cores != null ? p.cpu_cores + " 核" : "-";
    const nicCards = (p.nics || [])
      .map((n) => {
        const car =
          n.carrier === true ? "有载波" : n.carrier === false ? "无载波" : n.operstate || "-";
        const link = fmtLink(n.speed_mbps);
        const ip = n.ipv4 || "无地址";
        const rx = fmtBitrate(n.rx_kbps) || "-";
        const tx = fmtBitrate(n.tx_kbps) || "-";
        const occ =
          n.occupancy_percent != null ? Number(n.occupancy_percent).toFixed(1) + "%" : "速率未知";
        const bits = [car, link, ip, "占用 " + occ, "发送 " + tx].filter(Boolean);
        return `<div class="perf-nic">
          <div class="perf-nic-top">
            <span><span class="dot${n.up ? " on" : ""}">●</span>${escapeHtml(n.name || "")}</span>
            <b>${escapeHtml(rx)}</b>
          </div>
          ${perfBar(n.occupancy_percent)}
          <div class="perf-foot">${bits.map((t) => `<span>${escapeHtml(t)}</span>`).join("")}</div>
        </div>`;
      })
      .join("");
    const monBr = fmtBitrate(p.monitor_bitrate_kbps) || "-";
    return `
      <div class="perf-meters">
        ${perfMeter("CPU", cpu, p.cpu_percent, cores + " · 负载 " + load1 + " / " + load5)}
        ${perfMeter("内存", memPct, mem.used_percent, fmtBytes(mem.used_bytes) + " / " + fmtBytes(mem.total_bytes))}
        ${perfMeter("磁盘", diskPct, disk.used_percent, "已用 " + fmtBytes(disk.used_bytes) + " · 剩余 " + fmtBytes(disk.free_bytes))}
      </div>
      <div class="perf-net-head"><span>网络</span><span>监测节目合计 <b>${escapeHtml(monBr)}</b></span></div>
      <div class="perf-nics">${nicCards || '<div class="empty">没有读到网卡</div>'}</div>
    `;
  }

  async function refreshPerf() {
    const box = $("#perf-detail");
    try {
      let data = null;
      try {
        data = await fetchJSON("/api/hub/perf");
      } catch (e) {
        const p = await fetchJSON("/api/system/perf");
        data = { active: false, nodes: [{ id: "local", name: "本机", ok: true, perf: p }] };
      }
      lastPerf = data;
      if (lastDash) renderServerStrip(lastDash, data);
      if (!box) return;
      const nodes = (data && data.nodes) || [];
      const multi = !!(data && data.active && nodes.length > 1);
      if (!nodes.length) {
        box.textContent = "没有性能数据";
        return;
      }
      box.innerHTML = nodes
        .map((node) => {
          const body =
            node.ok && node.perf
              ? perfDetailHtml(node.perf)
              : '<div class="empty">离线，读不到这台的性能</div>';
          if (!multi) return body;
          return `<section class="perf-node"><div class="perf-node-name"><i class="dot ${
            node.ok ? "green" : "red"
          }"></i>${escapeHtml(node.name || node.id)}</div><div class="perf-node-body">${body}</div></section>`;
        })
        .join("");
      const stamp = nodes.map((n) => n.perf && n.perf.time).filter(Boolean)[0];
      if ($("#perf-time")) $("#perf-time").textContent = stamp ? "更新于 " + stamp : "";
    } catch (e) {
      if (box) box.textContent = "无法读取性能：" + (e.message || e);
    }
  }

  async function refreshStorageDetail() {
    const box = $("#storage-detail");
    if (!box) return;
    box.textContent = "读取中…";
    try {
      const d = await fetchJSON("/api/storage/detail");
      const sql = d.sqlite || {};
      box.innerHTML = `
        日志目录 <b>${fmtBytes(d.logs_bytes)}</b> ·
        事件文件 <b>${fmtBytes(d.events_bytes)}</b> ·
        截图 <b>${fmtBytes(d.snapshots_bytes)}</b> ·
        数据目录 <b>${fmtBytes(d.data_bytes)}</b> ·
        合计约 <b>${fmtBytes(d.total_bytes)}</b><br/>
        告警库 ${sql.alerts_count != null ? sql.alerts_count + " 条" : "-"} ·
        库文件 <b>${fmtBytes(sql.db_size_bytes)}</b>
      `;
    } catch (e) {
      box.textContent = "无法读取存储信息：" + (e.message || e);
    }
  }

  function setupAuto() {
    if (timer) clearInterval(timer);
    timer = null;
    if ($("#auto-refresh").checked) timer = setInterval(refresh, REFRESH_MS);
  }

  $("#btn-save-ai").addEventListener("click", async () => {
    const btn = $("#btn-save-ai");
    btn.disabled = true;
    try {
      const res = await postJSON("/api/config/ai", {
        enabled: $("#sw-ai-enabled").checked,
        mode: $("#sel-ai-mode").value,
        interval_sec: parseFloat($("#inp-ai-interval").value) || 2,
        threshold: parseFloat($("#inp-ai-threshold").value),
        green_ratio_th: parseFloat($("#inp-green-th").value),
        block_score_th: parseFloat($("#inp-block-th").value),
      });
      toast(res.message || "AI 设置已保存", "ok");
      await refresh();
    } catch (e) {
      toast("保存失败: " + e.message, "err");
    } finally {
      btn.disabled = false;
    }
  });

  document.querySelectorAll('input[name="freeze-mode"]').forEach((el) => {
    el.addEventListener("change", syncFreezeHint);
  });
  if ($("#inp-freeze")) $("#inp-freeze").addEventListener("input", syncFreezeHint);

  function hubRowHtml(n) {
    n = n || {};
    return `<div class="hub-row">
      <input class="hub-id" placeholder="ID" maxlength="32" value="${escapeHtml(n.id || "")}" />
      <input class="hub-name" placeholder="名称" maxlength="32" value="${escapeHtml(n.name || "")}" />
      <input class="hub-url" placeholder="http://监测机:8080，本机留空" value="${escapeHtml(n.url || "")}" />
      <button type="button" class="btn hub-del">删除</button>
    </div>`;
  }

  function bindHubRows() {
    document.querySelectorAll("#hub-rows .hub-del").forEach((btn) => {
      btn.onclick = () => {
        const row = btn.closest(".hub-row");
        if (row) row.remove();
      };
    });
  }

  async function loadHubEditor() {
    const box = $("#hub-rows");
    if (!box) return;
    const ae = document.activeElement;
    if (ae && ae.closest && ae.closest("#hub-editor")) return;
    const data = await fetchJSON("/api/hub/nodes");
    const nodes = data.nodes && data.nodes.length ? data.nodes : [{ id: "local", name: "本机", url: "" }];
    box.innerHTML = nodes.map(hubRowHtml).join("");
    bindHubRows();
  }

  const btnHubAdd = $("#btn-hub-add");
  if (btnHubAdd) {
    btnHubAdd.addEventListener("click", () => {
      const box = $("#hub-rows");
      if (!box) return;
      box.insertAdjacentHTML("beforeend", hubRowHtml({}));
      bindHubRows();
    });
  }
  const btnHubSave = $("#btn-hub-save");
  if (btnHubSave) {
    btnHubSave.addEventListener("click", async () => {
      const nodes = [];
      document.querySelectorAll("#hub-rows .hub-row").forEach((row) => {
        nodes.push({
          id: (row.querySelector(".hub-id").value || "").trim(),
          name: (row.querySelector(".hub-name").value || "").trim(),
          url: (row.querySelector(".hub-url").value || "").trim(),
        });
      });
      btnHubSave.disabled = true;
      try {
        const res = await postJSON("/api/hub/nodes", { nodes: nodes });
        toast(res.message || "节点已保存", "ok");
        await refresh();
      } catch (e) {
        toast("保存节点失败: " + (e.message || e), "err");
      } finally {
        btnHubSave.disabled = false;
      }
    });
  }

  $("#btn-save-defaults").addEventListener("click", async () => {
    const btn = $("#btn-save-defaults");
    btn.disabled = true;
    try {
      const res = await postJSON("/api/config/defaults", {
        save_snapshot: $("#sw-save-snapshot").checked,
        black_duration: parseFloat($("#inp-black").value) || 2,
        freeze_duration: parseFloat($("#inp-freeze").value) || 12,
        freeze_mode: (document.querySelector('input[name="freeze-mode"]:checked') || {}).value || "video",
        silence_duration: parseFloat($("#inp-silence").value) || 3,
        silence_threshold: parseFloat($("#inp-silence-db").value),
      });
      toast(res.message || "规则参数已保存", "ok");
      await refresh();
    } catch (e) {
      toast("保存失败: " + e.message, "err");
    } finally {
      btn.disabled = false;
    }
  });

  $("#btn-add-ch").addEventListener("click", () => openChannelModal("create"));
  $("#ch-modal-close").addEventListener("click", closeChannelModal);
  $("#ch-modal-cancel").addEventListener("click", closeChannelModal);
  $("#ch-modal").addEventListener("click", (e) => {
    if (e.target.id === "ch-modal") closeChannelModal();
  });

  function readProgramField() {
    const raw = ($("#ch-program").value || "").trim();
    if (!raw) return null;
    const n = parseInt(raw, 10);
    if (Number.isNaN(n) || n < 0) throw new Error("Program 须为非负整数");
    return n;
  }

  $("#ch-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    let program;
    try {
      program = readProgramField();
    } catch (err) {
      toast(err.message, "err");
      return;
    }
    const iface = ($("#ch-iface") && $("#ch-iface").value) || "";
    const payload = {
      id: $("#ch-id").value.trim(),
      name: $("#ch-name").value.trim(),
      url: $("#ch-url").value.trim(),
      enabled: $("#ch-enabled").checked,
      program: program,
      iface: iface || null,
    };
    try {
      let res;
      if (editMode === "create") {
        // 创建时无 program 不传该字段，避免多余 null
        const body = {
          id: payload.id,
          name: payload.name,
          url: payload.url,
          enabled: payload.enabled,
        };
        if (program !== null) body.program = program;
        if (iface) body.iface = iface;
        const cat = readCategoryTags();
        if (cat.length) body.category = cat;
        res = await postJSON("/api/config/channels", body);
      } else {
        res = await postJSON(`/api/config/channels/${encodeURIComponent(payload.id)}`, {
          name: payload.name,
          url: payload.url,
          enabled: payload.enabled,
          program: program, // null 表示清空
          iface: iface || null,
          category: readCategoryTags(),
        });
      }
      toast(res.message || "已保存", "ok");
      closeChannelModal();
      await refresh();
    } catch (err) {
      toast("保存失败: " + err.message, "err");
    }
  });

  function downloadExport(fmt) {
    window.location.href = "/api/config/export?fmt=" + fmt;
  }
  $("#btn-export-json").addEventListener("click", () => downloadExport("json"));
  $("#btn-export-yaml").addEventListener("click", () => downloadExport("yaml"));

  $("#btn-import").addEventListener("click", openImportModal);
  $("#import-modal-close").addEventListener("click", closeImportModal);
  $("#import-modal-cancel").addEventListener("click", closeImportModal);
  $("#import-modal").addEventListener("click", (e) => {
    if (e.target.id === "import-modal") closeImportModal();
  });
  $("#import-pick-file").addEventListener("click", () => $("#file-import").click());
  $("#file-import").addEventListener("change", async () => {
    const f = $("#file-import").files[0];
    if (!f) return;
    const text = await f.text();
    $("#import-text").value = text;
    $("#file-import").value = "";
  });

  $("#import-submit").addEventListener("click", async () => {
    const text = $("#import-text").value.trim();
    if (!text) {
      toast("请粘贴或选择文件", "err");
      return;
    }
    const mode = $("#import-mode").value;
    if (mode === "replace" && !confirm("替换模式会清空现有频道列表，确定继续？")) return;

    let data;
    try {
      data = JSON.parse(text);
    } catch (e) {
      // YAML: use file upload API via blob
      const blob = new Blob([text], { type: "application/x-yaml" });
      const fd = new FormData();
      fd.append("file", blob, "import.yaml");
      try {
        const r = await fetch("/api/config/import/file?mode=" + encodeURIComponent(mode), {
          method: "POST",
          body: fd,
        });
        const res = await r.json();
        if (!r.ok) throw new Error(res.detail || res.message || "导入失败");
        toast(res.message + "（合计 " + res.total + "）", "ok");
        closeImportModal();
        await refresh();
      } catch (err) {
        toast("导入失败: " + err.message, "err");
      }
      return;
    }

    let channels = Array.isArray(data) ? data : data.channels;
    if (!Array.isArray(channels)) {
      toast("JSON 需包含 channels 数组", "err");
      return;
    }

    try {
      const res = await postJSON("/api/config/import", { mode, channels });
      toast(
        res.message +
          "（导入 " +
          res.imported +
          "，新增 " +
          res.added +
          "，更新 " +
          res.updated +
          "，合计 " +
          res.total +
          "）",
        "ok"
      );
      closeImportModal();
      await refresh();
    } catch (e) {
      toast("导入失败: " + e.message, "err");
    }
  });

  loadAlertPrefs();
  $("#sw-sound").addEventListener("change", saveAlertPrefs);
  if ($("#sw-tts")) $("#sw-tts").addEventListener("change", saveAlertPrefs);
  $("#sw-desktop").addEventListener("change", () => {
    saveAlertPrefs();
    if ($("#sw-desktop").checked) ensureNotifyPermission();
  });
  if ($("#btn-tts-test")) {
    $("#btn-tts-test").addEventListener("click", () => {
      speak("语音告警测试，监测系统运行正常");
    });
  }
  if ($("#btn-tts-clear")) {
    $("#btn-tts-clear").addEventListener("click", () => {
      suppressionMap.clear();
      pendingTts = [];
      toast("已清除 TTS 抑制窗口", "ok");
    });
  }
  if ($("#btn-view-dash")) {
    $("#btn-view-dash").addEventListener("click", () => setView("dash"));
    $("#btn-view-manage").addEventListener("click", () => setView("manage"));
  }
  // 用户首次点击页面时申请通知权限（浏览器策略）
  document.addEventListener(
    "click",
    () => {
      if ($("#sw-desktop").checked) ensureNotifyPermission();
    },
    { once: true }
  );

  $("#btn-refresh").addEventListener("click", refresh);

  document.querySelectorAll(".card.clickable").forEach((el) => {
    el.addEventListener("click", () => {
      const go = el.dataset.goto;
      if (go === "channels" || go === "alarms") {
        setView("manage");
        const table = document.querySelector(".table-wrap");
        if (table) table.scrollIntoView({ behavior: "smooth" });
      } else if (go === "events") {
        setView("manage");
        const a = $("#manage-events-hint");
        if (a) a.scrollIntoView({ behavior: "smooth" });
        loadEventsPage().catch(() => {});
      } else if (go === "storage") {
        setView("manage");
        const a = $("#storage-anchor");
        if (a) a.scrollIntoView({ behavior: "smooth" });
        refreshStorageDetail();
      }
    });
  });

  const btnStorageRefresh = document.getElementById("btn-storage-refresh");
  if (btnStorageRefresh) {
    btnStorageRefresh.addEventListener("click", () => refreshStorageDetail());
  }
  const btnPerfRefresh = document.getElementById("btn-perf-refresh");
  if (btnPerfRefresh) {
    btnPerfRefresh.addEventListener("click", () => refreshPerf());
  }
  const previewClose = document.getElementById("preview-close");
  if (previewClose) previewClose.addEventListener("click", closePreview);
  const previewModal = document.getElementById("preview-modal");
  if (previewModal) {
    previewModal.addEventListener("click", (e) => {
      if (e.target === previewModal) closePreview();
    });
  }

  const btnStorageClear = document.getElementById("btn-storage-clear");
  if (btnStorageClear) {
    btnStorageClear.addEventListener("click", async () => {
      const body = {
        events_jsonl: !!(document.getElementById("clr-events") || {}).checked,
        channel_logs: !!(document.getElementById("clr-logs") || {}).checked,
        iface_capture_logs: !!(document.getElementById("clr-iface") || {}).checked,
        snapshots: !!(document.getElementById("clr-snaps") || {}).checked,
        sqlite_alerts: !!(document.getElementById("clr-sqlite") || {}).checked,
      };
      if (!Object.values(body).some(Boolean)) {
        toast("请先勾选要清理的项", "err");
        return;
      }
      if (!window.confirm("确认清理所选日志/截图/告警？此操作不可恢复。")) return;
      btnStorageClear.disabled = true;
      try {
        const res = await postJSON("/api/storage/clear", body);
        toast("清理完成", "ok");
        await refreshStorageDetail();
        await loadEventsPage();
      } catch (e) {
        toast("清理失败: " + (e.message || e), "err");
      } finally {
        btnStorageClear.disabled = false;
      }
    });
  }

  if ($("#ev-search")) {
    $("#ev-search").addEventListener("click", () => {
      evOffset = 0;
      loadEventsPage();
    });
    $("#ev-prev").addEventListener("click", () => {
      evOffset = Math.max(0, evOffset - EV_PAGE);
      loadEventsPage();
    });
    $("#ev-next").addEventListener("click", () => {
      if (evOffset + EV_PAGE < evTotal) evOffset += EV_PAGE;
      loadEventsPage();
    });
    ["ev-q", "ev-type", "ev-hours"].forEach((id) => {
      const el = $("#" + id);
      if (!el) return;
      el.addEventListener("keydown", (e) => {
        if (e.key === "Enter") {
          evOffset = 0;
          loadEventsPage();
        }
      });
    });
  }
  if ($("#btn-save-order")) {
    $("#btn-save-order").addEventListener("click", () => {
      saveChannelOrder();
    });
  }
  $("#auto-refresh").addEventListener("change", setupAuto);
  $("#lb-close").addEventListener("click", closeLightbox);
  $("#lightbox").addEventListener("click", (e) => {
    if (e.target.id === "lightbox") closeLightbox();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      closeLightbox();
      closeChannelModal();
      closeImportModal();
    }
  });

  refresh();
  setupAuto();
})();
