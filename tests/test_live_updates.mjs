import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import test from "node:test";
import vm from "node:vm";
import {webcrypto} from "node:crypto";

// A small DOM fixture keeps the real render paths testable without a browser dependency.
class TextNode {
  nodeType = 3;
  constructor(text) { this.nodeValue = String(text); }
  get textContent() { return this.nodeValue; }
  appendData(text) { this.nodeValue += text; }
  remove() { this.parentNode?.removeChild(this); }
}
class Element {
  nodeType = 1; childNodes = []; attrs = new Map(); onclick = null; value = "";
  _scrollTop = 0; scrollLeft = 0; clientHeight = 220;
  constructor(tag) {
    this.tagName = tag.toUpperCase();
    this.dataset = new Proxy({}, {get: (_, key) => this.getAttribute("data-" + key), set: (_, key, value) => { this.setAttribute("data-" + key, value); return true; }});
    this.classList = {add: (...values) => { this.className = [...new Set([...this.className.split(" "), ...values])].filter(Boolean).join(" "); },
      remove: (...values) => { this.className = this.className.split(" ").filter(value => !values.includes(value)).join(" "); },
      toggle: (value, enabled) => { const present = this.className.split(" ").includes(value); (enabled ?? !present) ? this.classList.add(value) : this.classList.remove(value); }};
  }
  get children() { return this.childNodes.filter(node => node.nodeType === 1); }
  get options() { return this.children.filter(node => node.tagName === "OPTION"); }
  get firstChild() { return this.childNodes[0] || null; }
  get attributes() { return [...this.attrs].map(([name, value]) => ({name, value})); }
  get className() { return this.getAttribute("class") || ""; }
  set className(value) { this.setAttribute("class", value); }
  get disabled() { return this.hasAttribute("disabled"); }
  set disabled(value) { value ? this.setAttribute("disabled", "") : this.removeAttribute("disabled"); }
  get textContent() { return this.childNodes.map(node => node.textContent).join(""); }
  set textContent(text) { this.replaceChildren(new TextNode(text)); }
  get scrollHeight() { return Math.max(this.clientHeight, this.textContent.split("\n").length * 22); }
  get scrollTop() { return this._scrollTop; }
  set scrollTop(value) { this._scrollTop = Math.max(0, Math.min(value, this.scrollHeight - this.clientHeight)); }
  setAttribute(name, value) { this.attrs.set(name, String(value)); }
  getAttribute(name) { return this.attrs.get(name) ?? null; }
  hasAttribute(name) { return this.attrs.has(name); }
  removeAttribute(name) { this.attrs.delete(name); }
  addEventListener(name, fn) { this["on" + name] = fn; }
  dispatchEvent(event) { this["on" + event.type]?.(event); return true; }
  append(...nodes) { for (let node of nodes) { if (typeof node === "string") node = new TextNode(node); this.insertBefore(node, null); } }
  prepend(...nodes) { const first = this.firstChild; for (let node of nodes) { if (typeof node === "string") node = new TextNode(node); this.insertBefore(node, first); } }
  insertBefore(node, reference) { node.remove(); const index = reference ? this.childNodes.indexOf(reference) : this.childNodes.length; this.childNodes.splice(index, 0, node); node.parentNode = this; }
  removeChild(node) { this.childNodes.splice(this.childNodes.indexOf(node), 1); node.parentNode = null; }
  replaceChildren(...nodes) { for (const node of [...this.childNodes]) this.removeChild(node); this.append(...nodes); }
  remove() { this.parentNode?.removeChild(this); }
  cloneNode(deep = false) {
    const copy = new Element(this.tagName); copy.value = this.value;
    for (const [name, value] of this.attrs) copy.setAttribute(name, value);
    if (deep) for (const child of this.childNodes) copy.append(child.nodeType === 1 ? child.cloneNode(true) : new TextNode(child.textContent));
    return copy;
  }
  querySelector(selector) { return find(this, selector); }
  querySelectorAll(selector) {
    const [tag, pseudo] = selector.split(":"); const result = [];
    const attribute = selector.match(/^\[([^\]]+)\]$/)?.[1];
    const visit = element => { for (const child of element.children) { if ((attribute ? child.hasAttribute(attribute) : child.tagName === tag.toUpperCase() && (pseudo !== "checked" || child.checked))) result.push(child); visit(child); } };
    visit(this); return result;
  }
}

function dashboard() {
  const elements = new Map();
  const clipboard = [];
  const document = {createElement: tag => new Element(tag), createTextNode: text => new TextNode(text),
    getElementById: id => { if (!elements.has(id)) elements.set(id, new Element("div")); return elements.get(id); },
    querySelector: () => ({content: "test-token"})};
  const context = vm.createContext({document, setTimeout, clearTimeout, console, Date, Map, TextEncoder,
    Event, crypto: webcrypto, navigator: {clipboard: {writeText: async text => clipboard.push(text)}}});
  vm.runInContext(readFileSync(new URL("../appletart/static/users.js", import.meta.url), "utf8"), context);
  vm.runInContext(readFileSync(new URL("../appletart/static/management.js", import.meta.url), "utf8"), context);
  vm.runInContext(readFileSync(new URL("../appletart/static/logs.js", import.meta.url), "utf8"), context);
  vm.runInContext(readFileSync(new URL("../appletart/static/software.js", import.meta.url), "utf8"), context);
  const app = readFileSync(new URL("../appletart/static/app.js", import.meta.url), "utf8");
  vm.runInContext(app.slice(0, app.lastIndexOf("initializeTheme();")), context);
  vm.runInContext('catalog = {images: {ubuntu: {label: "Ubuntu"}}};', context);
  return {context, document, clipboard, run: source => vm.runInContext(source, context)};
}

