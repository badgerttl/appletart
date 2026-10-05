"use strict";

// Preserve mounted elements, focus and hover state while applying new snapshots.
function liveKey(element) {
  return element.nodeType === 1 ? element.getAttribute("data-live-key") : null;
}
function sameNodeKind(left, right) {
  return left.nodeType === right.nodeType && (left.nodeType !== 1 || left.tagName === right.tagName);
}
function reconcileChildren(parent, desired) {
  const previous = Array.from(parent.childNodes);
  const keyed = new Map(previous.filter(child => liveKey(child) !== null).map(child => [liveKey(child), child]));
  const used = new Set();
  desired.forEach((fresh, index) => {
    const key = liveKey(fresh);
    const existing = key !== null ? keyed.get(key) : previous.find(child => !used.has(child) && liveKey(child) === null && sameNodeKind(child, fresh));
    const current = existing && sameNodeKind(existing, fresh) ? existing : fresh;
    used.add(current);
    if (current === existing) updateLiveNode(existing, fresh);
    if (parent.childNodes[index] !== current) parent.insertBefore(current, parent.childNodes[index] || null);
    if (current === fresh) initializeLiveLogs(current);
  });
  for (const child of previous) if (!used.has(child)) child.remove();
}
function updateLiveNode(existing, fresh) {
  if (existing.nodeType === 3) {
    if (existing.nodeValue !== fresh.nodeValue) existing.nodeValue = fresh.nodeValue;
    return;
  }
  for (const {name} of Array.from(existing.attributes)) if (!fresh.hasAttribute(name)) existing.removeAttribute(name);
  for (const {name, value} of Array.from(fresh.attributes)) if (existing.getAttribute(name) !== value) existing.setAttribute(name, value);
  // Rendered buttons capture the latest VM/job record, even when their DOM stays put.
  existing.onclick = fresh.onclick;
  if (fresh.hasAttribute("data-live-log")) updateLiveLog(existing, fresh.textContent);
  else reconcileChildren(existing, Array.from(fresh.childNodes));
}
function initializeLiveLogs(element) {
  if (element.nodeType !== 1) return;
  if (element.hasAttribute("data-live-log")) {
    element.liveLogSnapshot = element.textContent;
    element.scrollTop = element.scrollHeight;
  }
  for (const child of element.children) initializeLiveLogs(child);
}
function updateLiveLog(element, snapshot) {
  const previous = element.liveLogSnapshot ?? element.textContent;
  if (snapshot === previous) return;
  const following = element.scrollHeight - element.clientHeight - element.scrollTop <= 4;
  let top = element.scrollTop;
  const left = element.scrollLeft;
  let added = null;
  if (snapshot.startsWith(previous)) added = snapshot.slice(previous.length);
  else {
    // The server keeps a bounded tail. Append its new lines without discarding
    // history that the reader has already received in this browser session.
    const before = previous.split("\n"), after = snapshot.split("\n");
    for (let count = Math.min(before.length, after.length); count > 0; count--) {
      if (before.slice(-count).every((line, index) => line === after[index])) {
        added = after.length > count ? "\n" + after.slice(count).join("\n") : "";
        break;
      }
    }
  }
  if (added !== null && element.firstChild?.nodeType === 3 && element.childNodes.length === 1) element.firstChild.appendData(added);
  else element.textContent = snapshot;
  element.liveLogSnapshot = snapshot;
  // Bound browser history to 1M characters per job, trimming whole lines from its start.
  if (element.textContent.length > 1024 * 1024) {
    const text = element.textContent;
    const start = text.indexOf("\n", text.length - 1024 * 1024);
    const height = element.scrollHeight;
    element.textContent = text.slice(start < 0 ? text.length - 1024 * 1024 : start + 1);
    top = Math.max(0, top - (height - element.scrollHeight));
  }
  element.scrollTop = following ? element.scrollHeight : top;
  element.scrollLeft = left;
}
function setLiveText(element, value) {
  const text = String(value);
  if (element.textContent !== text) element.textContent = text;
}

const $ = (id) => document.getElementById(id);
const token = document.querySelector('meta[name="appletart-token"]').content;
let catalog, state = {machines: [], jobs: []}, step = 0, guest = "ubuntu", size = "small", lastJob = "", editingProfile = null;
let actionCallback = null, polling = false;
const addresses = new Map();
const copiedLogs = new Map();

async function discoverAddress(machine, force = false) {
  const name = machine.config.name;
  const previous = addresses.get(name);
  if (previous?.loading || (!force && previous && Date.now() - previous.checkedAt < 15000)) return;
  const item = {...previous, loading: true, checkedAt: Date.now()}; addresses.set(name, item);
  try {
    const result = await api("/api/health", {name});
    item.ip = result.ip; item.health = result; item.error = result.issues.join("\n");
  } catch (error) {
    item.ip = ""; item.error = error.message;
  } finally {
    item.loading = false; item.checkedAt = Date.now();
    // Ignore a lookup that finished after this VM stopped or restarted.
    if (addresses.get(name) === item && state.machines.some((vm) => vm.config.name === name && vm.running)) renderMachines();
  }
}
async function copyAddress(name, ip) {
  try {
    await navigator.clipboard.writeText(ip);
    const item = addresses.get(name);
    if (item) item.copiedUntil = Date.now() + 1800;
    renderMachines();
    setTimeout(renderMachines, 1800);
  } catch (_) { toast("Clipboard access was blocked. Select and copy the address manually: " + ip); }
}
function addressButton(machine) {
  const name = machine.config.name; const item = addresses.get(name);
  const ip = item?.ip;
  const copied = ip && item.copiedUntil > Date.now();
  const element = button(ip ? copied ? "Copied" : ip : item?.loading ? "Finding IP…" : "IP unavailable",
    () => ip ? copyAddress(name, ip) : discoverAddress(machine, true));
  element.setAttribute("data-live-key", "ip-address");
  element.classList.add("ip-address");
  element.classList.toggle("copied", !!copied);
  element.setAttribute("aria-label", copied ? `Copied IP address for ${name}: ${ip}` : ip ? `Copy IP address for ${name}: ${ip}` : `Retry IP discovery for ${name}`);
  element.setAttribute("aria-live", "polite");
  element.title = ip ? `${ip} · ${copied ? "Copied to clipboard" : "Click to copy IP address"}` : item?.error || "Waiting for the guest's IP address";
  element.disabled = !ip && !!item?.loading;
  return element;
}

