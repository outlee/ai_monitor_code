"use strict";

const assert = require("assert");
const {
  cardTags,
  toggleDashCategories,
  cardMatchesCategories,
  pruneDashCategories,
} = require("../web/static/category_filter.js");

const hdSat = { name: "卫视高清", category: ["卫视", "高清"] };
const sdSat = { name: "卫视标清", category: ["卫视", "标清"] };
const cctv = { name: "央视高清", category: "央视、高清" };
const plain = { name: "未分类", category: [] };
const alarm = { name: "异常卫视高清", category: ["卫视", "高清"], lamp: "red" };

let sel = [];
sel = toggleDashCategories(sel, "高清");
assert.deepStrictEqual(sel, ["高清"]);
assert.strictEqual(cardMatchesCategories(hdSat, sel), true);
assert.strictEqual(cardMatchesCategories(cctv, sel), true);
assert.strictEqual(cardMatchesCategories(sdSat, sel), false);

sel = toggleDashCategories(sel, "卫视");
assert.deepStrictEqual(sel, ["高清", "卫视"]);
assert.strictEqual(cardMatchesCategories(hdSat, sel), true);
assert.strictEqual(cardMatchesCategories(alarm, sel), true);
assert.strictEqual(cardMatchesCategories(sdSat, sel), false);
assert.strictEqual(cardMatchesCategories(cctv, sel), false);

sel = toggleDashCategories(sel, "高清");
sel = toggleDashCategories(sel, "标清");
assert.deepStrictEqual(sel, ["卫视", "标清"]);
assert.strictEqual(cardMatchesCategories(sdSat, sel), true);
assert.strictEqual(cardMatchesCategories(hdSat, sel), false);

assert.deepStrictEqual(toggleDashCategories(["高清"], "高清"), []);
assert.strictEqual(cardMatchesCategories(sdSat, []), true);
assert.deepStrictEqual(toggleDashCategories(sel, ""), []);

assert.deepStrictEqual(toggleDashCategories(["高清", "卫视"], "__none__"), ["__none__"]);
assert.strictEqual(cardMatchesCategories(plain, ["__none__"]), true);
assert.strictEqual(cardMatchesCategories(hdSat, ["__none__"]), false);
assert.deepStrictEqual(toggleDashCategories(["__none__"], "卫视"), ["卫视"]);
assert.deepStrictEqual(toggleDashCategories(["__none__"], "__none__"), []);

assert.deepStrictEqual(cardTags(cctv), ["央视", "高清"]);
assert.deepStrictEqual(
  pruneDashCategories(["高清", "没有了", "__none__"], ["卫视", "高清"], false),
  ["高清"]
);

console.log("category filter ok");