function find(element, tag, predicate = () => true) {
  if (element.tagName === tag.toUpperCase() && predicate(element)) return element;
  for (const child of element.children) { const found = find(child, tag, predicate); if (found) return found; }
}

test("accepted VM creation and profile deployment open Virtual Machines before refreshing", async () => {
  const ui = dashboard();
  ui.context.api = async () => ({});
  ui.run('toast = () => {}; refresh = async () => { if (currentView !== "vms") throw new Error("Machine screen must open before refresh"); };');
  for (const action of ["create", "deploy-profile"]) {
    ui.run('showView("library")');
    await ui.run(`submit("${action}", {name:"new-vm"})`);
    assert.equal(ui.document.getElementById("page-title").textContent, "Virtual Machines");
    assert.equal(ui.document.getElementById("vm-view").hidden, false);
    assert.equal(ui.document.getElementById("library-view").hidden, true);
    assert.equal(ui.document.getElementById("nav-vms").className, "active");
  }
});

test("rejected creation and other operations keep the current screen", async () => {
  const ui = dashboard();
  ui.run('showView("library"); toast = () => {}; refresh = async () => {};');
  ui.context.api = async () => { throw new Error("VM name already exists"); };
  await assert.rejects(ui.run('submit("create", {name:"existing"})'), /already exists/);
  assert.equal(ui.run("currentView"), "library");
  ui.context.api = async () => ({});
  for (const action of ["download", "build", "start"]) {
    await ui.run(`submit("${action}", {name:"vm"})`);
    assert.equal(ui.run("currentView"), "library");
  }
});

test("existing deployment profiles offer editing of their saved configuration", () => {
  const ui = dashboard();
  ui.run('state.profiles = [{config:{name:"recipe", os:"ubuntu", cpu:2, memory_mb:4096, network:"nat"}}]; renderProfiles();');
  assert.ok(find(ui.document.getElementById("profiles"), "button", button => button.getAttribute("aria-label") === "Edit profile recipe"));
});

test("profile editor loads settings and saves changes under the original name", async () => {
  const ui = dashboard(), calls = [];
  ui.context.document.querySelectorAll = () => [];
  const network = {value: "nat"};
  ui.context.document.querySelector = () => network;
  ui.document.getElementById("vm-form").reset = () => { ui.document.getElementById("edit-profile-notes").value = ""; };
  ui.document.getElementById("wizard").showModal = () => {};
  ui.document.getElementById("wizard").close = () => {};
  ui.run(`catalog = {images: {ubuntu: {label:"Ubuntu", family:"linux", source_kind:"tart", source:"ghcr.io/cirruslabs/ubuntu:24.04"}},
    sizes: {small:{cpu:2, memory_mb:4096, disk_gb:40}}, bundles: []};
    refreshListenAddresses = async () => true; refresh = async () => {}; toast = () => {};
    globalThis.recipe = {config:{name:"existing", os:"ubuntu", source_kind:"tart", source:"ghcr.io/cirruslabs/ubuntu:24.04", cpu:4, memory_mb:8192, disk_gb:80, network:"nat", ssh_user:"admin", ssh_public_keys:[], packages:["jq"], password_login:true}, notes:"Keep these notes"};
    state.profiles = [recipe]; renderProfiles();`);
  ui.context.api = async (path, data) => { calls.push({path, data}); return {config: data.config, key_count: 0}; };
  const edit = find(ui.document.getElementById("profiles"), "button", button => button.getAttribute("aria-label") === "Edit profile existing");
  await edit.onclick();
  assert.equal(ui.document.getElementById("wizard-title").textContent, "Edit Deployment Profile");
  assert.equal(ui.document.getElementById("edit-profile-notes").value, "Keep these notes");
  assert.equal(ui.document.getElementById("cpu").value, 4);
  assert.equal(ui.document.getElementById("packages").value, "jq");
  assert.equal(ui.document.getElementById("vm-name").readOnly, true);
  assert.equal(ui.document.getElementById("deployment-profile").disabled, true);
  ui.document.getElementById("cpu").value = "6";
  ui.document.getElementById("packages").value = "jq, git";
  ui.document.getElementById("edit-profile-notes").value = "Updated notes";
  await ui.run("review()");
  assert.equal(ui.document.getElementById("next").textContent, "Save changes");
  assert.equal(ui.document.getElementById("build-only").hidden, true);
  assert.equal("guest_password" in calls[0].data, false);
  await ui.run("saveEditedProfile()");
  assert.equal(calls[1].path, "/api/profile/save");
  assert.equal(calls[1].data.name, "existing");
  assert.equal(calls[1].data.config.name, "existing");
  assert.equal(calls[1].data.config.cpu, 6);
  assert.deepEqual(Array.from(calls[1].data.config.packages), ["jq", "git"]);
  assert.equal(calls[1].data.notes, "Updated notes");
  assert.equal(calls[1].data.config.password_login, true);
  assert.equal("guest_password" in calls[1].data, false);
  assert.equal(ui.run("recipe.config.cpu"), 4);
  await ui.run("openWizard()");
  assert.equal(ui.document.getElementById("wizard-title").textContent, "Create Virtual Machine");
  assert.equal(ui.document.getElementById("vm-name").readOnly, false);
  assert.equal(ui.document.getElementById("deployment-profile").disabled, false);
  ui.run("showStep(3)");
  assert.equal(ui.document.getElementById("next").textContent, "Download, build & deploy →");
  assert.equal(ui.document.getElementById("build-only").hidden, false);
});