const themePalettes = [
  {id: "orchard", name: "Orchard", detail: "AppleTart’s original garden greens."},
  {id: "macintosh", name: "Macintosh", detail: "Silver surfaces and a crisp Apple blue."},
  {id: "graphite", name: "Graphite", detail: "Quiet neutrals inspired by Space Gray."},
  {id: "grape", name: "iMac Grape", detail: "Playful purple from the colorful iMac era."},
  {id: "tokyo-night", name: "Tokyo Night", detail: "Midnight navy with blue and violet city lights."},
  {id: "rainbow", name: "Classic Rainbow", detail: "Warm ivory with a six-color flourish."}
];
function setTheme(theme) {
  theme = theme === "light" ? "light" : "dark";
  document.documentElement.dataset.theme = theme;
  $(theme === "dark" ? "theme-sun" : "theme-moon").removeAttribute("hidden");
  $(theme === "dark" ? "theme-moon" : "theme-sun").setAttribute("hidden", "");
  const label = `Switch To ${theme === "dark" ? "Light" : "Dark"} Mode`;
  $("theme-toggle").setAttribute("aria-label", label);
  $("theme-toggle").title = label;
  $("appearance-mode").value = theme;
  try { localStorage.setItem("appletart-theme", theme); } catch (_) {}
}
function setPalette(palette) {
  const selected = themePalettes.find(item => item.id === palette) || themePalettes[0];
  document.documentElement.dataset.palette = selected.id;
  for (const item of themePalettes) $("palette-" + item.id).checked = item.id === selected.id;
  $("appearance-status").textContent = selected.name + " theme · Saved automatically in this browser.";
  try { localStorage.setItem("appletart-palette", selected.id); } catch (_) {}
}
function initializeTheme() {
  for (const palette of themePalettes) {
    const card = node("label", undefined, "theme-card"); card.dataset.palette = palette.id;
    const radio = node("input"); radio.type = "radio"; radio.name = "palette"; radio.value = palette.id; radio.id = "palette-" + palette.id;
    radio.addEventListener("change", () => { if (radio.checked) setPalette(palette.id); });
    const preview = node("span", undefined, "theme-preview"); preview.setAttribute("aria-hidden", "true");
    const content = node("span", undefined, "theme-preview-content");
    content.append(node("span", undefined, "theme-preview-bar"), node("span", undefined, "theme-preview-panel"), node("span", undefined, "theme-preview-button"));
    preview.append(node("span", undefined, "theme-preview-sidebar"), content);
    const caption = node("span", undefined, "theme-caption"); caption.append(node("strong", palette.name), node("small", palette.detail));
    card.append(radio, preview, caption); $("theme-palettes").append(card);
  }
  let theme = "dark", palette = "orchard";
  try { theme = localStorage.getItem("appletart-theme") || theme; palette = localStorage.getItem("appletart-palette") || palette; } catch (_) {}
  setTheme(theme); setPalette(palette);
  $("theme-toggle").addEventListener("click", () => setTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark"));
  $("appearance-mode").addEventListener("change", event => setTheme(event.target.value));
}

function node(tag, text, className) {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = text;
  if (className) element.className = className;
  return element;
}
async function api(path, data) {
  const response = await fetch(path, {method: data === undefined ? "GET" : "POST", headers: {
    "X-Appletart-Token": token, ...(data === undefined ? {} : {"Content-Type": "application/json"})
  }, ...(data === undefined ? {} : {body: JSON.stringify(data)})});
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || "The local service could not complete this request.");
  return result;
}
function banner(id, error) { setLiveText($(id), error ? String(error.message || error) : ""); $(id).hidden = !error; }
function toast(message) {
  $("toast").textContent = message; $("toast").hidden = false;
  clearTimeout(toast.timer); toast.timer = setTimeout(() => { $("toast").hidden = true; }, 5500);
}
const localPickers = new Map();
async function browseLocal(control, endpoint, data, owner) {
  if (control.disabled) return {cancelled: true, path: ""};
  const pickerId = crypto.randomUUID();
  control.disabled = true; control.textContent = "Choosing…"; control.setAttribute("aria-busy", "true");
  localPickers.set(pickerId, owner);
  try { return await api(endpoint, {...data, picker_id: pickerId}); }
  finally {
    localPickers.delete(pickerId);
    control.disabled = false; control.textContent = "Browse…"; control.removeAttribute("aria-busy");
  }
}
function cancelLocalPickers(owner) {
  for (const [pickerId, pickerOwner] of localPickers) if (pickerOwner === owner) {
    api("/api/picker/cancel", {picker_id: pickerId}).catch(error => toast(error.message));
  }
}
function active(job) { return ["running", "cancelling"].includes(job.status); }
function vmJob(name) { return state.jobs.find((job) => active(job) && job.name === name); }
function platformLabel(os) { return catalog.images[os]?.label || os; }
function osIcon(os) {
  const id = catalog?.images[os]?.icon || os;
  const supported = ["ubuntu", "kali", "rhel", "fedora", "debian", "rocky", "macos", "other"];
  const icon = node("div", undefined, "os-icon " + (supported.includes(id) ? id : "other"));
  const image = node("img"); image.src = `/icons/${supported.includes(id) ? id : "other"}.svg`; image.alt = ""; image.setAttribute("aria-hidden", "true"); icon.append(image);
  return icon;
}
function button(text, fn, primary = false) {
  const element = node("button", text, primary ? "primary" : "secondary");
  if (text !== undefined) element.setAttribute("data-live-key", text);
  element.type = "button"; element.onclick = fn; return element;
}
function control(label, icon, fn) {
  const element = button(undefined, fn); element.classList.add("control", "control-" + icon);
  element.setAttribute("data-live-key", icon);
  element.setAttribute("aria-label", label); element.title = label;
  const image = node("img"); image.src = `/icons/${icon}.svg`; image.alt = ""; image.setAttribute("aria-hidden", "true");
  element.append(image); return element;
}
function switchControl(label, checked, fn) {
  const element = node("button", undefined, "toggle-button"); element.type = "button";
  element.setAttribute("role", "switch"); element.setAttribute("aria-label", label);
  element.setAttribute("aria-checked", String(checked));
  const track = node("span", undefined, "toggle-track"); track.setAttribute("aria-hidden", "true"); element.append(track);
  element.onclick = function () { return fn(this.getAttribute("aria-checked") !== "true", this); };
  return element;
}
function sshKeysEnabled() { return $("use-keys").getAttribute("aria-checked") === "true"; }
function defaultSSHKeys() { return catalog.install_ssh_key_by_default !== false && catalog.default_public_key ? [catalog.default_public_key] : []; }
async function submit(action, data) {
  await api("/api/action", {action, ...data});
  if (["create", "deploy-profile"].includes(action)) showView("vms");
  toast("Operation started. Follow its progress in Activity.");
  await refresh();
}
function dialog(title, description, fields, callback, confirm = "Confirm", danger = false) {
  const resetScroll = !$("action-dialog").open || $("action-title").textContent !== title;
  $("action-dialog").classList.remove("vm-details", "vm-logs", "vm-users", "catalog-dialog");
  logViewer = null;
  $("action-title").textContent = title; $("action-description").textContent = description;
  $("action-fields").replaceChildren(...fields); banner("action-error", null);
  $("confirm-action").textContent = confirm; $("confirm-action").hidden = !callback;
  $("confirm-action").classList.toggle("danger", danger); $("confirm-action").disabled = false;
  actionCallback = callback; $("action-dialog").showModal();
  if (resetScroll) $("action-dialog").scrollTop = 0;
}
function field(label, type, value, id) {
  const element = node("label", label); const input = node("input"); input.type = type; input.value = value ?? ""; if (id) input.id = id;
  element.append(input); return element;
}
function addBridge(container, initial = "", primary = $("bridge").value) {
  if (container.querySelectorAll("select").length >= 7) return;
  const row = node("div", undefined, "row"); const label = node("label", "Additional host network interface");
  const select = node("select"); select.className = "extra-bridge";
  for (const iface of catalog.interfaces) { const option = node("option", iface); option.value = iface; select.append(option); }
  const used = [primary, ...Array.from(container.querySelectorAll("select"), (item) => item.value)];
  select.value = initial || catalog.interfaces.find((iface) => !used.includes(iface)) || catalog.interfaces[0] || "";
  label.append(select); row.append(label, button("Remove", () => row.remove())); container.append(row);
}
function networkLabel(config) {
  return config.network !== "nat" ? "Bridged / " + (config.bridges?.length ? config.bridges : [config.bridge]).join(", ") : "NAT";
}
function forwardLabel(rule) { return rule.listen_interface ? `${rule.listen_interface} (current IP)` : rule.listen_address; }
async function refreshListenAddresses() {
  try { catalog.listen_addresses = (await api("/api/choices")).listen_addresses; return true; }
  catch (error) { toast("Could not refresh Mac IP addresses: " + error.message); return false; }
}
function addForward(container, initial = {}) {
  if (container.children.length >= 32) return;
  const row = node("div", undefined, "connection-row");
  const listen = node("label", "Mac listen interface or IP"); const select = node("select"); select.dataset.forward = "listen_address";
  const choices = [...(catalog.listen_addresses || []).map((item) => [`@${item.interface}`, `${item.interface} · follow IP automatically (${item.address})`]), ...(catalog.listen_addresses || []).map((item) => [item.address, `Fixed IP · ${item.address}`]), ["0.0.0.0", "All Mac interfaces · 0.0.0.0"], ["127.0.0.1", "Local Mac only · 127.0.0.1"]];
  const selected = initial.listen_interface ? `@${initial.listen_interface}` : initial.listen_address;
  if (selected && !choices.some(([value]) => value === selected)) choices.unshift([selected, `${initial.listen_interface || initial.listen_address} · unavailable on this Mac`]);
  for (const [value, label] of choices) { const option = node("option", label); option.value = value; select.append(option); }
  select.value = selected || choices[0][0]; listen.append(select);
  const host = field("Mac host port", "number", initial.host_port || 8443); const target = field("VM service port", "number", initial.guest_port || 443);
  host.querySelector("input").dataset.forward = "host_port"; target.querySelector("input").dataset.forward = "guest_port";
  for (const field of [host, target]) { field.querySelector("input").min = "1"; field.querySelector("input").max = "65535"; }
  const protocol = node("label", "Protocol"); const protocols = node("select"); protocols.dataset.forward = "protocol";
  for (const value of ["tcp", "udp"]) { const option = node("option", value.toUpperCase()); option.value = value; protocols.append(option); }
  protocols.value = initial.protocol || "tcp"; protocol.append(protocols);
  const ports = node("div", undefined, "row"); ports.append(host, target, protocol);
  row.append(listen, ports, button("Remove forward", () => row.remove())); container.append(row);
}
function readForwards(container) {
  return Array.from(container.children, (row) => {
    const rule = Object.fromEntries(Array.from(row.querySelectorAll("[data-forward]"), (input) => [input.dataset.forward, input.type === "number" ? Number(input.value) : input.value]));
    if (rule.listen_address.startsWith("@")) { rule.listen_interface = rule.listen_address.slice(1); rule.listen_address = catalog.listen_addresses.find((item) => item.interface === rule.listen_interface)?.address || "0.0.0.0"; }
    return rule;
  });
}
function addShare(container, initial = {}) {
  if (container.children.length >= 8) return;
  const row = node("div", undefined, "connection-row"); const host = field("Local Directory", "text", initial.host_path || ""); const guest = field("Mount In VM", "text", initial.guest_path || "/mnt/share" + (container.children.length || ""));
  const input = host.querySelector("input"); input.dataset.share = "host_path"; input.placeholder = "~/Projects"; input.setAttribute("aria-label", "Local Directory"); guest.querySelector("input").dataset.share = "guest_path";
  const browse = button("Browse…", async () => {
    try {
      const result = await browseLocal(browse, "/api/directory/browse", {}, container.id === "directory-shares" ? "wizard" : "action-dialog");
      if (result.path && row.parentNode === container) input.value = result.path;
    } catch (error) { toast(error.message); }
  });
  browse.setAttribute("aria-label", "Browse for local directory");
  const source = node("span", undefined, "directory-source-control"); source.append(input, browse); host.append(source);
  const paths = node("div", undefined, "row"); paths.append(host, guest);
  const label = node("label", " Read-only", "check"); const checkbox = node("input"); checkbox.type = "checkbox"; checkbox.checked = initial.read_only !== false; checkbox.dataset.share = "read_only"; label.prepend(checkbox);
  row.append(paths, label, button("Remove share", () => row.remove())); container.append(row);
}
function readShares(container) {
  return Array.from(container.children, (row) => Object.fromEntries(Array.from(row.querySelectorAll("[data-share]"), (input) => [input.dataset.share, input.type === "checkbox" ? input.checked : input.value.trim()])));
}
function passwordDialog(machine, action) {
  const hasKeys = !["cloud", "golden"].includes(machine.config.source_kind);
  const credentials = credentialFields(machine.config, "resume");
  const buildDescription = machine.config.source_kind === "golden"
    ? "Boot the golden-image copy to finish cloud-init setup and verify it through its inherited guest agent. SSH is used only if the agent is unavailable. The VM stops when the build finishes."
    : "Build a VM from the downloaded source. Guest setup may boot it briefly and then stop it.";
  dialog(action === "finish" ? "Finish Guest Setup" : "Build Virtual Machine",
    action === "finish" ? "Confirm that you completed the OS installation, enabled SSH if adding keys, and shut down the guest. Setup will boot from the installed disk to add your keys." : buildDescription,
    [...(hasKeys ? [field("Template Login Password", "password", "", "action-password")] : []), ...credentials.fields],
    () => submit(action, {name: machine.config.name, config: machine.config, password: hasKeys ? $("action-password").value : "", ...credentials.data()}), action === "finish" ? "Installation complete · Finish setup" : "Build VM");
}
function saveGolden(machine) {
  dialog("Save Golden Image", "Clone this stopped VM and prepare the copy for reuse. Its installed applications stay in the image. New VMs get their own hostname, machine ID and SSH keys.",
    [field("Image name", "text", machine.config.name + "-golden", "golden-name"), field("Version", "text", "1", "golden-version"), multiline("Notes", "", "golden-notes")],
    () => submit("golden", {name: machine.config.name, image_name: $("golden-name").value.trim(), version: $("golden-version").value, notes: $("golden-notes").value}), "Save golden image");
}
function goldenOptions() {
  const previous = $("golden-source").value; const options = [];
  for (const image of (state.images || []).filter((item) => item.os === guest && item.phase === "ready")) { const option = node("option", `${image.name} · ${image.disk_gb} GB disk`); option.value = image.name; option.setAttribute("data-live-key", image.name); options.push(option); }
  reconcileChildren($("golden-source"), options);
  if (Array.from($("golden-source").options).some((option) => option.value === previous)) $("golden-source").value = previous;
  $("golden-hint").textContent = $("golden-source").options.length ? "Installed software is retained." : "No golden images available.";
}
async function chooseGolden(image) {
  if (!await openWizard(image.os)) return;
  $("source-kind").value = "golden"; goldenOptions(); $("golden-source").value = image.name;
  $("disk").value = Math.max(Number($("disk").value), image.disk_gb); renderBundleChoices([]); syncSource();
}
function launchGoldenDialog(image) {
  let index = 1; const prefix = image.name.replace(/-golden$/, "").slice(0, 58); let name;
  do { name = `${prefix}-${index++}`; } while (state.machines.some(machine => machine.config.name === name) || state.jobs.some(job => active(job) && job.name === name));
  dialog("Launch From " + image.name, "Small · NAT · " + (defaultSSHKeys().length ? "Default SSH Public Key" : "Guest Agent Management"),
    [field("New VM name", "text", name, "golden-vm-name")],
    () => submit("create", {config: {
      name: $("golden-vm-name").value.trim(), os: image.os, size: "small", network: "nat",
      disk_gb: Math.max(catalog.sizes.small.disk_gb, image.disk_gb), source_kind: "golden", source: image.name,
      ssh_user: catalog.images[image.os]?.ssh_user || "vmadmin", ssh_public_keys: defaultSSHKeys(),
      packages: [], software_bundles: []
    }, headless: true}), "Launch VM");
}
function renderImages() {
  const list = $("golden-images"); const rows = [];
  for (const image of state.images || []) {
    const row = node("article", undefined, "library-row"); row.setAttribute("data-live-key", image.name);
    row.append(osIcon(image.os));
    const content = node("div", undefined, "library-content"); content.setAttribute("data-live-key", "content");
    const title = node("h3", image.name); title.append(node("span", image.phase === "ready" ? "Ready" : image.phase, "badge"));
    content.append(title, node("div", `${platformLabel(image.os)} · version ${image.version} · ${image.disk_gb} GB disk`, "detail"));
    if (image.notes) content.append(node("p", image.notes, "library-notes"));
    content.append(node("small", "Built " + date(image.created_at)));
    if (image.references?.length) content.append(node("small", image.references.join(" · ")));
    const actions = node("div", undefined, "actions"); actions.setAttribute("data-live-key", "actions");
    if (image.phase === "ready") actions.append(control("Launch VM from " + image.name, "start", () => launchGoldenDialog(image)),
      control("Customize VM from " + image.name, "configure", () => chooseGolden(image)));
    actions.append(control("Details / notes for " + image.name, "details", () => editImageMetadata(image)));
    const remove = control("Delete golden image " + image.name, "destroy", () => deleteImageDialog(image));
    remove.disabled = (state.jobs || []).some(job => active(job) && job.resources?.includes("image:" + image.name));
    actions.append(remove); row.append(content, actions); rows.push(row);
  }
  if (!rows.length) rows.push(node("p", "No golden images", "muted"));
  reconcileChildren(list, rows);
}
async function edit(machine) {
  if (!await refreshListenAddresses()) return;
  const config = machine.config;
  const network = node("label", "Network"); const select = node("select"); select.id = "edit-network";
  for (const value of ["nat", "bridged"]) { const option = node("option", {nat: "NAT", bridged: "Bridged"}[value]); option.value = value; select.append(option); }
  select.value = config.network; network.append(select);
  const bridge = node("label", "Bridge interface"); const interfaces = $("bridge").cloneNode(true); interfaces.id = "edit-bridge";
  interfaces.value = config.bridge || catalog.interfaces[0] || ""; bridge.append(interfaces);
  const extra = node("div"); for (const iface of (config.bridges || []).slice(1)) addBridge(extra, iface, interfaces.value);
  const add = button("＋ Add network interface", () => addBridge(extra, "", interfaces.value));
  const adapterFields = node("div"); adapterFields.append(bridge, extra, add);
  const sync = () => { adapterFields.hidden = select.value === "nat"; }; select.addEventListener("change", sync); sync();
  const forwards = node("div"); for (const rule of config.port_forwards || []) addForward(forwards, rule);
  const forwardFields = node("div"); forwardFields.append(node("h4", "Port Forwarding"), node("p", "External host → Mac listen IP : port → VM NAT IP : service port", "hint"), forwards, button("＋ Add port forward", () => addForward(forwards)));
  const shares = node("div"); for (const share of config.directory_shares || []) addShare(shares, share);
  const shareFields = node("div"); shareFields.append(node("h4", "Directory Shares"), shares, button("＋ Add Directory Share", () => addShare(shares)));
  if (config.guest_family !== "macos" && !["cloud", "golden"].includes(config.source_kind)) shareFields.append(node("p", "Linux guests require a VirtioFS mount in the guest for each directory share.", "hint"));
  const fields = [field("CPU Cores", "number", config.cpu, "edit-cpu"), field("Memory (MB)", "number", config.memory_mb, "edit-memory"), field("Disk (GB)", "number", config.disk_gb, "edit-disk"), network, adapterFields, forwardFields, shareFields];
  dialog("Configure " + config.name, "Stop the VM before editing. Disk capacity can only grow. Network and directory share changes apply on the next start.", fields,
    () => submit("configure", {config: {...config, cpu: Number($("edit-cpu").value), memory_mb: Number($("edit-memory").value), disk_gb: Number($("edit-disk").value), network: select.value, bridge: select.value !== "nat" ? interfaces.value : "", bridges: select.value !== "nat" ? [interfaces.value, ...Array.from(extra.querySelectorAll("select"), (item) => item.value)] : [], port_forwards: readForwards(forwards), directory_shares: readShares(shares)}}), "Save Configuration");
}
function destroy(machine) {
  const name = machine.config.name;
  dialog("Destroy " + name + "?", "This permanently deletes the VM and its disk. Downloaded source images stay cached. Stop the VM first, then type its name to confirm.",
    [field("Type " + name, "text", "", "destroy-name")], async () => {
      if ($("destroy-name").value !== name) throw new Error("The name must match exactly.");
      await submit("destroy", {name, confirmation: $("destroy-name").value});
    }, "Destroy VM", true);
}
async function information(machine, type) {
  await openMachineLogs(machine.config.name);
}
function renderMachines() {
  const managed = state.machines.filter((machine) => machine.managed);
  setLiveText($("total-count"), state.machines.length);
  setLiveText($("running-count"), state.machines.filter((machine) => machine.running).length);
  setLiveText($("ready-count"), managed.filter((machine) => machine.phase === "ready" && machine.exists).length);
  setLiveText($("machine-count"), state.machines.length);
  const list = $("machines"); const cards = [];
  const filtered = state.machines.filter((machine) => machine.config.name.toLowerCase().includes($("search").value.toLowerCase()));
  if (!filtered.length) {
    const empty = node("div", undefined, "empty"); empty.append(node("strong", state.machines.length ? "No Matching Virtual Machines" : "No Virtual Machines"), button("＋ New Virtual Machine", () => openWizard(), true)); reconcileChildren(list, [empty]); return;
  }
  for (const machine of filtered) {
    const config = machine.config; const card = node("article", undefined, "machine");
    card.setAttribute("data-live-key", config.name);
    card.append(osIcon(config.os));
    const content = node("div"); const title = node("h3", config.name);
    content.setAttribute("data-live-key", "content");
    const health = addresses.get(config.name)?.health; const job = vmJob(config.name);
    const label = job ? ({start: "Starting", restart: "Restarting", shutdown: "Shutting down", "force-stop": "Stopping", checkpoint: "Saving checkpoint", restore: "Restoring", agent: "Installing agent", users: "Adding users"}[job.action] || "Working") : machine.running ? (!machine.managed ? "Running" : health?.ssh_ready ? "SSH ready" : health?.agent_privileged ? "Agent ready" : health?.ip ? "Running" : "Booting") : machine.phase === "ready" ? "Ready" : {downloaded: "Downloaded", "awaiting-installation": "Needs installation", installing: "Finish setup", created: "Build incomplete", "restore-failed": "Restore incomplete", external: "External VM"}[machine.phase] || machine.phase;
    title.append(node("span", label, "badge " + (machine.running ? "running" : machine.phase.includes("install") ? "installing" : "")));
    content.append(title, node("div", machine.managed ? `${platformLabel(config.os)} · ${config.cpu} CPU · ${config.memory_mb / 1024} GB RAM · ${config.disk_gb} GB disk · ${networkLabel(config)}` : "Created outside AppleTart. Managed in Tart.", "detail"));
    if (config.port_forwards?.length) content.append(node("div", config.port_forwards.map((rule) => `${rule.protocol.toUpperCase()} ${forwardLabel(rule)}:${rule.host_port} → VM:${rule.guest_port}`).join(" · ") + (machine.forwarding_active ? " · Active" : " · Inactive"), "detail"));
    if (config.directory_shares?.length) content.append(node("div", config.directory_shares.map((share) => `${share.host_path} → ${share.guest_path} (${share.read_only ? "read-only" : "read-write"})`).join(" · "), "detail"));
    card.append(content);
    const actions = node("div", undefined, "actions");
    actions.setAttribute("data-live-key", "actions");
    if (machine.managed) {
      if (machine.running && machine.phase === "ready") actions.append(addressButton(machine));
      if (job) {
        if (job.cancellable) { const cancel = control(job.status === "cancelling" ? "Cancelling…" : "Cancel build", "cancel", () => cancelJob(job)); cancel.disabled = job.status === "cancelling"; actions.append(cancel); }
      } else {
      if (machine.running) actions.append(control("Restart", "restart", () => powerAction(machine, "restart")), control("Force stop", "force-stop", () => powerAction(machine, "force-stop")));
      else if (!machine.exists && !machine.owned) actions.append(control("Build", "build", () => passwordDialog(machine, "build")));
      else if (machine.exists && machine.phase === "created") actions.append(control("Resume build", "build", () => passwordDialog(machine, "build")));
      if (machine.exists && machine.phase.includes("install") && !machine.running) actions.append(control("Finish setup", "finish", () => passwordDialog(machine, "finish")));
      if (!machine.running) actions.append(control("Configure", "configure", () => edit(machine)));
      if (!machine.running && machine.exists && machine.phase === "ready" && config.guest_family !== "macos" && ["cloud", "golden", "tart"].includes(config.source_kind)) actions.append(control("Save as golden image", "golden", () => saveGolden(machine)));
      if (machine.exists && machine.phase === "ready") {
        const label = machine.guest_agent_privileged ? "Repair guest agent" : machine.guest_agent_version ? "Upgrade guest agent" : "Install guest agent";
        const description = machine.running ? "Uses privileged guest-agent management when available. The VM stays running. SSH fallback uses your selected key or the optional guest password." : "Boots briefly on NAT, uses privileged guest-agent management when available, then stops the VM. SSH fallback uses your selected key or the optional guest password.";
        actions.append(control(label, "agent", () => dialog(label, description,
          [field("Guest Login Password (SSH Fallback)", "password", "", "agent-password")],
          () => submit("agent", {name: config.name, password: $("agent-password").value}), label)));
      }
      if (!machine.running) actions.append(control("Destroy", "destroy", () => destroy(machine)));
      }
      if (machine.running && health?.ip && !job) {
        const ssh = control(sshLaunching.has(config.name) ? "Opening SSH…" : "SSH", "ssh", () => sshShortcut(machine, true));
        ssh.disabled = sshLaunching.has(config.name); ssh.setAttribute("aria-busy", String(ssh.disabled)); actions.append(ssh);
      }
      if (machine.exists && machine.owned && machine.phase === "ready" && !job) actions.append(sshConfigControl(machine, health?.ip));
      if (machine.exists && machine.owned && machine.phase === "ready" && !job) actions.append(control("Manage users", "users", () => manageUsers(machine)));
      if (machine.exists && machine.owned) actions.append(control("Details", "details", () => machineDetails(machine)));
      actions.append(control("Log", "log", () => information(machine, "log")));
      const power = switchControl("Power for " + config.name, machine.running, async (on, toggle) => {
        toggle.disabled = true; toggle.setAttribute("aria-busy", "true");
        try { await submit(on ? "start" : "shutdown", {name: config.name}); }
        catch (error) { toggle.disabled = false; toggle.setAttribute("aria-busy", "false"); toast(error.message); }
      });
      power.classList.add("vm-power"); power.setAttribute("data-live-key", "power");
      power.disabled = !!job || !machine.exists || !machine.owned || (!machine.running && ["created", "restore-failed"].includes(machine.phase));
      power.setAttribute("aria-busy", String(!!job));
      power.title = job ? label + "…" : !machine.exists ? "Build this VM before starting it" : power.disabled ? "Complete or recover this VM's build before starting it" : machine.running ? "Shut down " + config.name : machine.phase.includes("install") ? "Open installer for " + config.name : "Start " + config.name;
      card.classList.add("managed");
      card.insertBefore(power, card.firstChild);
    }
    card.append(actions); cards.push(card);
  }
  reconcileChildren(list, cards);
}
async function cancelJob(job) {
  try { await api("/api/cancel", {id: job.id}); toast("Cancellation requested. Waiting for build cleanup."); await refresh(); }
  catch (error) { toast(error.message); }
}
async function copyJobLog(job) {
  const card = Array.from($("job-list").children).find(element => liveKey(element) === job.id);
  const log = card?.querySelector("pre");
  if (!log) return;
  try {
    await navigator.clipboard.writeText(`${job.name} · ${job.action} (${job.status})\n\n${log.textContent}`);
    const until = Date.now() + 1800;
    copiedLogs.set(job.id, until); renderJobs();
    setTimeout(() => { if (copiedLogs.get(job.id) === until) copiedLogs.delete(job.id); renderJobs(); }, 1800);
  } catch (_) { toast("Clipboard access was blocked. Select and copy the log manually."); }
}
function renderJobs() {
  const running = state.jobs.filter(active);
  setLiveText($("job-status"), running.length ? `${running.length} operation${running.length === 1 ? "" : "s"} in progress` : "No active operations");
  const list = $("job-list"); const cards = [];
  const visible = [...running, ...state.jobs.filter((job) => !active(job)).slice(-4).reverse()];
  for (const job of visible) {
    const card = node("article", undefined, "job-card"); const header = node("div", undefined, "section-heading");
    card.setAttribute("data-live-key", job.id);
    header.append(node("strong", `${job.name} · ${job.action}`), node("span", job.status, "badge"));
    const copied = copiedLogs.get(job.id) > Date.now();
    const copy = control(`${copied ? "Copied" : "Copy"} log for ${job.name} ${job.action}`, "copy", () => copyJobLog(job)); copy.setAttribute("data-live-key", "copy-log");
    copy.classList.add("log-copy"); copy.classList.toggle("copied", copied);
    copy.title = copied ? "Log copied to clipboard" : "Copy this log, including received history";
    header.append(copy);
    header.append(control(`Detailed log for ${job.name} ${job.action}`, "log", () => openMachineLogs(job.name, job.id)));
    if (active(job) && job.cancellable) {
      const label = job.status === "cancelling" ? "Cancelling…" : ["create", "deploy-profile"].includes(job.action) ? "Cancel deploy" : job.action === "download" ? "Cancel download" : "Cancel build";
      const cancel = control(`${label} for ${job.name}`, "cancel", () => cancelJob(job));
      cancel.title = label; cancel.disabled = job.status === "cancelling"; header.append(cancel);
    }
    const log = node("pre", job.lines.join("\n") || "Preparing…"); log.setAttribute("data-live-log", ""); log.setAttribute("tabindex", "0"); log.setAttribute("aria-label", `${job.name} ${job.action} log`); log.setAttribute("aria-live", "polite"); card.append(header, log); cards.push(card);
  }
  if (!visible.length) cards.push(node("p", "No operations", "muted"));
  reconcileChildren(list, cards);
}
async function refresh() {
  if (polling) return; polling = true;
  try {
    state = await api("/api/state"); banner("error-banner", state.errors.length ? state.errors.join("\n") : null);
    for (const [name] of addresses) if (!state.machines.some((machine) => machine.config.name === name && machine.running) || vmJob(name)) addresses.delete(name);
    for (const machine of state.machines) if (machine.managed && machine.running && machine.phase === "ready" && !vmJob(machine.config.name)) discoverAddress(machine);
    renderMachines(); renderImages(); renderProfiles(); goldenOptions(); renderJobs();
    refreshLogViewer();
    const latest = state.jobs[state.jobs.length - 1];
    if (latest) {
      const identifier = latest.id + latest.status;
      if (identifier !== lastJob && !active(latest)) { toast(latest.status === "complete" ? "Operation complete." : latest.status === "cancelled" ? "Operation cancelled." : "Operation failed. See Activity for details."); if (currentView === "settings") loadStorage(); }
      lastJob = identifier;
    }
  } catch (error) { banner("error-banner", error); }
  finally { polling = false; }
}
function showStep(next) {
  step = next;
  document.querySelectorAll(".step").forEach((element) => { element.hidden = Number(element.dataset.step) !== step; });
  document.querySelectorAll(".steps li").forEach((element, index) => element.classList.toggle("current", index === step));
  $("back").hidden = step === 0; $("final-actions").hidden = step !== 3;
  for (const id of ["download-only", "build-only", "save-profile"]) $(id).hidden = !!editingProfile;
  const localImage = ["cloud", "iso"].includes($("source-kind").value) && $("source-location").value === "local";
  $("next").textContent = step === 3 ? editingProfile ? "Save changes" : localImage ? "Build & deploy →" : "Download, build & deploy →" : "Continue →";
  $("download-only").textContent = localImage ? "Prepare image only" : "Download only";
  banner("wizard-error", null); $("wizard").scrollTop = 0;
}
function chooseOS(value) {
  guest = value; const image = catalog.images[value];
  document.querySelectorAll("#os-options .tile").forEach((element) => element.classList.toggle("selected", element.dataset.os === guest));
  $("vm-name").value = editingProfile?.config.name || (value === "other" ? "linux-dev" : value + "-dev");
  $("source-kind").value = image.source_kind; $("source").value = image.source; $("source").dataset.checksumSource = image.source; $("source-location").value = "remote"; $("checksum-algorithm").value = "sha256"; $("sha256").value = image.sha512 || image.sha256 || ""; $("checksum-algorithm").value = image.sha512 ? "sha512" : "sha256"; checksumHint();
  $("ssh-user").value = image.ssh_user || "vmadmin"; $("password").value = image.source_kind === "tart" ? image.bootstrap_password || "" : "";
  $("headless").checked = image.family !== "macos"; $("refresh-source").checked = false; $("packages").value = ""; renderBundleChoices([]); populateVersions(); $("image-hint").textContent = image.description; goldenOptions(); syncSource();
}
function chooseSize(value) {
  size = value; const preset = catalog.sizes[value];
  $("cpu").value = preset.cpu; $("memory").value = preset.memory_mb; $("disk").value = Math.max(preset.disk_gb, minimumImageDisk());
  document.querySelectorAll("#size-options .tile").forEach((element) => element.classList.toggle("selected", element.dataset.size === size));
}
function syncSource() {
  const golden = $("source-kind").value === "golden"; const cloud = golden || $("source-kind").value === "cloud";
  const file = ["cloud", "iso"].includes($("source-kind").value); const local = file && $("source-location").value === "local";
  $("golden-label").hidden = !golden; $("source-label").hidden = golden; $("source").required = !golden;
  $("source-location-label").hidden = !file;
  $("browse-image").hidden = !local;
  $("source-caption").textContent = file ? local ? "Local image path" : "Image download URL" : "Image source";
  $("source").type = file && !local ? "url" : "text";
  $("source").pattern = file && !local ? "https://.+" : ".*";
  $("source-help").hidden = !file;
  $("source-help").textContent = local ? cloud ? "Local ARM64 disk: .tar.xz, .qcow2.xz, .qcow2, .raw or .img." : "Local ARM64 installer: .iso." : "ARM64 image URL (HTTPS).";
  $("checksum-label").hidden = golden || $("source-kind").value === "tart";
  $("headless-label").hidden = !!editingProfile || $("source-kind").value === "iso";
  $("source").placeholder = file ? local ? cloud ? "~/Downloads/debian-12-genericcloud-arm64.qcow2" : "~/Downloads/installer-arm64.iso" : cloud ? "https://…/arm64.tar.xz" : "https://…/arm64.iso" : "ghcr.io/… or a local template name";
  $("cloud-options").hidden = !cloud;
  $("software-options").hidden = $("source-kind").value === "iso";
  $("refresh-source-label").hidden = !file || local;
  $("platform-version-label").hidden = !catalog.images[guest]?.versions?.length || golden;
  $("add-platform-version").hidden = golden;
  const macos = catalog.images[guest]?.family === "macos";
  for (const option of $("source-kind").options) option.disabled = macos && option.value !== "tart";
  updateSoftwareWarning();
  $("password-label").hidden = cloud; if (cloud) $("password").value = "";
  $("username-label").textContent = cloud ? "Create Guest Username" : "Existing Guest Username";
  $("ssh-hint").textContent = cloud ? sshKeysEnabled() ? "Passwordless Sudo · SSH Public Key · Guest Agent" : "Passwordless Sudo · Guest Agent · No Personal SSH Key" : $("source-kind").value === "iso" ? "Enable SSH in the guest before Finish setup." : "SSH-enabled template required. Initial setup upgrades packages and installs the guest agent.";
}
function config() {
  const network = document.querySelector('input[name="network"]:checked').value;
  if (sshKeysEnabled() && !$("public-keys").value.trim()) throw new Error("Select a .pub file or turn off SSH key installation.");
  const golden = $("source-kind").value === "golden"; const cloud = golden || $("source-kind").value === "cloud";
  return {name: editingProfile?.config.name || $("vm-name").value.trim(), os: guest, size, source_kind: $("source-kind").value, source: golden ? $("golden-source").value : $("source").value.trim(), sha256: !golden && $("source-kind").value !== "tart" && $("checksum-algorithm").value === "sha256" ? $("sha256").value.trim() : "", sha512: !golden && $("source-kind").value !== "tart" && $("checksum-algorithm").value === "sha512" ? $("sha256").value.trim() : "",
    cpu: Number($("cpu").value), memory_mb: Number($("memory").value), disk_gb: Number($("disk").value), network, bridge: network !== "nat" ? $("bridge").value : "", bridges: network !== "nat" ? [$("bridge").value, ...Array.from($("extra-bridges").querySelectorAll("select"), (item) => item.value)] : [], port_forwards: readForwards($("port-forwards")), directory_shares: readShares($("directory-shares")),
    ssh_user: $("ssh-user").value.trim() || "vmadmin", ssh_public_keys: sshKeysEnabled() ? $("public-keys").value.split("\n").map((value) => value.trim()).filter(Boolean) : [],
    guest_family: catalog.images[guest]?.family || "linux", password_login: !!$("guest-password").value || $("guest-password").dataset.passwordLogin === "true", users: buildUserEditor?.recipes() || [], refresh_source: !golden && ["cloud", "iso"].includes($("source-kind").value) && $("source-location").value !== "local" && $("refresh-source").checked,
    packages: $("source-kind").value !== "iso" ? $("packages").value.split(/[\s,]+/).filter(Boolean) : [], software_bundles: $("source-kind").value !== "iso" ? selectedBundles() : [],
    ...(cloud ? {hostname: $("hostname").value.trim(), desktop: "none"} : {})};
}
async function review() {
  const result = await api("/api/preview", {config: config(), ...(editingProfile ? {} : buildCredentials())});
  const vm = result.config; $("review").replaceChildren();
  for (const [label, value] of [["Machine", vm.name], ["Guest", platformLabel(vm.os)], ["Resources", `${vm.cpu} CPUs · ${vm.memory_mb / 1024} GB RAM · ${vm.disk_gb} GB disk`], ["Network", networkLabel(vm)], ["Image Source", vm.source], ["SSH Access", result.key_count ? `${result.key_count} public key(s) for ${vm.ssh_user}` : ["cloud", "golden"].includes(vm.source_kind) ? "Guest Agent · No Personal SSH Key" : "Keep template access unchanged"]]) {
    const item = node("div"); item.append(node("span", label), node("strong", value)); $("review").append(item);
  }
  const access = node("div"); access.append(node("span", "Guest Account"), node("strong", `${vm.ssh_user} · ${vm.password_login ? "Password Login" : "No New Password"} · Passwordless Sudo`)); $("review").append(access);
  for (const user of vm.users || []) { const item = node("div"); item.append(node("span", "Additional User"), node("strong", `${user.username} · ${user.ssh_authorized_keys.length} Public Key(s)${user.password_login ? " · Password Login" : ""} · Passwordless Sudo`)); $("review").append(item); }
  if (["cloud", "iso"].includes(vm.source_kind)) { const item = node("div"); item.append(node("span", "Image Verification"), node("strong", vm.sha512 ? "SHA-512 · " + vm.sha512 : vm.sha256 ? "SHA-256 · " + vm.sha256 : "No publisher checksum supplied")); $("review").append(item); }
  if (["cloud", "golden"].includes(vm.source_kind)) {
    const item = node("div"); item.append(node("span", "First Boot"), node("strong", `${vm.hostname} · Headless · ${new Set([...(vm.packages || []), ...(vm.bundle_packages || [])]).size} requested packages`)); $("review").append(item);
  }
  for (const rule of vm.port_forwards || []) { const item = node("div"); item.append(node("span", "Port Forward"), node("strong", `${rule.protocol.toUpperCase()} · ${forwardLabel(rule)}:${rule.host_port} → VM NAT:${rule.guest_port}`)); $("review").append(item); }
  for (const [index, share] of (vm.directory_shares || []).entries()) { const item = node("div"); item.append(node("span", "Shared Directory"), node("strong", `${share.host_path} → ${share.guest_path} · ${share.read_only ? "read-only" : "read-write"}`)); if (!["cloud", "golden"].includes(vm.source_kind)) item.append(node("small", vm.guest_family === "macos" ? `Guest: sudo mkdir -p ${share.guest_path}; sudo mount_virtiofs appletart-share${index} ${share.guest_path} (macOS 13+)` : `Guest: sudo mkdir -p ${share.guest_path}; sudo mount -t virtiofs -o ${share.read_only ? "ro" : "rw"} appletart-share${index} ${share.guest_path}`)); $("review").append(item); }
  $("review-note").textContent = vm.source_kind === "golden" ? "Golden image clone. Installed software is retained." : vm.source_kind === "cloud" ? "Unattended cloud-init provisioning." : result.installation_required ? "Manual installation required. After installation, enable SSH, shut down the VM, and select Finish setup." : "Tart image provisioning.";
  const software = new Set([...(vm.packages || []), ...(vm.bundle_packages || [])]);
  if (software.size) {
    const item = node("div"); item.append(node("span", "Software"), node("strong", `${(vm.software_bundles || []).map(id => catalog.bundles.find(bundle => bundle.id === id)?.name || id).join(", ") || "Additional packages"} · ${software.size} packages`)); $("review").append(item);
  }
  if (vm.guest_family === "linux" && vm.source_kind !== "golden") $("review-note").textContent += "\n\n" + catalog.linux_build_notice;
  else if (software.size) $("review-note").textContent += "\n\n" + catalog.software_notice;
  if (vm.refresh_source) $("review-note").textContent += "\n\nThe source will be downloaded again.";
  if (vm.source_kind === "iso" && (vm.password_login || vm.users?.length)) $("review-note").textContent += "\n\nUser accounts are applied during Finish Setup. Enter passwords again at that step.";
  if (editingProfile) $("review-note").textContent = "Save changes to " + editingProfile.config.name + ". Future deployments use these settings. Passwords are requested when deploying.";
  showStep(3);
}
async function openWizard(value = "ubuntu", profile = null) {
  if (!catalog || !await refreshListenAddresses()) return false;
  editingProfile = profile;
  $("wizard-title").textContent = profile ? "Edit Deployment Profile" : "Create Virtual Machine";
  $("vm-name-label").textContent = profile ? "Profile name" : "Machine name";
  $("vm-name").readOnly = !!profile; $("deployment-profile").disabled = !!profile;
  $("edit-profile-notes-label").hidden = !profile;
  if (!catalog.images[value]) value = Object.keys(catalog.images)[0];
  $("vm-form").reset(); clearBuildCredentials(); for (const element of $("wizard").querySelectorAll("details")) element.open = false; $("use-keys").setAttribute("aria-checked", String(defaultSSHKeys().length > 0)); for (const id of ["extra-bridges", "port-forwards", "directory-shares"]) $(id).replaceChildren(); chooseOS(value); chooseSize("small"); $("public-keys").value = catalog.default_public_key || "";
  $("edit-profile-notes").value = profile?.notes || "";
  $("bridge-label").hidden = true; $("ssh-fields").hidden = false; showStep(0); $("wizard").showModal();
  return true;
}
async function create(action) {
  $("next").disabled = true;
  try { await submit(action, {config: config(), password: $("password").value, ...buildCredentials(), headless: $("headless").checked}); clearBuildCredentials(); $("wizard").close(); }
  catch (error) { banner("wizard-error", error); }
  finally { $("next").disabled = false; }
}
async function initialize() {
  $("vm-form").noValidate = true;
  catalog = await api("/api/choices");
  renderPlatforms(); initializeSoftwareSettings();
  initializeBuildUsers();
  $("wizard").addEventListener("close", clearBuildCredentials);
  $("guest-password").addEventListener("input", () => { $("guest-password").dataset.passwordLogin = "false"; });
  for (const [id, preset] of Object.entries(catalog.sizes)) {
    const tile = node("button", undefined, "tile"); tile.type = "button"; tile.dataset.size = id;
    tile.append(node("strong", id[0].toUpperCase() + id.slice(1)), node("small", `${preset.cpu} CPU cores\n${preset.memory_mb / 1024} GB memory · ${preset.disk_gb} GB disk`));
    tile.addEventListener("click", () => chooseSize(id)); $("size-options").append(tile);
  }
  for (const iface of catalog.interfaces) { const option = node("option", iface); option.value = iface; $("bridge").append(option); }
  $("vm-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (step === 3) return editingProfile ? saveEditedProfile() : create("create");
    const fields = document.querySelectorAll(`.step[data-step="${step}"] input, .step[data-step="${step}"] select`);
    for (const input of fields) if (!input.closest("[hidden]") && !input.reportValidity()) return;
    try { if (step === 2) await review(); else showStep(step + 1); } catch (error) { banner("wizard-error", error); }
  });
  $("back").addEventListener("click", () => showStep(step - 1));
  $("source-kind").addEventListener("change", () => { $("source").value = ""; $("sha256").value = ""; $("password").value = ""; $("platform-version").value = ""; goldenOptions(); syncSource(); });
  $("source-location").addEventListener("change", () => { $("source").value = ""; $("sha256").value = ""; delete $("source").dataset.checksumSource; syncSource(); });
  $("browse-image").addEventListener("click", async () => {
    const kind = $("source-kind").value;
    try {
      const result = await browseLocal($("browse-image"), "/api/image/browse", {source_kind: kind}, "wizard");
      if (result.path && $("wizard").open && $("source-kind").value === kind && $("source-location").value === "local") {
        $("source").value = result.path; $("sha256").value = ""; delete $("source").dataset.checksumSource; banner("wizard-error", null);
      }
    } catch (error) { banner("wizard-error", error); }
  });
  $("source").addEventListener("input", () => { $("password").value = ""; $("platform-version").value = ""; if ($("source").dataset.checksumSource && $("source").value !== $("source").dataset.checksumSource) { $("sha256").value = ""; delete $("source").dataset.checksumSource; } });
  $("sha256").addEventListener("input", () => { delete $("source").dataset.checksumSource; });
  $("checksum-algorithm").addEventListener("change", () => { $("sha256").value = ""; checksumHint(); });
  $("deployment-profile").addEventListener("change", () => { const profile = (state.profiles || []).find((item) => item.config.name === $("deployment-profile").value); if (profile) applyProfile(profile); });
  $("import-profile").addEventListener("click", importProfileDialog);
  $("refresh-storage").addEventListener("click", loadStorage);
  $("cleanup-storage").addEventListener("click", cleanupStorageDialog);
  $("settings-form").addEventListener("submit", saveSettings);
  $("packages").addEventListener("input", updateSoftwareWarning);
  $("add-bridge").addEventListener("click", () => addBridge($("extra-bridges")));
  $("add-forward").addEventListener("click", () => addForward($("port-forwards")));
  $("add-share").addEventListener("click", () => addShare($("directory-shares")));
  document.querySelectorAll('input[name="network"]').forEach((input) => input.addEventListener("change", () => { $("bridge-label").hidden = input.value === "nat"; }));
  $("use-keys").addEventListener("click", () => { $("use-keys").setAttribute("aria-checked", String(!sshKeysEnabled())); syncSource(); });
  $("default-install-ssh-key").addEventListener("click", () => { const toggle = $("default-install-ssh-key"); toggle.setAttribute("aria-checked", String(toggle.getAttribute("aria-checked") !== "true")); });
  $("new-vm").addEventListener("click", () => openWizard());
  $("purge-unused-storage").addEventListener("click", purgeUnusedStorageDialog);
  $("close-wizard").addEventListener("click", () => { $("password").value = ""; $("wizard").close(); });
  $("wizard").addEventListener("close", () => { $("password").value = ""; cancelLocalPickers("wizard"); });
  $("action-dialog").addEventListener("close", () => cancelLocalPickers("action-dialog"));
  $("build-only").addEventListener("click", () => create("build")); $("download-only").addEventListener("click", () => create("download"));
  $("save-profile").addEventListener("click", () => { try { saveProfileDialog(config()); } catch (error) { banner("wizard-error", error); } });
  for (const id of ["close-action", "cancel-action"]) $(id).addEventListener("click", () => $("action-dialog").close());
  $("action-dialog").addEventListener("close", () => { $("action-fields").replaceChildren(); actionCallback = null; logViewer = null; });
  $("confirm-action").addEventListener("click", async () => {
    $("confirm-action").disabled = true;
    try { await actionCallback(); $("action-dialog").close(); } catch (error) { banner("action-error", error); }
    finally { $("confirm-action").disabled = false; }
  });
  $("refresh").addEventListener("click", refresh); $("search").addEventListener("input", renderMachines);
  for (const view of ["vms", "library", "settings"]) $("nav-" + view).addEventListener("click", () => showView(view));
  await refresh(); setInterval(refresh, 3000);
}
initializeTheme();
initialize().catch((error) => banner("error-banner", error));
