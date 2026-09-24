/* A minimal DOM, enough to run the viewer's own script and see what it renders.
 *
 * Why not jsdom: it is a dependency, and the viewer's whole claim is that it needs none. A
 * shim small enough to read is also a statement of exactly which DOM surface the viewer is
 * allowed to use — anything it reaches for that is not here fails loudly rather than working
 * on the author's browser and nowhere else.
 *
 * Usage: node tests/viewer_dom.js <rendered.html>  ->  prints the rendered DOM as text.
 */

"use strict";

var fs = require("fs");
var vm = require("vm");

var VOID = { br: 1, hr: 1, img: 1, input: 1, meta: 1, link: 1 };

function Node(tag, ns) {
  this.tagName = tag;
  this.ns = ns || null;
  this.children = [];
  this.attributes = {};
  this.className = "";
  this._text = null;
  this.style = {};
  this.listeners = {};
  this.parentNode = null;
}

Node.prototype.appendChild = function (child) {
  if (child == null) throw new Error("appendChild(null) in " + this.tagName);
  child.parentNode = this;
  // A real node holds either text or children, and appending replaces the text. Without
  // this, `panel.textContent = ""` before a re-render would make every later child
  // invisible to the serialiser — which reads as "the tab rendered nothing".
  this._text = null;
  this.children.push(child);
  return child;
};

Node.prototype.setAttribute = function (name, value) {
  this.attributes[name] = String(value);
};

Node.prototype.addEventListener = function (name, fn) {
  (this.listeners[name] = this.listeners[name] || []).push(fn);
};

Node.prototype.click = function () {
  (this.listeners.click || []).forEach(function (fn) { fn.call(this); }, this);
};

Object.defineProperty(Node.prototype, "textContent", {
  get: function () {
    if (this._text != null) return this._text;
    return this.children.map(function (c) { return c.textContent; }).join("");
  },
  set: function (value) {
    this.children = [];
    this._text = String(value);
  },
});

// The rule the viewer is built on. If it ever reaches for innerHTML the shim says so.
Object.defineProperty(Node.prototype, "innerHTML", {
  get: function () { throw new Error("the viewer read innerHTML"); },
  set: function () { throw new Error("the viewer assigned to innerHTML"); },
});

function TextNode(value) {
  this._text = String(value);
  this.tagName = "#text";
  this.children = [];
  this.attributes = {};
}
TextNode.prototype = Object.create(Node.prototype);

function walk(node, visit) {
  visit(node);
  (node.children || []).forEach(function (child) { walk(child, visit); });
}

function matches(node, selector) {
  // Only the two selector shapes the viewer uses: ".cls" and ".cls tag".
  var parts = selector.trim().split(/\s+/);
  var last = parts[parts.length - 1];
  var hit = last.charAt(0) === "."
    ? (" " + node.className + " ").indexOf(" " + last.slice(1) + " ") !== -1
    : node.tagName === last;
  if (!hit || parts.length === 1) return hit;
  var wanted = parts[0].slice(1);
  for (var p = node.parentNode; p; p = p.parentNode) {
    if ((" " + p.className + " ").indexOf(" " + wanted + " ") !== -1) return true;
  }
  return false;
}

function makeDocument(blob) {
  var byId = {};
  ["runline", "tabs", "panel", "warnings"].forEach(function (id) {
    byId[id] = new Node("div");
  });
  var data = new Node("script");
  data.textContent = blob;
  byId["trace-data"] = data;

  var root = new Node("body");
  Object.keys(byId).forEach(function (id) { root.appendChild(byId[id]); });

  return {
    root: root,
    getElementById: function (id) { return byId[id] || null; },
    createElement: function (tag) { return new Node(tag); },
    createElementNS: function (ns, tag) { return new Node(tag, ns); },
    createTextNode: function (value) { return new TextNode(value); },
    querySelectorAll: function (selector) {
      var out = [];
      walk(root, function (node) { if (matches(node, selector)) out.push(node); });
      return out;
    },
  };
}

function escape(text) {
  // Text is escaped on the way out so the serialisation can tell an *element* the viewer
  // built from a *string* it rendered. Without this, a trace containing "<script>" and a
  // viewer that injected a real <script> serialise identically — which is the one
  // distinction the escaping test exists to make.
  return String(text).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function serialise(node, depth) {
  depth = depth || 0;
  if (node.tagName === "#text") return escape(node._text);
  var attrs = Object.keys(node.attributes)
    .map(function (k) { return " " + k + '="' + node.attributes[k] + '"'; })
    .join("");
  var cls = node.className ? ' class="' + node.className + '"' : "";
  var open = "<" + node.tagName + cls + attrs + ">";
  if (VOID[node.tagName]) return open;
  var inner = node._text != null
    ? escape(node._text)
    : node.children.map(function (c) { return serialise(c, depth + 1); }).join("");
  return open + inner + "</" + node.tagName + ">";
}

// -- run -------------------------------------------------------------------------------------

var html = fs.readFileSync(process.argv[2], "utf8");
var blob = html.match(
  /<script type="application\/json" id="trace-data">([\s\S]*?)<\/script>/
)[1];
var source = html.match(/<script>\n([\s\S]*?)\n<\/script>\n<\/body>/)[1];

var document = makeDocument(blob);
var sandbox = {
  document: document,
  localStorage: {
    _v: {},
    getItem: function (k) { return k in this._v ? this._v[k] : null; },
    setItem: function (k, v) { this._v[k] = String(v); },
  },
  console: console,
  JSON: JSON,
  Math: Math,
  Number: Number,
  String: String,
  Array: Array,
  Object: Object,
  isNaN: isNaN,
  parseInt: parseInt,
};
sandbox.window = sandbox;

vm.createContext(sandbox);
new vm.Script(source, { filename: "viewer.js" }).runInContext(sandbox);

// Render every tab, so one broken tab cannot hide behind the default.
var tabs = document.getElementById("tabs").children;
var out = { tabs: tabs.map(function (t) { return t.textContent; }), panels: {} };
out.header = serialise(document.getElementById("runline"));
tabs.forEach(function (tab, i) {
  tab.click();
  out.panels[tab.textContent] = serialise(document.getElementById("panel"));
});
process.stdout.write(JSON.stringify(out));