test("theme button changes its icon and accessible label and remembers the preference", () => {
  const ui = dashboard(), saved = new Map();
  ui.document.documentElement = new Element("html");
  ui.context.localStorage = {setItem: (key, value) => saved.set(key, value)};
  ui.run('setTheme("light");');
  assert.equal(ui.document.getElementById("theme-sun").hasAttribute("hidden"), true);
  assert.equal(ui.document.getElementById("theme-moon").hasAttribute("hidden"), false);
  assert.equal(ui.document.getElementById("theme-toggle").getAttribute("aria-label"), "Switch To Dark Mode");
  ui.run('setTheme("dark");');
  assert.equal(ui.document.getElementById("theme-sun").hasAttribute("hidden"), false);
  assert.equal(ui.document.getElementById("theme-moon").hasAttribute("hidden"), true);
  assert.equal(ui.document.getElementById("theme-toggle").getAttribute("aria-label"), "Switch To Light Mode");
  assert.equal(saved.get("appletart-theme"), "dark");
});

test("appearance restores palette and mode and keeps the palette when toggling mode", () => {
  const ui = dashboard(), saved = new Map([["appletart-theme", "light"], ["appletart-palette", "grape"]]);
  ui.document.documentElement = new Element("html");
  ui.context.localStorage = {getItem: key => saved.get(key), setItem: (key, value) => saved.set(key, value)};
  ui.run('initializeTheme();');
  assert.equal(ui.document.documentElement.dataset.palette, "grape");
  assert.equal(ui.document.documentElement.dataset.theme, "light");
  assert.equal(ui.document.getElementById("appearance-mode").value, "light");
  assert.equal(ui.document.getElementById("palette-grape").checked, true);
  ui.document.getElementById("theme-toggle").onclick();
  assert.equal(ui.document.documentElement.dataset.theme, "dark");
  assert.equal(ui.document.documentElement.dataset.palette, "grape");
  ui.run('setPalette("bondi");');
  assert.equal(ui.document.getElementById("palette-grape").checked, false);
  assert.equal(ui.document.getElementById("palette-bondi").checked, true);
  assert.equal(saved.get("appletart-palette"), "bondi");
  ui.run('initializeTheme();');
  assert.equal(ui.document.documentElement.dataset.palette, "bondi");
  assert.equal(ui.document.documentElement.dataset.theme, "dark");
});

test("appearance tolerates stale preferences and unavailable browser storage", () => {
  const ui = dashboard(); ui.document.documentElement = new Element("html");
  ui.context.localStorage = {getItem: () => "obsolete", setItem: () => { throw new Error("Blocked storage"); }};
  ui.run('initializeTheme();');
  assert.equal(ui.document.documentElement.dataset.palette, "orchard");
  assert.equal(ui.document.documentElement.dataset.theme, "dark");
  ui.context.localStorage.getItem = () => { throw new Error("Blocked storage"); };
  ui.run('initializeTheme(); setPalette("rainbow"); setTheme("light");');
  assert.equal(ui.document.documentElement.dataset.palette, "rainbow");
  assert.equal(ui.document.documentElement.dataset.theme, "light");
});

test("Configure excludes its current primary bridge when adding an adapter", async () => {
  const ui = dashboard();
  ui.run(`catalog.interfaces = ["en0", "en1"]; $("bridge").value = "en0";
    refreshListenAddresses = async () => true;
    dialog = (title, description, fields) => $("action-fields").replaceChildren(...fields);`);
  await ui.run('edit({config:{name:"bridge", os:"ubuntu", cpu:2, memory_mb:4096, disk_gb:40, network:"bridged", bridge:"en1", bridges:["en1"]}})');
  const fields = ui.document.getElementById("action-fields");
  find(fields, "button", element => element.textContent === "＋ Add network interface").onclick();
  assert.equal(find(fields, "select", element => element.className === "extra-bridge").value, "en0");
});

test("cloud and golden source changes preserve an optional SSH key selection", () => {
  const ui = dashboard();
  ui.run('$("use-keys").setAttribute("aria-checked", "false");');
  for (const kind of ["cloud", "golden", "tart"]) {
    ui.run(`$("source-kind").value = "${kind}"; syncSource();`);
    assert.equal(ui.document.getElementById("use-keys").disabled, false, `${kind} prevents turning key installation off`);
    assert.equal(ui.document.getElementById("use-keys").getAttribute("aria-checked"), "false", `${kind} forces key installation on`);
    assert.equal(ui.document.getElementById("public-key-fields").hidden, true, `${kind} shows public key fields with key installation off`);
    if (kind !== "tart") assert.equal(ui.document.getElementById("ssh-fields").hidden, false, `${kind} hides the guest account without personal SSH keys`);
  }
});

test("quick golden launch key selection respects the saved installation preference", () => {
  const ui = dashboard();
  ui.run('catalog.default_public_key = "~/.ssh/custom.pub"; catalog.install_ssh_key_by_default = true;');
  assert.deepEqual(Array.from(ui.run('defaultSSHKeys()')), ["~/.ssh/custom.pub"]);
  ui.run('catalog.install_ssh_key_by_default = false;');
  assert.equal(ui.run('defaultSSHKeys().length'), 0);
  ui.run('catalog.install_ssh_key_by_default = true; catalog.default_public_key = "";');
  assert.equal(ui.run('defaultSSHKeys().length'), 0);
});

