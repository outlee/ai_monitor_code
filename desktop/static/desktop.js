/* 桌面汇总。在原页面脚本之前运行。 */
(function () {
  window.AM_DESKTOP = true;
  var KEY = "am.desktop.scope";
  var snapTimer = null;

  function currentScope() {
    try {
      return localStorage.getItem(KEY) || "all";
    } catch (e) {
      return "all";
    }
  }

  function needsScope(path) {
    if (path === "/api/overview" || path === "/api/channels" || path === "/api/system/nics") return true;
    if (path === "/api/storage/detail" || path === "/api/storage/clear") return true;
    if (path === "/api/snapshots" || path === "/api/alerts/history") return true;
    if (path.indexOf("/api/config/") === 0) return true;
    return false;
  }

  function rewrite(url) {
    var hash = "";
    var hashAt = url.indexOf("#");
    if (hashAt >= 0) {
      hash = url.slice(hashAt);
      url = url.slice(0, hashAt);
    }
    url = url.replace(/(\/api\/config\/channels\/)([^?]+)/, function (_, pre, id) {
      try {
        id = decodeURIComponent(id);
      } catch (e) {}
      return pre + id;
    });
    var q = url.indexOf("?");
    var path = q >= 0 ? url.slice(0, q) : url;
    if (!needsScope(path) || /[?&]scope=/.test(url)) return url + hash;
    url += (q >= 0 ? "&" : "?") + "scope=" + encodeURIComponent(currentScope());
    return url + hash;
  }

  var origFetch = window.fetch;
  if (origFetch) {
    window.fetch = function (input, init) {
      if (typeof input === "string") input = rewrite(input);
      return origFetch.call(this, input, init);
    };
  }

  function note(msg, kind) {
    var el = document.getElementById("toast");
    if (!el) return;
    el.textContent = msg;
    el.className = "toast " + (kind || "ok");
    clearTimeout(el._t);
    el._t = setTimeout(function () {
      el.classList.add("hidden");
    }, 4200);
  }

  function stopSnap() {
    if (snapTimer) {
      clearInterval(snapTimer);
      snapTimer = null;
    }
  }

  function hidePreview() {
    stopSnap();
    var modal = document.getElementById("preview-modal");
    if (modal) modal.classList.add("hidden");
  }

  function openSnap(id, name, snap, message) {
    var modal = document.getElementById("preview-modal");
    var title = document.getElementById("preview-title");
    var hint = document.getElementById("preview-hint");
    var video = document.getElementById("preview-video");
    var stage = document.getElementById("preview-stage");
    var img = document.getElementById("preview-thumb");
    var vu = document.getElementById("preview-vu");
    if (!modal || !img) {
      note(message || "没有打开画面", "err");
      return;
    }
    stopSnap();
    if (title) title.textContent = "预览: " + (name || id);
    if (video) video.style.display = "none";
    if (stage) stage.style.display = "flex";
    if (vu) vu.dataset.id = id || "";
    if (hint) {
      hint.textContent = message || "播放器还没就绪，先按每秒一张图看。";
    }
    var base = snap || "/api/snapshots/" + encodeURIComponent(id || "");
    var tick = function () {
      img.src = String(base).replace(/\/$/, "") + "/latest.jpg?t=" + Date.now();
    };
    tick();
    snapTimer = setInterval(tick, 1000);
    modal.classList.remove("hidden");
  }

  var lastPlayAt = 0;

  function cardMeta(id) {
    var key = String(id || "");
    var cards = document.querySelectorAll(".ch-card");
    var card = null;
    for (var i = 0; i < cards.length; i++) {
      if (cards[i].dataset.id === key) {
        card = cards[i];
        break;
      }
    }
    if (!card) return {};
    var rate = card.querySelector(".ch-rate");
    return {
      url: card.dataset.url || "",
      program: card.dataset.program || "",
      iface: card.dataset.iface || "",
      node: card.dataset.node || "",
      bitrate: rate ? rate.textContent.trim() : "",
    };
  }

  function setNowText(id, text) {
    var el = document.getElementById(id);
    if (el) el.textContent = text || "";
  }

  function showNow(data) {
    var bar = document.getElementById("desk-now");
    if (!bar) return;
    if (!data || !data.playing) {
      bar.hidden = true;
      return;
    }
    bar.hidden = false;
    setNowText("desk-now-name", data.name || "");
    setNowText("desk-now-node", data.node ? "来自 " + data.node : "");
    var addr = data.addr && data.port ? data.addr + ":" + data.port : data.addr || "";
    setNowText("desk-now-addr", addr ? "组播 " + addr : "");
    setNowText("desk-now-prog", data.program ? "节目 " + data.program : "");
    setNowText("desk-now-iface", data.iface ? "网卡 " + data.iface : "");
    setNowText("desk-now-rate", data.bitrate ? "码率 " + data.bitrate : "码率 —");
  }

  function pollPlay() {
    fetch("/api/desktop/play")
      .then(function (r) {
        return r.json();
      })
      .then(function (data) {
        if (data && data.playing && data.id) {
          var live = cardMeta(data.id);
          if (live.bitrate) data.bitrate = live.bitrate;
        }
        showNow(data);
      })
      .catch(function () {});
  }

  function playCard(card) {
    if (!card) return;
    if (Date.now() - lastPlayAt < 700) return;
    lastPlayAt = Date.now();
    var nameEl = card.querySelector(".ch-name");
    postPlay(card.dataset.id || "", (nameEl && nameEl.textContent) || "", card.dataset.snap || "", cardMeta(card.dataset.id || ""));
  }

  function postPlay(id, name, snap, extra) {
    if (!id) return;
    var meta = extra || cardMeta(id);
    fetch("/api/desktop/play", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        id: id,
        name: name || id,
        url: meta.url || "",
        program: meta.program || "",
        iface: meta.iface || "",
        node: meta.node || "",
        bitrate: meta.bitrate || "",
      }),
    })
      .then(function (r) {
        return r.json().then(function (data) {
          return { ok: r.ok, data: data };
        });
      })
      .then(function (res) {
        var data = res.data || {};
        if (data.ok) {
          hidePreview();
          showNow(data);
          note(data.message || "正在播放，画面在最前面的窗口", "ok");
          return;
        }
        showNow({ playing: false });
        openSnap(id, name, snap, data.message || data.detail || "没能打开播放器");
        note(data.message || data.detail || "没能打开播放器", "err");
      })
      .catch(function (err) {
        openSnap(id, name, snap, "没能打开播放器");
        note("播放失败: " + (err && err.message ? err.message : err), "err");
      });
  }

  var cardPress = null;
  document.addEventListener(
    "pointerdown",
    function (e) {
      var t = e.target;
      if (!t || !t.closest || e.button !== 0) {
        cardPress = null;
        return;
      }
      var card = t.closest(".ch-card");
      cardPress = card ? { x: e.clientX, y: e.clientY, card: card } : null;
    },
    true
  );
  document.addEventListener(
    "pointerup",
    function (e) {
      if (!cardPress) return;
      var card = cardPress.card;
      var dx = Math.abs(e.clientX - cardPress.x);
      var dy = Math.abs(e.clientY - cardPress.y);
      cardPress = null;
      if (card.dataset.justDragged === "1") return;
      if (dx > 8 || dy > 8) return;
      if (!card.isConnected) return;
      e.preventDefault();
      e.stopPropagation();
      playCard(card);
    },
    true
  );

  document.addEventListener(
    "click",
    function (e) {
      var t = e.target;
      if (!t || !t.closest) return;
      if (t.closest("#summary-cards .card")) {
        e.preventDefault();
        e.stopPropagation();
        return;
      }
      var writeBtn = t.closest("#btn-add-ch, #btn-import, #import-submit");
      if (writeBtn && currentScope() === "all") {
        e.preventDefault();
        e.stopPropagation();
        note("请先在频道管理里选一台服务器，再新增或导入", "err");
        return;
      }
      var card = t.closest(".ch-card");
      if (card) {
        if (Date.now() - lastPlayAt < 700) {
          e.preventDefault();
          e.stopPropagation();
          return;
        }
        if (card.dataset.justDragged === "1") return;
        e.preventDefault();
        e.stopPropagation();
        playCard(card);
        return;
      }
      var row = t.closest("#event-list-dash .event-item[data-live='1']");
      if (row) {
        e.preventDefault();
        e.stopPropagation();
        var node = row.dataset.node || "";
        var ch = row.dataset.channel || "";
        var id = node && ch ? node + "/" + ch : ch;
        var strong = row.querySelector("strong");
        postPlay(id, (strong && strong.textContent) || id, row.dataset.snap || "");
        return;
      }
      var exp = t.closest("#btn-export-json, #btn-export-yaml");
      if (exp) {
        e.preventDefault();
        e.stopPropagation();
        if (currentScope() === "all") {
          note("请先在频道管理里选一台服务器", "err");
          return;
        }
        var fmt = exp.id === "btn-export-yaml" ? "yaml" : "json";
        window.location.href =
          "/api/config/export?fmt=" + fmt + "&scope=" + encodeURIComponent(currentScope());
      }
    },
    true
  );

  function scopeHint(value) {
    if (value === "all") {
      return "三台一起时，AI 和规则会写到每一台。频道、导入、导出和清理要先改成只选一台。存储数字是三台合计。";
    }
    return "当前只改这一台。AI、规则、频道、导入导出和清理都落在它上面。大屏仍是三台合在一起。";
  }

  function selectedLabel(sel) {
    var opt = sel.options[sel.selectedIndex];
    return (opt && opt.textContent) || sel.value;
  }

  function readDetail(data, status) {
    if (!data) return String(status || "");
    if (typeof data.detail === "string") return data.detail;
    if (data.message) return data.message;
    return String(status || "");
  }

  function channelScopeHint(value) {
    if (value === "all") {
      return "现在三台合在一起看。每一行前面是所属服务器，编辑和删除只改那一台。新增、导入、导出要先在这里选中一台。";
    }
    var sel = document.getElementById("desk-ch-scope") || document.getElementById("desktop-scope");
    var label = sel ? selectedLabel(sel) : value;
    return "正在改 " + label + " 的频道。新增、导入、导出和改地址只写到这一台，不会推到另外两台。";
  }

  function paintScope(value) {
    ["desktop-scope", "desk-ch-scope"].forEach(function (id) {
      var el = document.getElementById(id);
      if (!el) return;
      var has = false;
      for (var i = 0; i < el.options.length; i++) {
        if (el.options[i].value === value) has = true;
      }
      if (has) el.value = value;
    });
    var hint = document.getElementById("desktop-scope-hint");
    if (hint) hint.textContent = scopeHint(value);
    var ch = document.getElementById("desk-ch-hint");
    if (ch) ch.textContent = channelScopeHint(value);
  }

  function mountChannelScope() {
    var heads = document.querySelectorAll("#view-manage h2");
    var head = null;
    for (var i = 0; i < heads.length; i++) {
      if (heads[i].textContent.replace(/\s/g, "") === "频道管理") head = heads[i];
    }
    if (!head || document.getElementById("desk-ch-scope")) {
      paintScope(currentScope());
      return;
    }
    var actions = head.parentNode && head.parentNode.querySelector(".head-actions");
    if (!actions) return;
    var label = document.createElement("label");
    label.className = "desk-ch-scope";
    label.innerHTML =
      '服务器 <select id="desk-ch-scope"><option value="all">三台一起</option><option value="n173">173</option><option value="n176">176</option><option value="n177">177</option></select>';
    actions.insertBefore(label, actions.firstChild);
    var hint = document.createElement("p");
    hint.id = "desk-ch-hint";
    hint.className = "hint desk-ch-hint";
    if (head.parentNode && head.parentNode.parentNode) {
      head.parentNode.parentNode.insertBefore(hint, head.parentNode.nextSibling);
    }
    var sel = document.getElementById("desk-ch-scope");
    sel.addEventListener("change", function () {
      try {
        localStorage.setItem(KEY, sel.value);
      } catch (e) {}
      paintScope(sel.value);
      var other = document.getElementById("desktop-scope");
      if (other && other.value !== sel.value) other.value = sel.value;
      var btn = document.getElementById("btn-refresh");
      if (btn) btn.click();
    });
    paintScope(currentScope());
  }

  function decorateChannelIds() {
    var tbody = document.getElementById("channel-tbody");
    if (!tbody) return;
    var rows = tbody.querySelectorAll("tr[data-id]");
    for (var i = 0; i < rows.length; i++) {
      var tr = rows[i];
      var id = tr.getAttribute("data-id") || "";
      var cell = tr.children[2];
      if (!cell || cell.getAttribute("data-badged") === id) continue;
      var slash = id.indexOf("/");
      if (slash < 1) continue;
      var node = id.slice(0, slash).replace(/^n/, "");
      var raw = id.slice(slash + 1);
      cell.setAttribute("data-badged", id);
      cell.textContent = "";
      var badge = document.createElement("span");
      badge.className = "desk-srv";
      badge.textContent = node;
      cell.appendChild(badge);
      cell.appendChild(document.createTextNode(raw));
    }
  }

  function buildBar(nodes) {
    var panel = document.querySelector("#view-manage .controls-panel");
    if (!panel || document.getElementById("desktop-bar")) return;
    var saved = currentScope();
    var known = false;
    var options = '<option value="all">三台一起</option>';
    (nodes || []).forEach(function (n) {
      if (n.id === saved) known = true;
      options +=
        '<option value="' +
        String(n.id).replace(/"/g, "") +
        '">' +
        (n.name || n.id) +
        "</option>";
    });
    if (saved !== "all" && !known) saved = "all";
    var bar = document.createElement("div");
    bar.className = "desktop-bar";
    bar.id = "desktop-bar";
    bar.innerHTML =
      '<h3>桌面配置</h3>' +
      '<label>范围 <select id="desktop-scope">' +
      options +
      "</select></label>" +
      '<span class="desktop-files">' +
      '<label>模型 <input id="desktop-onnx" type="file" accept=".onnx" /></label>' +
      '<label>类别 <input id="desktop-classes" type="file" accept=".json,application/json" /></label>' +
      '<label><input id="desktop-restart-after" type="checkbox" checked /> 上传后重启</label>' +
      "</span>" +
      '<button type="button" class="btn btn-primary" id="desktop-upload">上传模型</button>' +
      '<button type="button" class="btn" id="desktop-restart">重启监测进程</button>' +
      '<p class="hint" id="desktop-scope-hint"></p>' +
      '<p class="hint">模型会写成 mosaic_detector.onnx，类别文件写在旁边。重启的是监测进程，网页不停，大约一分钟。进行中的告警不会补一条恢复。</p>';
    panel.insertBefore(bar, panel.firstChild);
    var sel = document.getElementById("desktop-scope");
    sel.value = saved;
    try {
      localStorage.setItem(KEY, saved);
    } catch (e) {}
    document.getElementById("desktop-scope-hint").textContent = scopeHint(saved);
    sel.addEventListener("change", function () {
      try {
        localStorage.setItem(KEY, sel.value);
      } catch (e) {}
      paintScope(sel.value);
      var btn = document.getElementById("btn-refresh");
      if (btn) btn.click();
    });
    var chSel = document.getElementById("desk-ch-scope");
    if (chSel) {
      chSel.innerHTML = sel.innerHTML;
      paintScope(saved);
    }
    document.getElementById("desktop-restart").addEventListener("click", function () {
      var label = selectedLabel(sel);
      if (!window.confirm("重启「" + label + "」的监测进程？大约一分钟。网页不重启。进行中的告警不会写恢复。")) {
        return;
      }
      fetch("/api/desktop/restart?scope=" + encodeURIComponent(sel.value), { method: "POST" })
        .then(function (r) {
          return r.json().then(function (data) {
            note(readDetail(data, r.status) || "已发出重启", r.ok && data.ok !== false ? "ok" : "err");
          });
        })
        .catch(function (err) {
          note("重启失败: " + err, "err");
        });
    });
    document.getElementById("desktop-upload").addEventListener("click", function () {
      var onnx = document.getElementById("desktop-onnx").files[0];
      var classes = document.getElementById("desktop-classes").files[0];
      if (!onnx || !classes) {
        note("请同时选择模型文件和类别 JSON", "err");
        return;
      }
      var label = selectedLabel(sel);
      var restart = document.getElementById("desktop-restart-after").checked;
      var ask =
        "把模型写到「" +
        label +
        "」？文件名固定为 mosaic_detector.onnx 和 mosaic_detector.onnx.classes.json。";
      if (restart) ask += "写完后重启这些机器的监测进程。";
      if (!window.confirm(ask)) return;
      var fd = new FormData();
      fd.append("onnx", onnx, "mosaic_detector.onnx");
      fd.append("classes", classes, "mosaic_detector.onnx.classes.json");
      var btn = document.getElementById("desktop-upload");
      btn.disabled = true;
      fetch(
        "/api/desktop/model?scope=" +
          encodeURIComponent(sel.value) +
          "&restart=" +
          (restart ? "true" : "false"),
        { method: "POST", body: fd }
      )
        .then(function (r) {
          return r.json().then(function (data) {
            note(readDetail(data, r.status) || "已上传", r.ok && data.ok !== false ? "ok" : "err");
          });
        })
        .catch(function (err) {
          note("上传失败: " + err, "err");
        })
        .then(function () {
          btn.disabled = false;
        });
    });
  }

  var SPLIT_KEY = "am.desk.railSplit";

  function playAlarmTest() {
    try {
      var Ctx = window.AudioContext || window.webkitAudioContext;
      if (!Ctx) return;
      var ctx = new Ctx();
      var seq = [880, 660, 880, 660, 988];
      var t = ctx.currentTime;
      seq.forEach(function (freq) {
        var o = ctx.createOscillator();
        var g = ctx.createGain();
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
      setTimeout(function () {
        try {
          ctx.close();
        } catch (e) {}
      }, 2200);
    } catch (e) {}
  }

  function readSplit() {
    var v = 0.5;
    try {
      v = parseFloat(localStorage.getItem(SPLIT_KEY) || "0.5");
    } catch (e) {}
    if (!isFinite(v)) v = 0.5;
    return Math.min(0.96, Math.max(0.04, v));
  }

  function setGrow(el, grow) {
    if (!el) return;
    var g = grow == null || grow === "" ? "" : String(grow);
    var b = g === "" ? "" : "0px";
    if (el.style.flexGrow !== g) el.style.flexGrow = g;
    if (el.style.flexBasis !== b) el.style.flexBasis = b;
  }

  function applySplit(ratio) {
    var open = document.getElementById("float-open");
    var remote = document.getElementById("float-remote");
    if (!open || !remote) return;
    setGrow(open, ratio);
    setGrow(remote, Math.max(0.04, 1 - ratio));
  }

  function syncSplit() {
    var bar = document.getElementById("desk-rail-split");
    var open = document.getElementById("float-open");
    if (!bar || !open) return;
    var openOn =
      !open.classList.contains("is-float") &&
      open.parentNode &&
      open.parentNode.id === "desk-rail";
    bar.hidden = !openOn;
  }

  function mountSplit() {
    var rail = document.getElementById("desk-rail");
    var remote = document.getElementById("float-remote");
    if (!rail || !remote || document.getElementById("desk-rail-split")) {
      syncSplit();
      return;
    }
    var bar = document.createElement("div");
    bar.id = "desk-rail-split";
    bar.className = "desk-rail-split";
    bar.title = "往下拉，未恢复告警变高，下面的内容下移；往上推则上移。超出窗口出现滚动条，缩回放得下滚动条消失";
    rail.insertBefore(bar, remote);
    var drag = null;
    bar.addEventListener("pointerdown", function (e) {
      if (e.button !== 0 || bar.hidden) return;
      e.preventDefault();
      e.stopPropagation();
      var rect = rail.getBoundingClientRect();
      var barRect = bar.getBoundingClientRect();
      var open = document.getElementById("float-open");
      drag = {
        id: e.pointerId,
        y0: e.clientY,
        topH: open ? open.offsetHeight : MIN_RAIL
      };
      paneDragging++;
      try {
        bar.setPointerCapture(e.pointerId);
      } catch (err) {}
    });
    bar.addEventListener("pointermove", function (e) {
      if (!drag || e.pointerId !== drag.id) return;
      var open = document.getElementById("float-open");
      var wantTop = Math.max(MIN_RAIL, drag.topH + (e.clientY - drag.y0));
      if (open) holdBlock(open, wantTop);
      saveNum(OPEN_H_KEY, wantTop);
      markUserSized();
      keepBarVisible(bar);
    });
    function endDrag(e) {
      if (!drag || !e || e.pointerId !== drag.id) return;
      drag = null;
      paneDragging = Math.max(0, paneDragging - 1);
    }
    bar.addEventListener("pointerup", endDrag);
    bar.addEventListener("pointercancel", endDrag);
    if (!document.getElementById("desk-split-remote")) {
      var remoteBar = document.createElement("div");
      remoteBar.id = "desk-split-remote";
      remoteBar.className = "desk-rail-split";
      remoteBar.title = "往下拉，最近告警变高，下面的内容下移；往上推则上移。超出窗口出现滚动条，缩回放得下滚动条消失";
      rail.appendChild(remoteBar);
      bindSplit(
        remoteBar,
        function () {
          var el = document.getElementById("float-remote");
          return { h: el ? el.offsetHeight : MIN_RAIL };
        },
        function (e, drag) {
          var el = document.getElementById("float-remote");
          if (!el) return;
          var want = Math.max(MIN_RAIL, drag.h + (e.clientY - drag.y0));
          holdBlock(el, want);
          saveNum(REMOTE_H_KEY, want);
          markUserSized();
          keepBarVisible(remoteBar);
        }
      );
    }
    syncSplit();
  }

  var RAIL_W_KEY = "am.desk.railW";
  var ALARM_SPLIT_KEY = "am.desk.alarmSplit";
  var ALARM_H_KEY = "am.desk.flowAlarm";
  var WATCH_H_KEY = "am.desk.flowWatch";
  var OPEN_H_KEY = "am.desk.flowOpen";
  var REMOTE_H_KEY = "am.desk.flowRemote";
  var SNAPS_H_KEY = "am.desk.flowSnaps";
  var SERVER_H_KEY = "am.desk.flowServer";
  var MIN_ALARM = 72;
  var MIN_WATCH = 120;
  var MIN_RAIL = 72;
  var MIN_SNAPS = 140;
  var MIN_SERVER = 88;
  var MIN_MAIN = 220;

  function readNum(key, fallback, min, max) {
    var v = fallback;
    try {
      var raw = localStorage.getItem(key);
      if (raw == null || raw === "") return fallback;
      v = parseFloat(raw);
    } catch (e) {}
    if (!isFinite(v)) v = fallback;
    if (min != null) v = Math.max(min, v);
    if (max != null) v = Math.min(max, v);
    return v;
  }

  function saveNum(key, v) {
    try {
      localStorage.setItem(key, String(Math.round(v * 1000) / 1000));
    } catch (e) {}
  }

  function makeSplit(id, vertical, title) {
    var bar = document.getElementById(id);
    if (bar) return bar;
    bar = document.createElement("div");
    bar.id = id;
    bar.className = "desk-split" + (vertical ? " desk-split-v" : "");
    bar.title = title;
    return bar;
  }

  var paneDragging = 0;

  function bindSplit(bar, onStart, onMove) {
    if (!bar || bar.dataset.bound) return;
    bar.dataset.bound = "1";
    var drag = null;
    bar.addEventListener("pointerdown", function (e) {
      if (e.button !== 0 || bar.hidden) return;
      e.preventDefault();
      e.stopPropagation();
      drag = onStart(e, bar);
      if (!drag) return;
      drag.pid = e.pointerId;
      drag.y0 = e.clientY;
      paneDragging++;
      try {
        bar.setPointerCapture(e.pointerId);
      } catch (err) {}
    });
    bar.addEventListener("pointermove", function (e) {
      if (!drag || e.pointerId !== drag.pid) return;
      onMove(e, drag);
    });
    function endDrag(e) {
      if (!drag || !e || e.pointerId !== drag.pid) return;
      drag = null;
      paneDragging = Math.max(0, paneDragging - 1);
    }
    bar.addEventListener("pointerup", endDrag);
    bar.addEventListener("pointercancel", endDrag);
  }

  var winGrow = null;
  var winGrowGen = 0;

  function canGrowWindow() {
    return !!(window.pywebview && window.pywebview.api && typeof window.pywebview.api.resize_to === "function");
  }

  function screenMax() {
    var x = window.screenX || 0;
    var y = window.screenY || 0;
    var aw = (window.screen && window.screen.availWidth) || 1920;
    var ah = (window.screen && window.screen.availHeight) || 1080;
    return {
      w: Math.max(window.outerWidth || 640, aw - Math.max(0, x)),
      h: Math.max(window.outerHeight || 480, ah - Math.max(0, y))
    };
  }

  function beginWinGrow() {
    winGrowGen += 1;
    if ((window.outerWidth || 0) < 200) {
      winGrow = null;
      return;
    }
    winGrow = {
      gen: winGrowGen,
      baseW: window.outerWidth,
      baseH: window.outerHeight,
      sentW: window.outerWidth,
      sentH: window.outerHeight,
      busy: false,
      queued: null,
      closed: false
    };
  }

  function endWinGrow() {
    if (!winGrow) return;
    winGrow.closed = true;
    if (winGrow.busy) return;
    if (winGrow.queued) {
      var next = winGrow.queued;
      winGrow.queued = null;
      requestWindowSize(next.w, next.h);
      return;
    }
    winGrow = null;
  }

  function requestWindowSize(w, h) {
    if (!winGrow || !canGrowWindow()) return;
    var session = winGrow;
    if (session.busy) {
      session.queued = { w: w, h: h };
      return;
    }
    session.busy = true;
    session.queued = null;
    var finish = function (res) {
      if (!winGrow || winGrow.gen !== session.gen) return;
      winGrow.busy = false;
      var retry = res && res.busy;
      var next = winGrow.queued;
      winGrow.queued = null;
      if (next) {
        if (retry) window.setTimeout(function () { requestWindowSize(next.w, next.h); }, 40);
        else requestWindowSize(next.w, next.h);
        return;
      }
      if (winGrow.closed) winGrow = null;
    };
    try {
      var ret = window.pywebview.api.resize_to(Math.round(w), Math.round(h));
      if (ret && typeof ret.then === "function") ret.then(finish, finish);
      else finish();
    } catch (err) {
      finish();
    }
  }

  function growBy(dw, dh) {
    if (!winGrow || winGrow.closed || !canGrowWindow()) return { dw: 0, dh: 0 };
    var max = screenMax();
    var w = Math.min(max.w, winGrow.baseW + Math.max(0, dw || 0));
    var h = Math.min(max.h, winGrow.baseH + Math.max(0, dh || 0));
    w = Math.max(winGrow.baseW, w);
    h = Math.max(winGrow.baseH, h);
    if (Math.abs(w - winGrow.sentW) >= 6 || Math.abs(h - winGrow.sentH) >= 6) {
      winGrow.sentW = w;
      winGrow.sentH = h;
      requestWindowSize(w, h);
    }
    return { dw: Math.max(0, w - winGrow.baseW), dh: Math.max(0, h - winGrow.baseH) };
  }

  function setRail(px) {
    var stage = document.getElementById("desk-stage");
    if (!stage) return;
    var next = Math.round(px) + "px";
    if (stage.style.getPropertyValue("--desk-rail-w").trim() !== next) {
      stage.style.setProperty("--desk-rail-w", next);
    }
  }

  function setHeight(el, h) {
    if (!el) return;
    var next = Math.round(h) + "px";
    if (el.style.height !== next) el.style.height = next;
  }

  function readOptional(key) {
    try {
      var raw = localStorage.getItem(key);
      if (raw == null || raw === "") return null;
      var v = parseFloat(raw);
      return isFinite(v) ? v : null;
    } catch (e) {
      return null;
    }
  }

  function usePageScroll() {
    document.documentElement.classList.add("desk-page-scroll");
    document.body.classList.add("desk-page-scroll");
  }

  function holdBlock(el, h) {
    if (!el) return;
    var px = Math.round(h) + "px";
    el.style.flexGrow = "0";
    el.style.flexShrink = "0";
    el.style.flexBasis = px;
    el.style.height = px;
    el.style.minHeight = px;
    el.style.maxHeight = "none";
    el.dataset.pinned = "1";
  }

  function releaseBlock(el) {
    if (!el) return;
    el.style.flexGrow = "";
    el.style.flexShrink = "";
    el.style.flexBasis = "";
    el.style.height = "";
    el.style.minHeight = "";
    el.style.maxHeight = "";
    if (el.dataset) delete el.dataset.pinned;
  }

  function keepBarVisible(bar) {
    if (!bar) return;
    var rect = bar.getBoundingClientRect();
    var bottom = window.innerHeight - 12;
    if (rect.bottom > bottom) window.scrollBy(0, rect.bottom - bottom);
    else if (rect.top < 12) window.scrollBy(0, rect.top - 12);
  }

  function dockedIn(el, parentId) {
    return !!(
      el &&
      !el.classList.contains("hidden") &&
      !el.classList.contains("is-float") &&
      el.parentNode &&
      el.parentNode.id === parentId
    );
  }

  function pinProgramColumn() {
    var alarm = document.getElementById("alarm-strip");
    var watch = document.getElementById("desk-watch");
    var open = document.getElementById("float-open");
    var remote = document.getElementById("float-remote");
    if (alarm && !alarm.classList.contains("hidden") && alarm.offsetHeight > 40) {
      var alarmH = Math.max(MIN_ALARM, alarm.offsetHeight);
      holdBlock(alarm, alarmH);
      saveNum(ALARM_H_KEY, alarmH);
    }
    if (watch && watch.offsetHeight > 40) {
      var watchH = Math.max(MIN_WATCH, watch.offsetHeight);
      holdBlock(watch, watchH);
      saveNum(WATCH_H_KEY, watchH);
    }
    if (open && !open.classList.contains("is-float") && open.offsetHeight > 40) {
      var openH = Math.max(MIN_RAIL, open.offsetHeight);
      holdBlock(open, openH);
      saveNum(OPEN_H_KEY, openH);
    }
    if (remote && !remote.classList.contains("hidden") && !remote.classList.contains("is-float") && remote.offsetHeight > 40) {
      var remoteH = Math.max(MIN_RAIL, remote.offsetHeight);
      holdBlock(remote, remoteH);
      saveNum(REMOTE_H_KEY, remoteH);
    }
  }

  function applyRail(px) {
    var stage = document.getElementById("desk-stage");
    if (!stage || window.innerWidth < 1100) return;
    var width = stage.getBoundingClientRect().width;
    var max = width > 80 ? Math.max(48, width - 62) : px;
    setRail(Math.min(max, Math.max(48, px)));
  }

  var FLOW_USER_KEY = "am.desk.flowUser";

  function userSized() {
    try { return localStorage.getItem(FLOW_USER_KEY) === "1"; } catch (e) { return false; }
  }

  function markUserSized() {
    try { localStorage.setItem(FLOW_USER_KEY, "1"); } catch (e) {}
  }

  function seedFlow() {
    if (document.body.classList.contains("am-manage")) return;
    var locked = userSized();
    var topbar = document.querySelector(".topbar");
    var now = document.getElementById("desk-now");
    var summary = document.getElementById("summary-cards");
    var alarm = document.getElementById("alarm-strip");
    var watch = document.getElementById("desk-watch");
    var open = document.getElementById("float-open");
    var remote = document.getElementById("float-remote");
    var snaps = document.getElementById("float-snaps");
    var server = document.getElementById("server-strip-wrap");
    var alarmOn = !!(alarm && !alarm.classList.contains("hidden") && alarm.parentNode && alarm.parentNode.id === "desk-prog");
    var openOn = !!(open && !open.classList.contains("is-float") && open.parentNode && open.parentNode.id === "desk-rail");
    var remoteOn = dockedIn(remote, "desk-rail");
    var snapsOn = !!(snaps && !snaps.classList.contains("is-float") && snaps.parentNode && snaps.parentNode.id === "view-dash");
    var serverOn = !!(server && !server.classList.contains("hidden"));
    var snapsH = locked ? readOptional(SNAPS_H_KEY) : 268;
    var serverH = locked ? readOptional(SERVER_H_KEY) : 120;
    if (!snapsOn) snapsH = 0;
    if (!serverOn) serverH = 0;
    if (snapsOn && snapsH == null) snapsH = 268;
    if (serverOn && serverH == null) serverH = 120;
    var topH = topbar ? topbar.offsetHeight : 52;
    if (now && !now.hidden) topH += now.offsetHeight;
    var summaryH = summary && summary.offsetHeight > 20 ? summary.offsetHeight : 72;
    var below = (snapsH || 0) + (serverH || 0) + 14 + (snapsOn ? 14 : 0) + (serverOn ? 14 : 0) + 20;
    var avail = window.innerHeight - topH - summaryH - below - 28;
    if (avail < 280) avail = 280;
    if (!locked) {
      var alarmH = alarmOn ? Math.max(MIN_ALARM, Math.round(avail * 0.26)) : 0;
      var watchH = Math.max(MIN_WATCH, avail - (alarmOn ? alarmH + 14 : 0));
      if (watch) saveNum(WATCH_H_KEY, watchH);
      if (alarmOn) saveNum(ALARM_H_KEY, alarmH);
      if (snapsOn) saveNum(SNAPS_H_KEY, snapsH);
      if (serverOn) saveNum(SERVER_H_KEY, serverH);
      var railAvail = (alarmOn ? alarmH + 14 : 0) + watchH;
      if (openOn) {
        var openH = remoteOn ? Math.max(MIN_RAIL, Math.round(railAvail * 0.46)) : Math.max(MIN_RAIL, railAvail);
        saveNum(OPEN_H_KEY, openH);
        if (remoteOn) saveNum(REMOTE_H_KEY, Math.max(MIN_RAIL, railAvail - openH - 14));
      }
      return;
    }
    if (watch && readOptional(WATCH_H_KEY) == null) {
      saveNum(WATCH_H_KEY, Math.max(MIN_WATCH, avail - (alarmOn ? Math.max(MIN_ALARM, Math.round(avail * 0.26)) + 14 : 0)));
    }
    if (alarmOn && readOptional(ALARM_H_KEY) == null) saveNum(ALARM_H_KEY, Math.max(MIN_ALARM, Math.round(avail * 0.26)));
    if (snapsOn && readOptional(SNAPS_H_KEY) == null) saveNum(SNAPS_H_KEY, 268);
    if (serverOn && readOptional(SERVER_H_KEY) == null) saveNum(SERVER_H_KEY, 120);
    if (openOn && readOptional(OPEN_H_KEY) == null) {
      var savedWatch = readOptional(WATCH_H_KEY) || MIN_WATCH;
      var savedAlarm = alarmOn ? (readOptional(ALARM_H_KEY) || MIN_ALARM) + 14 : 0;
      var railNow = savedAlarm + savedWatch;
      saveNum(OPEN_H_KEY, remoteOn ? Math.max(MIN_RAIL, Math.round(railNow * 0.46)) : Math.max(MIN_RAIL, railNow));
    }
    if (remoteOn && readOptional(REMOTE_H_KEY) == null) {
      var openNow = readOptional(OPEN_H_KEY) || MIN_RAIL;
      var savedWatch2 = readOptional(WATCH_H_KEY) || MIN_WATCH;
      var savedAlarm2 = alarmOn ? (readOptional(ALARM_H_KEY) || MIN_ALARM) + 14 : 0;
      saveNum(REMOTE_H_KEY, Math.max(MIN_RAIL, savedAlarm2 + savedWatch2 - openNow - 14));
    }
  }

  function applyFlow() {
    var alarm = document.getElementById("alarm-strip");
    var watch = document.getElementById("desk-watch");
    var open = document.getElementById("float-open");
    var remote = document.getElementById("float-remote");
    var snaps = document.getElementById("float-snaps");
    var server = document.getElementById("server-strip-wrap");
    var alarmOn = !!(alarm && watch && !alarm.classList.contains("hidden") && alarm.parentNode && alarm.parentNode.id === "desk-prog");
    var abar = document.getElementById("desk-split-alarm");
    if (abar) abar.hidden = !alarmOn;
    if (alarmOn) {
      var alarmH = readOptional(ALARM_H_KEY);
      if (alarmH != null) holdBlock(alarm, Math.max(MIN_ALARM, alarmH));
    }
    var wbar = document.getElementById("desk-split-watch");
    if (wbar) wbar.hidden = !watch;
    if (watch && watch.parentNode && watch.parentNode.id === "desk-prog") {
      var watchH = readOptional(WATCH_H_KEY);
      if (watchH != null) holdBlock(watch, Math.max(MIN_WATCH, watchH));
    }
    var openOn = !!(open && !open.classList.contains("is-float") && open.parentNode && open.parentNode.id === "desk-rail");
    var rbar = document.getElementById("desk-rail-split");
    if (rbar) rbar.hidden = !openOn;
    if (openOn) {
      var openH = readOptional(OPEN_H_KEY);
      if (openH != null) holdBlock(open, Math.max(MIN_RAIL, openH));
    } else if (open && open.classList.contains("is-float")) {
      releaseBlock(open);
    }
    var remoteOn = dockedIn(remote, "desk-rail");
    var remoteBar = document.getElementById("desk-split-remote");
    if (remoteBar) remoteBar.hidden = !remoteOn;
    if (remoteOn) {
      var remoteH = readOptional(REMOTE_H_KEY);
      if (remoteH != null) holdBlock(remote, Math.max(MIN_RAIL, remoteH));
    } else if (remote && remote.classList.contains("is-float")) {
      releaseBlock(remote);
    }
    var snapsOn = !!(snaps && !snaps.classList.contains("is-float") && snaps.parentNode && snaps.parentNode.id === "view-dash");
    var sbar = document.getElementById("desk-split-snaps");
    if (sbar) sbar.hidden = !snapsOn;
    if (snapsOn) holdBlock(snaps, Math.max(MIN_SNAPS, readNum(SNAPS_H_KEY, 268, MIN_SNAPS, null)));
    else if (snaps && snaps.classList.contains("is-float")) releaseBlock(snaps);
    var serverOn = !!(server && !server.classList.contains("hidden"));
    var pbar = document.getElementById("desk-split-server");
    if (pbar) pbar.hidden = !serverOn;
    if (serverOn) {
      var serverH = readOptional(SERVER_H_KEY);
      if (serverH != null) {
        holdBlock(server, Math.max(MIN_SERVER, serverH));
        if (!server.classList.contains("desk-sized")) server.classList.add("desk-sized");
      }
    }
  }

  function trimToFit() {
    if (userSized() || document.body.classList.contains("am-manage")) return;
    var extra = document.documentElement.scrollHeight - window.innerHeight;
    if (extra <= 1) return;
    var cut = extra + 8;
    var watch = document.getElementById("desk-watch");
    if (watch && watch.parentNode && watch.parentNode.id === "desk-prog") {
      var wh = Math.max(MIN_WATCH, watch.offsetHeight - cut);
      holdBlock(watch, wh);
      saveNum(WATCH_H_KEY, wh);
    }
    var open = document.getElementById("float-open");
    var remote = document.getElementById("float-remote");
    var openOn = !!(open && !open.classList.contains("is-float") && open.parentNode && open.parentNode.id === "desk-rail");
    var remoteOn = dockedIn(remote, "desk-rail");
    var left = cut;
    if (remoteOn) {
      var rh = Math.max(MIN_RAIL, remote.offsetHeight - left);
      left -= remote.offsetHeight - rh;
      holdBlock(remote, rh);
      saveNum(REMOTE_H_KEY, rh);
    }
    if (openOn && left > 0) {
      var oh = Math.max(MIN_RAIL, open.offsetHeight - left);
      holdBlock(open, oh);
      saveNum(OPEN_H_KEY, oh);
    }
  }

  var paneLock = false;

  function syncPanes() {
    if (paneLock || paneDragging) return;
    paneLock = true;
    try {
    var vbar = document.getElementById("desk-split-v");
    var narrow = window.innerWidth < 1100;
    if (vbar) vbar.hidden = narrow;
    if (!narrow) applyRail(readNum(RAIL_W_KEY, 320, 120, null));
    syncSplit();
    seedFlow();
    applyFlow();
    trimToFit();
    } finally {
      paneLock = false;
    }
  }

  function mountPanes() {
    var stage = document.getElementById("desk-stage");
    var rail = document.getElementById("desk-rail");
    if (stage && rail && !document.getElementById("desk-split-v")) {
      var vbar = makeSplit("desk-split-v", true, "左右拖动，改变右侧告警栏宽度");
      stage.insertBefore(vbar, rail);
      bindSplit(
        vbar,
        function () {
          var rect = stage.getBoundingClientRect();
          var railRect = rail.getBoundingClientRect();
          var main = stage.querySelector(".desk-main");
          return {
            right: rect.right,
            railW: railRect.width,
            mainW: main ? main.getBoundingClientRect().width : Math.max(MIN_MAIN, rect.width - railRect.width - 14)
          };
        },
        function (e, drag) {
          var want = drag.right - e.clientX - 7;
          var give = Math.max(0, drag.mainW - MIN_MAIN);
          var w = Math.min(Math.max(120, want), drag.railW + give);
          setRail(w);
          saveNum(RAIL_W_KEY, w);
        }
      );
    }

    var prog = document.getElementById("desk-prog");
    var watch = document.getElementById("desk-watch");
    if (prog && watch && !document.getElementById("desk-split-alarm")) {
      var abar = makeSplit("desk-split-alarm", false, "往下拉，异常节目变高，下面的内容下移；往上推则上移。超出窗口出现滚动条，缩回放得下滚动条消失");
      prog.insertBefore(abar, watch);
      bindSplit(
        abar,
        function () {
          var alarm = document.getElementById("alarm-strip");
          return { h: alarm ? alarm.offsetHeight : MIN_ALARM };
        },
        function (e, drag) {
          var alarm = document.getElementById("alarm-strip");
          if (!alarm) return;
          var want = Math.max(MIN_ALARM, drag.h + (e.clientY - drag.y0));
          holdBlock(alarm, want);
          saveNum(ALARM_H_KEY, want);
          markUserSized();
          keepBarVisible(abar);
        }
      );
    }

    mountSplit();

    var dash = document.getElementById("view-dash");
    var snaps = document.getElementById("float-snaps");
    var servers = document.getElementById("server-strip-wrap");
    if (dash && watch && !document.getElementById("desk-split-watch")) {
      var wbar = makeSplit("desk-split-watch", false, "往下拉，监测中变高，下面的截图和服务器下移；往上推则上移。超出窗口出现滚动条，缩回放得下滚动条消失");
      var watchAnchor = snaps && snaps.parentNode === dash ? snaps : servers;
      if (watchAnchor && watchAnchor.parentNode === dash) dash.insertBefore(wbar, watchAnchor);
      else dash.appendChild(wbar);
      bindSplit(
        wbar,
        function () {
          return { h: watch.offsetHeight || MIN_WATCH };
        },
        function (e, drag) {
          var want = Math.max(MIN_WATCH, drag.h + (e.clientY - drag.y0));
          holdBlock(watch, want);
          saveNum(WATCH_H_KEY, want);
          markUserSized();
          keepBarVisible(wbar);
        }
      );
    }
    if (dash && !document.getElementById("desk-split-snaps")) {
      var sbar = makeSplit("desk-split-snaps", false, "往下拉，截图变高，下面的服务器下移；往上推则上移。超出窗口出现滚动条，缩回放得下滚动条消失");
      if (servers && servers.parentNode === dash) dash.insertBefore(sbar, servers);
      else dash.appendChild(sbar);
      bindSplit(
        sbar,
        function () {
          return { h: snaps ? snaps.offsetHeight : 268 };
        },
        function (e, drag) {
          if (!snaps) return;
          var want = Math.max(MIN_SNAPS, drag.h + (e.clientY - drag.y0));
          holdBlock(snaps, want);
          saveNum(SNAPS_H_KEY, want);
          markUserSized();
          keepBarVisible(sbar);
        }
      );
    }
    if (dash && servers && !document.getElementById("desk-split-server")) {
      var pbar = makeSplit("desk-split-server", false, "往下拉，服务器性能变高；往上推则变矮。超出窗口右边出现滚动条，缩回放得下滚动条消失");
      if (servers.nextSibling) dash.insertBefore(pbar, servers.nextSibling);
      else dash.appendChild(pbar);
      bindSplit(
        pbar,
        function () {
          return { h: servers.offsetHeight || MIN_SERVER };
        },
        function (e, drag) {
          var want = Math.max(MIN_SERVER, drag.h + (e.clientY - drag.y0));
          holdBlock(servers, want);
          if (!servers.classList.contains("desk-sized")) servers.classList.add("desk-sized");
          saveNum(SERVER_H_KEY, want);
          markUserSized();
          keepBarVisible(pbar);
        }
      );
    }
    syncPanes();
    if (!window.__deskPaneResize) {
      window.__deskPaneResize = true;
      var paneResizeTimer = 0;
      window.addEventListener("resize", function () {
        if (paneDragging) return;
        window.clearTimeout(paneResizeTimer);
        paneResizeTimer = window.setTimeout(syncPanes, 200);
      });
    }
  }

  function layoutWall() {
    document.body.classList.add("am-desktop");
    var manageBtn = document.getElementById("btn-view-manage");
    if (manageBtn) manageBtn.textContent = "配置";
    var brand = document.querySelector(".brand");
    var toggle = document.querySelector(".view-toggle");
    var topbar = document.querySelector(".topbar");
    if (brand && toggle && toggle.parentNode !== topbar) {
      topbar.insertBefore(toggle, brand.nextSibling);
    } else if (brand && toggle && brand.nextSibling !== toggle) {
      topbar.insertBefore(toggle, brand.nextSibling);
    }
    if (topbar && !document.getElementById("desk-pills")) {
      var pills = document.createElement("div");
      pills.id = "desk-pills";
      pills.className = "desk-pills";
      var anchor = toggle ? toggle.nextSibling : brand && brand.nextSibling;
      topbar.insertBefore(pills, anchor);
    }
    var sound = document.getElementById("sw-sound");
    var soundLabel = sound && sound.closest("label");
    if (soundLabel && !document.getElementById("desk-sound-test")) {
      var test = document.createElement("button");
      test.id = "desk-sound-test";
      test.type = "button";
      test.className = "btn";
      test.textContent = "试告警";
      test.title = "听一下告警提示音";
      test.addEventListener("click", playAlarmTest);
      soundLabel.parentNode.insertBefore(test, soundLabel);
    }
    ["sw-tts", "sw-desktop", "btn-tts-test", "btn-tts-clear", "auto-refresh"].forEach(function (id) {
      var el = document.getElementById(id);
      if (!el) return;
      (el.closest("label") || el).classList.add("desk-aux");
    });
    if (topbar && !document.getElementById("desk-now")) {
      var now = document.createElement("div");
      now.id = "desk-now";
      now.className = "desk-now";
      now.hidden = true;
      now.innerHTML =
        '<span>正在播放</span><b id="desk-now-name"></b><span id="desk-now-node"></span><span id="desk-now-addr"></span><span id="desk-now-prog"></span><span id="desk-now-iface"></span><span id="desk-now-rate"></span>';
      topbar.parentNode.insertBefore(now, topbar.nextSibling);
    }
    var aiCard = document.querySelector("#summary-cards .card:nth-child(4)");
    if (aiCard && !aiCard.id) aiCard.id = "card-ai";
    var enabledLabel = document.querySelector("#card-enabled .card-label");
    var alarmLabel = document.querySelector("#card-alarm .card-label");
    if (!document.getElementById("card-offline")) {
      var offline = document.createElement("div");
      offline.className = "card";
      offline.id = "card-offline";
      offline.innerHTML =
        '<div class="card-label">离线 / 无信号</div><div class="card-value" id="stat-offline">0</div>';
      var alarmCard = document.getElementById("card-alarm");
      if (alarmCard && alarmCard.parentNode) alarmCard.parentNode.insertBefore(offline, alarmCard.nextSibling);
    }
    document.querySelectorAll("#summary-cards .card").forEach(function (el) {
      el.classList.remove("clickable");
      el.removeAttribute("data-goto");
      el.removeAttribute("title");
    });
    var dash = document.getElementById("view-dash");
    if (dash && !document.getElementById("desk-stage")) {
      var firstHead = dash.querySelector(":scope > .panel-head");
      if (firstHead) firstHead.classList.add("desk-web-only");
      var legend = dash.querySelector(":scope > .lamp-legend");
      if (legend) legend.classList.add("desk-web-only");
      dash.querySelectorAll(":scope > .panel-head").forEach(function (head) {
        var title = head.querySelector("h2");
        if (title && title.textContent.indexOf("24h") >= 0) head.classList.add("desk-web-only");
      });
      var hist = dash.querySelector(":scope > .hist-wrap");
      if (hist) hist.classList.add("desk-web-only");
      var stage = document.createElement("div");
      stage.id = "desk-stage";
      stage.className = "desk-stage";
      var main = document.createElement("div");
      main.className = "desk-main";
      var rail = document.createElement("aside");
      rail.id = "desk-rail";
      rail.className = "desk-rail";
      stage.appendChild(main);
      stage.appendChild(rail);
      var alarmStrip = document.getElementById("alarm-strip");
      if (alarmStrip && alarmStrip.parentNode) alarmStrip.parentNode.insertBefore(stage, alarmStrip);
      var summary = document.getElementById("summary-cards");
      if (summary) main.appendChild(summary);
      var prog = document.createElement("div");
      prog.id = "desk-prog";
      prog.className = "desk-prog";
      var watch = document.createElement("div");
      watch.id = "desk-watch";
      watch.className = "desk-watch";
      main.appendChild(prog);
      if (alarmStrip) prog.appendChild(alarmStrip);
      [document.getElementById("cat-tabs"), document.querySelector(".cat-tab-hint"), document.getElementById("ok-strip-head"), document.getElementById("channel-grid")].forEach(function (el) {
        if (el) watch.appendChild(el);
      });
      prog.appendChild(watch);
      ["float-open", "float-remote"].forEach(function (id) {
        var panel = document.getElementById(id);
        if (!panel) return;
        if (panel.classList.contains("is-float")) {
          var ph = document.querySelector('.float-placeholder[data-for="' + id + '"]');
          if (ph) rail.appendChild(ph);
          return;
        }
        rail.appendChild(panel);
      });
      var servers = document.getElementById("server-strip-wrap");
      if (servers) dash.appendChild(servers);
      var snaps = document.getElementById("float-snaps");
      if (snaps && servers && servers.parentNode) {
        if (snaps.classList.contains("is-float")) {
          var sph = document.querySelector('.float-placeholder[data-for="float-snaps"]');
          if (sph) servers.parentNode.insertBefore(sph, servers);
        } else {
          servers.parentNode.insertBefore(snaps, servers);
        }
      }
    }
    var alarmHint = document.querySelector("#alarm-strip .hint");
    if (alarmHint) alarmHint.textContent = "置顶放大 · 点画面播放这一路";
    var okHint = document.getElementById("ok-strip-hint");
    if (okHint && okHint.textContent.indexOf("尚未保存") < 0) {
      okHint.textContent = "三台节目排在一起 · 拖动后点保存排列";
    }
    var catHint = document.querySelector(".cat-tab-hint");
    if (catHint) catHint.textContent = "分类可多选，点中的要同时满足。";
    var recentTitle = document.querySelector("#remote-events-head h2");
    if (recentTitle) recentTitle.textContent = "最近告警";
    var recentHint = document.querySelector("#remote-events-head .hint");
    if (recentHint) recentHint.textContent = "点一条看当时的画面";
    var openHint = document.querySelector("#float-open .hint");
    if (openHint) openHint.textContent = "点一条看这一路的实时画面";
    var snapHint = document.querySelector("#float-snaps .panel-head .hint");
    if (snapHint) snapHint.textContent = "告警当时的画面。点一张放大";
    mountPanes();
    ["float-open", "float-remote", "float-snaps"].forEach(function (id) {
      var panel = document.getElementById(id);
      var head = panel && panel.querySelector(":scope > .panel-head");
      if (head) head.title = "按住标题拖动，不会拖出这个窗口";
    });

    function syncStats() {
      var onManage = document.body.classList.contains("am-manage");
      if (enabledLabel) enabledLabel.textContent = onManage ? "已启用" : "正常";
      if (alarmLabel) alarmLabel.textContent = onManage ? "异常频道" : "异常";
      if (!onManage) {
        var green = document.getElementById("sum-green");
        var red = document.getElementById("sum-red");
        var yellow = document.getElementById("sum-yellow");
        var statEnabled = document.getElementById("stat-enabled");
        var statAlarm = document.getElementById("stat-alarm");
        var statOffline = document.getElementById("stat-offline");
        if (statEnabled && green) statEnabled.textContent = green.textContent;
        if (statAlarm && red) statAlarm.textContent = red.textContent;
        if (statOffline && yellow) statOffline.textContent = yellow.textContent;
      }
      var pills = document.getElementById("desk-pills");
      var strip = document.getElementById("node-strip");
      if (!pills || !strip) return;
      var html = "";
      strip.querySelectorAll(".server-card").forEach(function (card) {
        var name = card.querySelector("b");
        var count = card.querySelector(".server-card-top span");
        var bad = card.classList.contains("bad");
        html +=
          '<span class="desk-pill"><i class="dot ' +
          (bad ? "red" : "green") +
          '"></i>' +
          (name ? name.textContent : "") +
          " · " +
          (count ? count.textContent : "") +
          "</span>";
      });
      if (pills.getAttribute("data-html") !== html) {
        pills.setAttribute("data-html", html);
        pills.innerHTML = html;
      }
      var recentTitle = document.querySelector("#remote-events-head h2");
      if (recentTitle && recentTitle.textContent !== "最近告警") recentTitle.textContent = "最近告警";
      var recentHint = document.querySelector("#remote-events-head .hint");
      if (recentHint && recentHint.textContent !== "点一条看当时的画面") {
        recentHint.textContent = "点一条看当时的画面";
      }
    }
    var manage = document.getElementById("view-manage");
    if (manage && window.MutationObserver) {
      new MutationObserver(function () {
        document.body.classList.toggle("am-manage", !manage.classList.contains("hidden"));
        syncStats();
      }).observe(manage, { attributes: true, attributeFilter: ["class"] });
    }
    var remotePanel = document.getElementById("float-remote");
    var openPanel = document.getElementById("float-open");
    var alarmPanel = document.getElementById("alarm-strip");
    var serverPanel = document.getElementById("server-strip-wrap");
    var snapsPanel = document.getElementById("float-snaps");
    if (window.MutationObserver) {
      var watchSplit = new MutationObserver(syncPanes);
      [remotePanel, openPanel, alarmPanel, serverPanel, snapsPanel].forEach(function (el) {
        if (el) watchSplit.observe(el, { attributes: true, attributeFilter: ["class"] });
      });
    }
    syncStats();
    setInterval(syncStats, 1000);
  }

  function tuneCopy() {
    var sub = document.querySelector(".sub");
    if (sub) sub.textContent = "本机汇总 · 监测在 173 / 176 / 177";
    var hint = document.querySelector("#hub-editor .hint");
    if (hint) {
      hint.textContent =
        "三台都是监测机。地址写 http://IP:8080，不要留空。节目排列保存在这台电脑，不写进各台的频道表。分类仍写在节目所属的那一台。";
    }
    document.querySelectorAll("#hub-rows .hub-url").forEach(function (el) {
      el.placeholder = "http://10.6.9.173:8080";
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    layoutWall();
    mountChannelScope();
    decorateChannelIds();
    var tbody = document.getElementById("channel-tbody");
    if (tbody && window.MutationObserver) {
      new MutationObserver(decorateChannelIds).observe(tbody, { childList: true });
    }
    pollPlay();
    setInterval(pollPlay, 1000);
    var closeBtn = document.getElementById("preview-close");
    if (closeBtn) closeBtn.addEventListener("click", stopSnap);
    fetch("/api/hub/nodes")
      .then(function (r) {
        return r.json();
      })
      .then(function (data) {
        buildBar((data && data.nodes) || []);
        tuneCopy();
      })
      .catch(function () {
        buildBar([
          { id: "n173", name: "173" },
          { id: "n176", name: "176" },
          { id: "n177", name: "177" },
        ]);
        tuneCopy();
      });
    var rows = document.getElementById("hub-rows");
    if (rows && window.MutationObserver) {
      new MutationObserver(tuneCopy).observe(rows, { childList: true });
    }
  });
})();
