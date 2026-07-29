(() => {
  const $ = (sel) => document.querySelector(sel);
  let timer = null;
  let suppressRefresh = false;
  let editMode = null;
  let channelCache = {};
  const REFRESH_MS = 5000;
  // 本地提醒：记录已见事件，避免刷新时重复吵
  let seenEventKeys = new Set();
  let alertsPrimed = false; // 首次加载只建基线，不提醒
  const LS_SOUND = "ai_monitor_sound";
  const LS_DESKTOP = "ai_monitor_desktop";

  function loadAlertPrefs() {
    const s = localStorage.getItem(LS_SOUND);
    const d = localStorage.getItem(LS_DESKTOP);
    if (s !== null) $("#sw-sound").checked = s === "1";
    if (d !== null) $("#sw-desktop").checked = d === "1";
  }

  function saveAlertPrefs() {
    localStorage.setItem(LS_SOUND, $("#sw-sound").checked ? "1" : "0");
    localStorage.setItem(LS_DESKTOP, $("#sw-desktop").checked ? "1" : "0");
  }

  function eventKey(ev) {
    return [ev.time || "", ev.type || "", ev.channel_id || "", ev.message || ev.msg || ""].join("|");
  }

  function playBeep() {
    try {
      const ctx = new (window.AudioContext || window.webkitAudioContext)();
      const o = ctx.createOscillator();
      const g = ctx.createGain();
      o.type = "sine";
      o.frequency.value = 880;
      g.gain.value = 0.08;
      o.connect(g);
      g.connect(ctx.destination);
      o.start();
      setTimeout(() => {
        o.frequency.value = 660;
      }, 120);
      setTimeout(() => {
        o.stop();
        ctx.close();
      }, 280);
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

    // 只对异常类型提醒（有 type 即视为事件）
    const alarms = fresh.filter((e) => e.type);
    if (!alarms.length) return;

    if ($("#sw-sound") && $("#sw-sound").checked) playBeep();

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

  function statusBadge(status) {
    const map = {
      ok: ["正常", "ok"],
      alarm: ["异常", "alarm"],
      disabled: ["禁用", "disabled"],
      unknown: ["未知", "unknown"],
      offline: ["离线", "offline"],
      stale: ["心跳超时", "stale"],
      reconnecting: ["重连中", "reconnecting"],
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

  async function fetchJSON(url) {
    const r = await fetch(url);
    if (!r.ok) throw new Error(url + " " + r.status);
    return r.json();
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

  function openChannelModal(mode, ch) {
    editMode = mode;
    $("#ch-modal-title").textContent = mode === "create" ? "新增频道" : "编辑频道";
    const idEl = $("#ch-id");
    if (mode === "create") {
      idEl.value = "";
      idEl.disabled = false;
      $("#ch-name").value = "";
      $("#ch-url").value = "udp://@239.1.1.1:5000";
      $("#ch-program").value = "";
      $("#ch-enabled").checked = true;
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
    }
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
      tbody.innerHTML = `<tr><td colspan="9" class="empty">暂无频道，点击「新增」或「导入」</td></tr>`;
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
          <td>${escapeHtml(c.last_type || "-")}${
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
    const events = data.recent_events || [];
    if (!events.length) {
      list.innerHTML = `<div class="empty">暂无事件</div>`;
    } else {
      list.innerHTML = events
        .map((ev) => {
          const t = ev.type || "event";
          const msg = ev.message || ev.msg || JSON.stringify(ev);
          return `
          <div class="event-item ${typeClass(t)}">
            <div class="event-top">
              <span class="event-type">${escapeHtml(t)}</span>
              <span class="event-time">${escapeHtml(ev.time || "")}</span>
            </div>
            <div class="event-msg">${escapeHtml(ev.channel_name || ev.channel_id || "")} ${escapeHtml(msg)}</div>
          </div>`;
        })
        .join("");
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
      .map(
        (s) => `
      <div class="snap-card" data-url="${escapeHtml(s.url)}" data-cap="${escapeHtml(
          s.channel_id + " · " + s.filename + " · " + s.mtime
        )}">
        <img src="${escapeHtml(s.url)}" loading="lazy" alt="${escapeHtml(s.filename)}" />
        <div class="snap-meta">
          <strong>${escapeHtml(s.channel_id)}</strong>
          ${escapeHtml(s.filename)}<br/>${escapeHtml(s.mtime)}
        </div>
      </div>`
      )
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
      const [overview, snaps, health, full] = await Promise.all([
        fetchJSON("/api/overview"),
        fetchJSON("/api/snapshots?limit=24"),
        fetchJSON("/api/health"),
        fetchJSON("/api/channels"),
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
        }
      });
      handleNewEvents(overview.recent_events || []);
      renderOverview(overview);
      renderSnapshots(snaps);
      $("#health").textContent = health.ok ? "服务正常" : "服务异常";
    } catch (e) {
      console.error(e);
      $("#health").textContent = "接口请求失败";
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
    const payload = {
      id: $("#ch-id").value.trim(),
      name: $("#ch-name").value.trim(),
      url: $("#ch-url").value.trim(),
      enabled: $("#ch-enabled").checked,
      program: program,
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
        res = await postJSON("/api/config/channels", body);
      } else {
        res = await postJSON(`/api/config/channels/${encodeURIComponent(payload.id)}`, {
          name: payload.name,
          url: payload.url,
          enabled: payload.enabled,
          program: program, // null 表示清空
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
  $("#sw-desktop").addEventListener("change", () => {
    saveAlertPrefs();
    if ($("#sw-desktop").checked) ensureNotifyPermission();
  });
  // 用户首次点击页面时申请通知权限（浏览器策略）
  document.addEventListener(
    "click",
    () => {
      if ($("#sw-desktop").checked) ensureNotifyPermission();
    },
    { once: true }
  );

  $("#btn-refresh").addEventListener("click", refresh);
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