test("folder browsing updates its own share row and cancellation keeps typed paths", async () => {
  const ui = dashboard(), calls = [];
  const results = [{path:"/Users/test/Shared Folder", cancelled:false}, {path:"", cancelled:true}];
  ui.context.api = async (path, data) => { calls.push({path, data}); return results.shift(); };
  ui.run('addShare($("directory-shares"), {host_path:"~/First"}); addShare($("directory-shares"), {host_path:"~/Second"});');
  const rows = ui.document.getElementById("directory-shares").children;
  const host = row => find(row, "input", element => element.dataset.share === "host_path");
  const browse = row => find(row, "button", element => element.getAttribute("aria-label") === "Browse for local directory");
  await browse(rows[1]).onclick();
  assert.equal(host(rows[1]).value, "/Users/test/Shared Folder");
  assert.equal(host(rows[0]).value, "~/First");
  await browse(rows[0]).onclick();
  assert.equal(host(rows[0]).value, "~/First");
  assert.equal(browse(rows[0]).disabled, false);
  assert.equal(browse(rows[0]).textContent, "Browse…");
  assert.equal(calls.length, 2);
  assert.equal(calls[0].path, "/api/directory/browse");
});

test("Configure preserves shares and saves browsed additions and removals", async () => {
  const ui = dashboard(), calls = [];
  let save;
  ui.run('catalog.interfaces = []; refreshListenAddresses = async () => true;');
  ui.context.dialog = (_title, _description, fields, callback) => {
    ui.document.getElementById("action-fields").replaceChildren(...fields);
    for (const input of ui.document.getElementById("action-fields").querySelectorAll("input")) {
      if (input.id) ui.document.getElementById(input.id).value = input.value;
    }
    save = callback;
  };
  ui.context.api = async () => ({path:"/Users/test/New Folder"});
  ui.context.submit = async (action, data) => calls.push({action, config:JSON.parse(JSON.stringify(data.config))});
  await ui.run('edit({config:{name:"vm", os:"kali", source_kind:"cloud", cpu:2, memory_mb:4096, disk_gb:40, network:"nat", directory_shares:[{host_path:"/Users/test/Existing", guest_path:"/mnt/existing", read_only:true}]}})');
  const fields = ui.document.getElementById("action-fields");
  await save();
  assert.deepEqual(calls[0].config.directory_shares, [{host_path:"/Users/test/Existing", guest_path:"/mnt/existing", read_only:true}]);
  find(fields, "button", element => element.textContent === "＋ Add Directory Share").onclick();
  const rows = fields.querySelectorAll("div").filter(element => element.className === "connection-row");
  await find(rows[1], "button", element => element.getAttribute("aria-label") === "Browse for local directory").onclick();
  find(rows[1], "input", element => element.dataset.share === "guest_path").value = "/mnt/new";
  find(rows[1], "input", element => element.dataset.share === "read_only").checked = false;
  find(rows[0], "button", element => element.textContent === "Remove share").onclick();
  await save();
  assert.equal(calls[1].action, "configure");
  assert.deepEqual(calls[1].config.directory_shares, [{host_path:"/Users/test/New Folder", guest_path:"/mnt/new", read_only:false}]);
});

test("polling retains the VM button under the pointer", () => {
  const ui = dashboard();
  ui.run('state.machines = [{managed:true, exists:true, owned:true, running:false, phase:"ready", config:{name:"ubuntu-dev", os:"ubuntu", cpu:2, memory_mb:4096, disk_gb:40, network:"nat"}}]; renderMachines();');
  const button = find(ui.document.getElementById("machines"), "button");
  ui.run("renderMachines();");
  assert.equal(find(ui.document.getElementById("machines"), "button"), button, "unchanged button was replaced, restarting hover effects");
});

test("SSH config action requires a current IP and writes the selected VM once", async () => {
  const ui = dashboard(), calls = [];
  let complete;
  ui.context.api = async (path, data) => { calls.push({path, name:data.name}); return await new Promise(resolve => { complete = resolve; }); };
  ui.run('state.machines = [{managed:true, exists:true, owned:true, running:false, phase:"ready", config:{name:"vm", os:"ubuntu", cpu:2, memory_mb:4096, disk_gb:40, network:"nat"}}]; renderMachines();');
  const action = () => find(ui.document.getElementById("machines"), "button", element => element.getAttribute("aria-label") === "Add To SSH Config");
  assert.equal(action().disabled, true);
  ui.run('state.machines[0].running = true; addresses.set("vm", {health:{ip:"192.0.2.10"}}); renderMachines();');
  assert.equal(action().disabled, false);
  const pending = action().onclick();
  assert.equal(action().disabled, true);
  await action().onclick();
  assert.equal(calls.length, 1);
  complete({changed:true, command:"ssh vm"});
  await pending;
  assert.deepEqual(calls, [{path:"/api/ssh-config", name:"vm"}]);
  assert.equal(action().disabled, false);
  assert.match(ui.document.getElementById("toast").textContent, /ssh vm/);
});

