/* 大屏标签筛选。多选表示同时满足：点中「高清」和「卫视」只留两样都有的节目。 */
(function (root, factory) {
  const api = factory();
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (root) root.CategoryFilter = api;
})(typeof window !== "undefined" ? window : globalThis, function () {
  function cardTags(c) {
    const raw = c && c.category;
    if (Array.isArray(raw)) {
      return raw.map((x) => String(x || "").trim()).filter(Boolean);
    }
    const text = String(raw || "").trim();
    if (!text) return [];
    return text.split(/[、,，;；\s]+/).filter(Boolean);
  }

  function toggleDashCategories(selected, cat) {
    const cur = (selected || []).slice();
    if (!cat) return [];
    if (cat === "__none__") {
      if (cur.length === 1 && cur[0] === "__none__") return [];
      return ["__none__"];
    }
    const next = cur.filter((x) => x !== "__none__");
    const i = next.indexOf(cat);
    if (i >= 0) next.splice(i, 1);
    else next.push(cat);
    return next;
  }

  function cardMatchesCategories(c, selected) {
    if (!selected || !selected.length) return true;
    const tags = cardTags(c);
    if (selected.indexOf("__none__") >= 0) return tags.length === 0;
    return selected.every((name) => tags.indexOf(name) >= 0);
  }

  function pruneDashCategories(selected, names, uncat) {
    const known = names || [];
    return (selected || []).filter((cat) => {
      if (cat === "__none__") return !!uncat;
      return known.indexOf(cat) >= 0;
    });
  }

  return {
    cardTags: cardTags,
    toggleDashCategories: toggleDashCategories,
    cardMatchesCategories: cardMatchesCategories,
    pruneDashCategories: pruneDashCategories,
  };
});
