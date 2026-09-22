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

  let evOffset = 0;
  const EV_PAGE = 20;
  let evTotal = 0;

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
    $("#inp-freeze").value = d.freeze_duration ?? 3;
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
      $("#ch-url").value = "udp://@239.1.1.1:5000";
      $("#ch-program").value = "";
      $("#ch-enabled").checked = true;
      ifaceVal = "";
    } else {
      idEl.value = ch.id;
      idEl.disabled = true;
      $("#ch-name").value = ch.name || "";
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

  function renderDashboard(dash) {
    if (!dash) return;
    const sum = dash.summary || {};
    if ($("#sum-green")) $("#sum-green").textContent = sum.green ?? 0;
    if ($("#sum-red")) $("#sum-red").textContent = sum.red ?? 0;
    if ($("#sum-yellow")) $("#sum-yellow").textContent = sum.yellow ?? 0;
    if ($("#sum-gray")) $("#sum-gray").textContent = sum.gray ?? 0;

    const grid = $("#channel-grid");
    if (grid) {
      const cards = dash.cards || [];
      if (!cards.length) {
        grid.innerHTML = `<div class="empty">暂无频道</div>`;
      } else {
        grid.innerHTML = cards
          .map((c) => {
            const alarms = formatAlarmTags(c.active_alarms, c.last_type);
            const prog =
              c.program !== undefined && c.program !== null && c.program !== ""
                ? "P" + c.program
                : "";
            const thumb = c.thumb_url
              ? `<img class="ch-thumb" src="${escapeHtml(c.thumb_url)}" loading="lazy" alt="" />`
              : `<div class="ch-thumb placeholder">暂无画面</div>`;
            return `<div class="ch-card lamp-${escapeHtml(c.lamp || "gray")}" data-id="${escapeHtml(
              c.id
            )}" title="${escapeHtml(c.name || c.id)}">
              ${thumb}
              <div class="ch-body">
                <div class="ch-name">${escapeHtml(c.name || c.id)}</div>
                <div class="ch-id">${escapeHtml(c.id)}${prog ? " · " + escapeHtml(prog) : ""}</div>
                <div class="ch-meta">${escapeHtml(statusText(c.status))} · ${escapeHtml(alarms)}</div>
              </div>
            </div>`;
          })
          .join("");
        grid.querySelectorAll(".ch-card").forEach((el) => {
          el.addEventListener("click", () => {
            const id = el.dataset.id;
            const name =
              (el.querySelector(".ch-name") && el.querySelector(".ch-name").textContent) ||
              id;
            openPreview(id, name);
          });
        });
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

  function startThumbFallback(channelId, hint) {
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
    const tick = () => {
      img.src =
        "/api/snapshots/" +
        encodeURIComponent(channelId) +
        "/latest.jpg?t=" +
        Date.now();
    };
    tick();
    previewThumbTimer = setInterval(tick, 1000);
    if (hint) {
      hint.textContent = "当前为实时截图预览（每秒刷新）。直播播放失败时自动降级。";
    }
  }

  function openPreview(channelId, name) {
    // 内网默认用实时截图轮询（稳）；不依赖浏览器播 TS
    const modal = $("#preview-modal");
    const video = $("#preview-video");
    const title = $("#preview-title");
    const hint = $("#preview-hint");
    if (!modal || !video) return;
    closePreview();
    if (title) title.textContent = "预览: " + (name || channelId);
    modal.classList.remove("hidden");
    startThumbFallback(channelId, hint);
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
      tbody.innerHTML = `<tr><td colspan="10" class="empty">暂无频道，点击「新增」或「导入」</td></tr>`;
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
            c.program !== undefined && c.program !== null && c.program !== ""
              ? escapeHtml(c.program)
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

    const list = $("#event-list");
    const events = (data.recent_events || []).slice(0, 8);
    if (!events.length) {
      list.innerHTML = `<div class="empty">暂无事件</div>`;
    } else {
      list.innerHTML = events.map(renderEventItem).join("");
    }
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
    const box = $("#event-list-dash");
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
          <strong>${escapeHtml(title)}</strong>
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
      if (manageVisible) {
        refreshPerf().catch(() => {});
        refreshStorageDetail().catch(() => {});
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

  async function refreshPerf() {
    const box = $("#perf-detail");
    if (!box) return;
    try {
      const p = await fetchJSON("/api/system/perf");
      const cpu = p.cpu_percent != null ? p.cpu_percent + "%" : "-";
      const memPct = p.memory && p.memory.used_percent != null ? p.memory.used_percent + "%" : "-";
      const diskPct = p.disk && p.disk.used_percent != null ? p.disk.used_percent + "%" : "-";
      const load1 = p.loadavg && p.loadavg["1"] != null ? Number(p.loadavg["1"]).toFixed(2) : "-";
      const load5 = p.loadavg && p.loadavg["5"] != null ? Number(p.loadavg["5"]).toFixed(2) : "-";
      const cores = p.cpu_cores != null ? p.cpu_cores : "-";
      box.innerHTML = `
        CPU 估算 <b>${cpu}</b>（核数 ${cores}）· 负载 <b>${load1}</b> / ${load5}<br/>
        内存 <b>${memPct}</b>（${fmtBytes(p.memory && p.memory.used_bytes)} / ${fmtBytes(
        p.memory && p.memory.total_bytes
      )}）<br/>
        磁盘 <b>${diskPct}</b>（已用 ${fmtBytes(p.disk && p.disk.used_bytes)} · 剩余 ${fmtBytes(
        p.disk && p.disk.free_bytes
      )}）<br/>
        <span style="font-size:11px">更新于 ${escapeHtml(p.time || "")}</span>
      `;
    } catch (e) {
      box.textContent = "无法读取性能：" + (e.message || e);
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

  $("#btn-save-defaults").addEventListener("click", async () => {
    const btn = $("#btn-save-defaults");
    btn.disabled = true;
    try {
      const res = await postJSON("/api/config/defaults", {
        save_snapshot: $("#sw-save-snapshot").checked,
        black_duration: parseFloat($("#inp-black").value) || 2,
        freeze_duration: parseFloat($("#inp-freeze").value) || 3,
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
        res = await postJSON("/api/config/channels", body);
      } else {
        res = await postJSON(`/api/config/channels/${encodeURIComponent(payload.id)}`, {
          name: payload.name,
          url: payload.url,
          enabled: payload.enabled,
          program: program, // null 表示清空
          iface: iface || null,
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
        setView("dash");
        const a = $("#events-anchor");
        if (a) a.scrollIntoView({ behavior: "smooth" });
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