test("SSH launch shows pending feedback and blocks repeated clicks across polling", async () => {
  const ui = dashboard(), calls = [];
  let finish;
  ui.context.api = async (path, data) => {
    calls.push({path, data});
    return new Promise(resolve => { finish = resolve; });
  };
  ui.run('state.machines = [{managed:true, exists:true, owned:true, running:true, phase:"ready", config:{name:"vm", os:"ubuntu", cpu:2, memory_mb:4096, disk_gb:40, network:"nat"}}]; addresses.set("vm", {health:{ip:"192.0.2.10", ssh_ready:true}}); renderMachines();');
  const action = label => find(ui.document.getElementById("machines"), "button", e => e.getAttribute("aria-label") === label);
  const pending = action("SSH").onclick();
  assert.equal(action("Opening SSH…").disabled, true);
  assert.equal(ui.document.getElementById("toast").textContent, "Opening SSH…");
  ui.run('renderMachines();');
  await action("Opening SSH…").onclick();
  assert.equal(calls.length, 1);
  finish({terminal:"iTerm2"});
  await pending;
  assert.equal(action("SSH").disabled, false);
});

test("closing the chooser's dialog cancels it without adding a browser cancel button", async () => {
  const ui = dashboard(), calls = [];
  let finish;
  ui.context.api = async (path, data) => {
    calls.push({path, data});
    if (path === "/api/picker/cancel") { finish({cancelled:true, path:""}); return {cancelled:true}; }
    return new Promise(resolve => { finish = resolve; });
  };
  ui.run('addShare($("directory-shares"), {host_path:"~/Keep"});');
  const row = ui.document.getElementById("directory-shares").children[0];
  const browse = find(row, "button", e => e.getAttribute("aria-label") === "Browse for local directory");
  const pending = browse.onclick();
  assert.equal(browse.disabled, true);
  assert.equal(find(row, "button", e => e.textContent === "Cancel chooser"), undefined);
  ui.run('cancelLocalPickers("action-dialog");');
  await pending;
  assert.equal(find(row, "input", e => e.dataset.share === "host_path").value, "~/Keep");
  assert.equal(browse.disabled, false);
  assert.equal(find(row, "button", e => e.textContent === "Cancel chooser"), undefined);
  assert.equal(calls[0].data.picker_id, calls[1].data.picker_id);
});

test("a log update preserves the reader's scroll position and log element", () => {
  const ui = dashboard();
  ui.run('state.jobs = [{id:"build-1", name:"ubuntu-dev", action:"build", status:"running", cancellable:true, lines:Array.from({length:40}, (_, i) => "line " + i)}]; renderJobs();');
  const log = find(ui.document.getElementById("job-list"), "pre");
  log.scrollTop = 120;
  ui.run('state.jobs[0].lines.push("new progress"); renderJobs();');
  const updated = find(ui.document.getElementById("job-list"), "pre");
  assert.equal(updated.scrollTop, 120, "polling jumped a scrolled log back to its start");
  assert.equal(updated, log, "the existing log element was replaced");
  assert.match(updated.textContent, /new progress$/);
});

test("new log lines follow the bottom only when the reader is already there", () => {
  const ui = dashboard();
  ui.run('state.jobs = [{id:"build-1", name:"vm", action:"build", status:"running", lines:Array.from({length:40}, (_, i) => "line " + i)}]; renderJobs();');
  const log = find(ui.document.getElementById("job-list"), "pre");
  assert.equal(log.scrollTop, log.scrollHeight - log.clientHeight);
  const text = log.firstChild;
  ui.run('state.jobs[0].lines.push("more progress"); renderJobs();');
  assert.equal(log.scrollTop, log.scrollHeight - log.clientHeight);
  assert.equal(log.firstChild, text, "appending progress replaced the text node used by selections");
  log.scrollTop = 50;
  ui.run('state.jobs[0].lines.push("even more progress"); renderJobs();');
  assert.equal(log.scrollTop, 50);
});

test("rolling server log snapshots retain previously received history", () => {
  const ui = dashboard();
  ui.run('state.jobs = [{id:"build-1", name:"vm", action:"build", status:"running", lines:Array.from({length:40}, (_, i) => "line " + i)}]; renderJobs();');
  const log = find(ui.document.getElementById("job-list"), "pre");
  log.scrollTop = 120;
  ui.run('state.jobs[0].lines = state.jobs[0].lines.slice(10).concat(["line 40", "line 41"]); renderJobs(); renderJobs();');
  assert.equal(log.scrollTop, 120);
  assert.equal(log.textContent, Array.from({length:42}, (_, i) => "line " + i).join("\n"));
});

test("Copy log includes received history and keeps its feedback across updates", async () => {
  const ui = dashboard();
  ui.run('state.jobs = [{id:"build-1", name:"vm", action:"build", status:"failed", lines:Array.from({length:40}, (_, i) => "line " + i)}]; renderJobs();');
  const log = find(ui.document.getElementById("job-list"), "pre");
  log.scrollTop = 120;
  ui.run('state.jobs[0].lines = state.jobs[0].lines.slice(10).concat(["build error"]); renderJobs();');
  const copy = find(ui.document.getElementById("job-list"), "button", e => e.getAttribute("aria-label") === "Copy log for vm build");
  assert.ok(copy, "Activity has no Copy log button");
  await copy.onclick();
  assert.equal(ui.clipboard[0], "vm · build (failed)\n\n" + log.textContent);
  assert.match(ui.clipboard[0], /line 0\n/);
  ui.run('renderJobs();');
  assert.equal(find(ui.document.getElementById("job-list"), "button", e => e.getAttribute("aria-label") === "Copied log for vm build"), copy);
  assert.match(copy.className, /copied/);
  assert.equal(log.scrollTop, 120);
});

test("retained controls act on the latest VM configuration", () => {
  const ui = dashboard();
  ui.context.machineDetails = machine => machine.config.cpu;
  ui.run('state.machines = [{managed:true, exists:true, owned:true, running:false, phase:"ready", config:{name:"vm", os:"ubuntu", cpu:2, memory_mb:4096, disk_gb:40, network:"nat"}}]; renderMachines();');
  const details = find(ui.document.getElementById("machines"), "button", element => element.getAttribute("aria-label") === "Details");
  assert.equal(details.onclick(), 2);
  ui.run('state.machines[0] = {...state.machines[0], config:{...state.machines[0].config, cpu:4}}; renderMachines();');
  assert.equal(find(ui.document.getElementById("machines"), "button", element => element.getAttribute("aria-label") === "Details"), details);
  assert.equal(details.onclick(), 4, "the retained button used a stale record");
});

test("the power switch precedes the VM icon and starts and gracefully shuts down", async () => {
  const ui = dashboard(), calls = [];
  ui.context.submit = async (action, data) => calls.push({action, name: data.name});
  ui.run('state.machines = [{managed:true, exists:true, owned:true, running:false, phase:"ready", config:{name:"vm", os:"ubuntu", cpu:2, memory_mb:4096, disk_gb:40, network:"nat"}}]; renderMachines();');
  const power = find(ui.document.getElementById("machines"), "button", element => element.getAttribute("role") === "switch");
  assert.equal(power.parentNode.firstChild, power);
  assert.match(power.parentNode.children[1].className, /os-icon/);
  assert.equal(power.getAttribute("aria-checked"), "false");
  await power.onclick();
  assert.deepEqual(calls, [{action:"start", name:"vm"}]);
  ui.run('state.jobs = [{name:"vm", action:"start", status:"running"}]; renderMachines();');
  assert.equal(find(ui.document.getElementById("machines"), "button", element => element.getAttribute("role") === "switch"), power);
  assert.equal(power.disabled, true);
  assert.equal(power.getAttribute("aria-busy"), "true");
  ui.run('state.jobs = []; state.machines[0].running = true; renderMachines();');
  assert.equal(power.getAttribute("aria-checked"), "true");
  assert.equal(power.disabled, false);
  assert.equal(power.parentNode.firstChild, power);
  assert.equal(find(ui.document.getElementById("machines"), "button", element => element.getAttribute("aria-label") === "Shutdown"), undefined);
  await power.onclick();
  assert.deepEqual(calls, [{action:"start", name:"vm"}, {action:"shutdown", name:"vm"}]);
});

test("stopped Linux Tart templates offer golden creation while macOS templates do not", () => {
  const ui = dashboard();
  ui.run('state.machines = [{managed:true, exists:true, owned:true, running:false, phase:"ready", config:{name:"linux", os:"fedora", guest_family:"linux", source_kind:"tart", cpu:2, memory_mb:4096, disk_gb:40, network:"nat"}}]; renderMachines();');
  assert.ok(find(ui.document.getElementById("machines"), "button", element => element.getAttribute("aria-label") === "Save as golden image"));
  ui.run('state.machines[0].config.guest_family="macos"; renderMachines();');
  assert.equal(find(ui.document.getElementById("machines"), "button", element => element.getAttribute("aria-label") === "Save as golden image"), undefined);
});

test("jobs can be added, completed and removed without replacing other log panes", () => {
  const ui = dashboard();
  ui.run('state.jobs = [{id:"first", name:"one", action:"build", status:"running", lines:Array.from({length:40}, (_, i) => "line " + i)}]; renderJobs();');
  const log = find(ui.document.getElementById("job-list"), "pre");
  log.scrollTop = 120;
  ui.run('state.jobs.unshift({id:"second", name:"two", action:"build", status:"running", lines:["starting"]}); renderJobs(); state.jobs[1].status = "complete"; renderJobs();');
  assert.equal(find(ui.document.getElementById("job-list"), "pre", element => element.getAttribute("aria-label") === "one build log"), log);
  assert.equal(log.scrollTop, 120);
  ui.run('state.jobs = state.jobs.filter(job => job.id === "first"); renderJobs();');
  assert.equal(ui.document.getElementById("job-list").children.length, 1);
  assert.equal(find(ui.document.getElementById("job-list"), "pre"), log);
});

test("library buttons and image/profile choices survive background updates", () => {
  const ui = dashboard();
  ui.run('state.images = [{name:"golden", os:"ubuntu", phase:"ready", version:"1", disk_gb:40}]; state.profiles = [{config:{name:"recipe", os:"ubuntu", cpu:2, memory_mb:4096, network:"nat"}}]; renderImages(); renderProfiles(); goldenOptions();');
  const imageButton = find(ui.document.getElementById("golden-images"), "button");
  const profileButton = find(ui.document.getElementById("profiles"), "button");
  const imageChoice = ui.document.getElementById("golden-source").children[0];
  const profileChoice = ui.document.getElementById("deployment-profile").children[1];
  ui.document.getElementById("golden-source").value = "golden";
  ui.document.getElementById("deployment-profile").value = "recipe";
  ui.run('state.images[0].version = "2"; state.profiles[0].config.cpu = 4; renderImages(); renderProfiles(); goldenOptions();');
  assert.equal(find(ui.document.getElementById("golden-images"), "button"), imageButton);
  assert.equal(find(ui.document.getElementById("profiles"), "button"), profileButton);
  assert.equal(ui.document.getElementById("golden-source").children[0], imageChoice);
  assert.equal(ui.document.getElementById("deployment-profile").children[1], profileChoice);
  assert.equal(ui.document.getElementById("golden-source").value, "golden");
  assert.equal(ui.document.getElementById("deployment-profile").value, "recipe");
});

test("saved log updates preserve the scrolled reader and append without replacing text", async () => {
  const ui = dashboard();
  ui.run(`const savedPre = node("pre", Array.from({length:40}, (_, i) => "line " + i).join("\\n"));
    const savedViewer = {name:"vm", source:"abc", start:0, end:100, size:100, pre:savedPre,
      select:node("select"), range:node("p"), earlier:button("earlier"), copy:button("copy"), download:button("download"), live:{checked:true}};
    logViewer = savedViewer; $("action-dialog").open = true;
    api = async () => ({log:"\\nnew diagnostic", start:100, end:115, size_bytes:115, source:"abc", sources:[{source:"abc", action:"build", status:"running", started_at:"2026-10-03T00:00:00Z"}]});
    savedPre.scrollTop = 120;`);
  const pre = ui.run("savedPre"), text = pre.firstChild;
  await ui.run('loadSavedLog(savedViewer, "append")');
  assert.equal(pre.scrollTop, 120);
  assert.equal(pre.firstChild, text);
  assert.match(pre.textContent, /new diagnostic$/);
  assert.equal(ui.run("savedViewer.end"), 115);
});

test("loading earlier saved output pauses live updates and keeps the current text in view", async () => {
  const ui = dashboard();
  ui.run(`const savedPre = node("pre", Array.from({length:40}, (_, i) => "line " + i).join("\\n"));
    const savedViewer = {name:"vm", source:"abc", start:100, end:200, size:200, pre:savedPre,
      select:node("select"), range:node("p"), earlier:button("earlier"), copy:button("copy"), download:button("download"), live:{checked:true}};
    logViewer = savedViewer; $("action-dialog").open = true;
    api = async () => ({log:"previous output\\n", start:0, end:100, size_bytes:200, source:"abc", sources:[{source:"abc", action:"build", status:"complete", started_at:"2026-10-03T00:00:00Z"}]});
    savedPre.scrollTop = 120;`);
  await ui.run('loadSavedLog(savedViewer, "earlier")');
  assert.equal(ui.run("savedViewer.live.checked"), false);
  assert.equal(ui.run("savedViewer.start"), 0);
  assert.equal(ui.run("savedViewer.end"), 200);
  assert.equal(ui.run("savedPre.scrollTop"), 142);
  assert.match(ui.run("savedPre.textContent"), /^previous output\nline 0/);
});

test("saved log copy fetches the full transcript rather than the preview", async () => {
  const ui = dashboard();
  ui.context.fetch = async () => ({ok: true, blob: async () => ({text: async () => "FULL LOG START\n" + "x".repeat(150000) + "\nFULL LOG END"})});
  ui.run('const savedViewer = {name:"vm", source:"abc", pre:node("pre", "short preview"), copy:button("copy")}; logViewer = savedViewer;');
  await ui.run("copySavedLog(savedViewer)");
  assert.match(ui.clipboard[0], /^FULL LOG START/);
  assert.match(ui.clipboard[0], /FULL LOG END$/);
  assert.ok(ui.clipboard[0].length > 150000);
  assert.equal(ui.run('savedViewer.copy.getAttribute("aria-label")'), "Copied full log");
});


test("software bundles are optional, filtered by platform and retain explicit selections", () => {
  const ui = dashboard();
  ui.run('catalog.images.kali = {label:"Kali", family:"linux", icon:"kali"}; catalog.bundles = [{id:"kali-essentials", name:"Kali essentials", platforms:["kali"], packages:["git", "jq"]}, {id:"brew-tools", name:"Homebrew tools", platforms:["macos"], packages:["jq"]}]; catalog.software_notice="Allow 30 minutes"; guest="kali"; renderBundleChoices([]);');
  assert.equal(ui.document.getElementById("bundle-options").children.length, 1);
  assert.equal(find(ui.document.getElementById("bundle-options"), "button").getAttribute("aria-checked"), "false");
  ui.run('renderBundleChoices(["kali-essentials"]);');
  const toggle = find(ui.document.getElementById("bundle-options"), "button");
  assert.equal(toggle.getAttribute("role"), "switch");
  assert.equal(toggle.getAttribute("aria-checked"), "true");
  assert.equal(ui.document.getElementById("package-build-warning").hidden, false);
  toggle.onclick();
  assert.equal(ui.run("selectedBundles().length"), 0);
  assert.equal(ui.document.getElementById("package-build-warning").hidden, false, "Initial Linux builds still upgrade with no bundles selected");
  toggle.onclick();
  assert.equal(ui.run("selectedBundles()[0]"), "kali-essentials");
  ui.run('guest="ubuntu"; renderBundleChoices([]);');
  assert.equal(find(ui.document.getElementById("bundle-options"), "button"), undefined);
});

test("every platform uses an image icon and unknown platforms use the Linux fallback", () => {
  const ui = dashboard();
  for (const id of ["ubuntu", "kali", "rhel", "fedora", "debian", "rocky", "macos", "other"]) {
    const icon = ui.run(`osIcon(${JSON.stringify(id)})`);
    assert.equal(find(icon, "img").src, `/icons/${id}.svg`);
  }
  ui.run('catalog.images.runner = {icon:"ubuntu"};');
  assert.equal(find(ui.run('osIcon("runner")'), "img").src, "/icons/ubuntu.svg");
  assert.equal(find(ui.run('osIcon("custom")'), "img").src, "/icons/other.svg");
});

test("adding an image version preserves the latest default and rejects duplicates", () => {
  const ui = dashboard();
  ui.run('const versionsCatalog = {kali:{label:"Kali", family:"linux", source_kind:"cloud", source:"https://example.org/latest-arm64.img", sha256:"a".repeat(64), versions:[{label:"Latest",source:"https://example.org/latest-arm64.img"}]}}; const addedVersion = {label:"2025.4", source_kind:"cloud", source:"https://example.org/older-arm64.img", sha512:"b".repeat(128)}; const extended = withImageVersion(versionsCatalog, "kali", addedVersion);');
  assert.equal(ui.run('extended.kali.source'), 'https://example.org/latest-arm64.img');
  assert.equal(ui.run('extended.kali.versions.length'), 2);
  assert.equal(ui.run('versionsCatalog.kali.versions.length'), 1);
  assert.equal(ui.run('extended.kali.versions[1].sha512'), 'b'.repeat(128));
  assert.throws(() => ui.run('withImageVersion(extended,"kali",addedVersion)'), /already in/);
});

test("Kali version selection updates source and checksum without changing its default", () => {
  const ui = dashboard();
  ui.run('catalog.images.kali = {family:"linux", source_kind:"cloud", source:"https://example.org/latest-arm64.img", sha256:"a".repeat(64), versions:[{label:"Latest",source_kind:"cloud",source:"https://example.org/latest-arm64.img"},{label:"Older",source_kind:"cloud",source:"https://example.org/older-arm64.img",sha512:"b".repeat(128)},{label:"Local",source_kind:"cloud",source:"/tmp/kali-arm64.img"}]}; guest="kali"; checksumHint=()=>{}; syncSource=()=>{}; $("disk").value=40; populateVersions();');
  assert.equal(ui.document.getElementById('platform-version').value, 'https://example.org/latest-arm64.img');
  assert.equal(ui.document.getElementById('platform-version-label').hidden, false);
  ui.run('$("platform-version").value="https://example.org/older-arm64.img"; chooseVersion();');
  assert.equal(ui.document.getElementById('source').value, 'https://example.org/older-arm64.img');
  assert.equal(ui.document.getElementById('sha256').value, 'b'.repeat(128));
  assert.equal(ui.document.getElementById('checksum-algorithm').value, 'sha512');
  ui.run('$("platform-version").value=catalog.images.kali.source; chooseVersion();');
  assert.equal(ui.document.getElementById('sha256').value, 'a'.repeat(64));
  assert.equal(ui.document.getElementById('checksum-algorithm').value, 'sha256');
  ui.run('$("platform-version").value="/tmp/kali-arm64.img"; chooseVersion();');
  assert.equal(ui.document.getElementById('source-location').value, 'local');
  assert.equal(ui.document.getElementById('sha256').value, '');
});

test("saving a version retains the selected platform tile", () => {
  const ui = dashboard();
  ui.run('catalog.images.kali={label:"Kali",family:"linux",source_kind:"cloud",source:"https://example.org/kali-arm64.img",versions:[]}; guest="kali"; renderPlatforms(); catalog.images=withImageVersion(catalog.images,"kali",{label:"Older",source_kind:"cloud",source:"https://example.org/older-arm64.img"}); renderPlatforms();');
  const tile = find(ui.document.getElementById('os-options'), 'button', button => button.dataset.os === 'kali');
  assert.match(tile.className, /selected/);
});

test("provisioning account editor separates passwords from reusable recipes", () => {
  const {run} = dashboard();
  run(`const container = node("section");
    const editor = userEditor(container, [{username: "analyst", ssh_authorized_keys: [], password: "test-only-password", password_login: true}]);
    globalThis.accountRecipes = editor.recipes(); globalThis.accountSecrets = editor.passwords();
    editor.clear(); globalThis.remainingUsers = editor.count();`);
  assert.equal(run('accountRecipes[0].password_login'), true);
  assert.equal(run('JSON.stringify(accountRecipes).includes("test-only-password")'), false);
  assert.equal(run('accountSecrets.analyst'), 'test-only-password');
  assert.equal(run('remainingUsers'), 0);
});

test("guest password stays available with personal keys off for Linux and macOS", () => {
  const {run, document} = dashboard();
  run('catalog.images.macos = {family: "macos"};');
  for (const kind of ['cloud', 'golden', 'tart']) {
    document.getElementById('source-kind').value = kind;
    run('$("use-keys").setAttribute("aria-checked", "false"); syncSource();');
    assert.equal(document.getElementById('ssh-fields').hidden, false);
    assert.equal(document.getElementById('public-key-fields').hidden, true);
  }
  run('guest = "macos"; $("source-kind").value = "tart"; syncSource();');
  assert.equal(document.getElementById('ssh-fields').hidden, false);
});

test("profile credentials prompt for password accounts without placing secrets in recipes", () => {
  const {run} = dashboard();
  run(`const recipe = {ssh_user: "vmadmin", password_login: true, users: [{username: "analyst", ssh_authorized_keys: [], password_login: true}]};
    const prompt = credentialFields(recipe);
    prompt.fields[0].querySelector("input").value = "primary test password";
    prompt.fields[1].querySelector("input").value = "additional test password";
    globalThis.promptCredentials = prompt.data(); globalThis.promptRecipe = recipe;`);
  assert.equal(run('promptCredentials.guest_password'), 'primary test password');
  assert.equal(run('promptCredentials.user_passwords.analyst'), 'additional test password');
  assert.equal(run('JSON.stringify(promptRecipe).includes("test password")'), false);
});
