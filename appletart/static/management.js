"use strict";
let currentView = "vms", storageLoading = false;

function bytes(value) {
  if (!value) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const index = Math.min(4, Math.floor(Math.log(value) / Math.log(1024)));
  return `${(value / 1024 ** index).toFixed(index > 1 ? 1 : 0)} ${units[index]}`;
}
function date(value) { return value ? new Date(value).toLocaleString() : "Unknown"; }
function multiline(label, value, id) {
  const element = node("label", label); const input = node("textarea"); input.rows = 4; input.value = value || ""; input.id = id;
  element.append(input); return element;
}
function fact(label, value) {
  const row = node("div", undefined, "detail-fact"); row.append(node("strong", label), node("span", value)); return row;
}
const sshLaunching = new Set();
async function sshShortcut(machine, terminal = false) {
  const name = machine.config.name;
  if (sshLaunching.has(name)) return;
  sshLaunching.add(name); renderMachines();
  if (terminal) toast("Opening SSH…");
  try {
    const result = await api("/api/connection", {name: machine.config.name, terminal});
    if (!terminal) { await navigator.clipboard.writeText(result.command); toast("SSH command copied."); }
    else toast(`SSH connection opened in ${result.terminal}.`);
  } catch (error) { toast(error.message); }
  finally { sshLaunching.delete(name); renderMachines(); }
}
const sshConfigSaving = new Set();
function sshConfigControl(machine, ip, icon = true) {
  const name = machine.config.name, label = "Add To SSH Config";
  const element = icon ? control(label, "ssh-config", () => saveSSHConfig(machine)) : button(label, () => saveSSHConfig(machine));
  element.disabled = !ip || sshConfigSaving.has(name) || !!vmJob(name);
  element.title = sshConfigSaving.has(name) ? "Saving SSH Config…" : !machine.running ? "Start The VM To Add Its SSH Config" : !ip ? "Waiting For The VM IP" : label;
  return element;
}
async function saveSSHConfig(machine) {
  const name = machine.config.name;
  if (sshConfigSaving.has(name)) return;
  sshConfigSaving.add(name); renderMachines();
  try {
    const result = await api("/api/ssh-config", {name});
    toast(`${result.changed ? "SSH config saved" : "SSH config is current"}. Connect with ${result.command}.`);
  } catch (error) { toast(error.message); }
  finally { sshConfigSaving.delete(name); renderMachines(); }
}
function powerAction(machine, action) {
  const name = machine.config.name;
  if (action === "force-stop") dialog("Force Stop " + name + "?", "Immediately power off this VM. Unsaved guest data may be lost.", [],
    () => submit(action, {name}), "Force stop", true);
  else if (action === "restart") dialog("Restart " + name + "?", "Ask the guest to shut down cleanly, then boot with its saved settings. Active sessions will disconnect.", [],
    () => submit(action, {name}), "Restart");
  else submit("shutdown", {name}).catch((error) => toast(error.message));
}
function checkpointDialog(machine) {
  dialog("Create Checkpoint", "Save this stopped VM's disk and deployment configuration. Shared Mac directories are outside the recovery point.",
    [field("Checkpoint name", "text", "Before changes " + new Date().toISOString().slice(0, 10), "checkpoint-label"), multiline("Notes", "", "checkpoint-notes")],
    () => submit("checkpoint", {name: machine.config.name, label: $("checkpoint-label").value, notes: $("checkpoint-notes").value}), "Save checkpoint");
}
function restoreDialog(machine, item) {
  dialog("Restore " + item.label + "?", "Replace this stopped VM's disk and settings with this checkpoint. The current disk will be saved as a recovery point first. Shared Mac files are unchanged.",
    [field("Type " + machine.config.name, "text", "", "restore-confirm")],
    () => submit("restore", {name: machine.config.name, checkpoint_id: item.id, confirmation: $("restore-confirm").value}), "Restore checkpoint", true);
}
function deleteCheckpointDialog(machine, item) {
  dialog("Delete Checkpoint?", "Permanently remove this recovery point. The VM itself is preserved.",
    [field("Type " + item.label, "text", "", "checkpoint-confirm")],
    () => submit("delete-checkpoint", {name: machine.config.name, checkpoint_id: item.id, confirmation: $("checkpoint-confirm").value}), "Delete checkpoint", true);
}
function resourceCard(label, value, description) {
  const card = node("div", undefined, "vm-resource");
  card.append(node("span", label), node("strong", value), node("small", description)); return card;
}
function machineConfiguration(machine) {
  const config = machine.config;
  const resources = node("div", undefined, "vm-resource-grid");
  resources.append(resourceCard("CPU", `${config.cpu} cores`, "Virtual CPU allocation"),
    resourceCard("RAM", `${config.memory_mb / 1024} GB`, `${config.memory_mb} MB allocated`),
    resourceCard("Disk", `${config.disk_gb} GB`, "Maximum disk capacity"));
  const fields = [resources, node("h4", "Overview"), fact("Machine", config.name),
    fact("Guest OS", catalog.images[config.os]?.label || config.os), fact("Architecture", "ARM64"),
    fact("Tart state", machine.state || (machine.running ? "Running" : "Stopped")),
    fact("Build stage", machine.phase), fact("Created", date(machine.created_at)), fact("Display", config.guest_family === "macos" ? "macOS graphical / headless" : "Headless Linux")];
  fields.push(fact("Guest management", machine.guest_agent_privileged ? "Privileged guest agent · VSOCK" : "SSH fallback · upgrade agent to enable privileged management"));
  if (machine.last_restored_at) fields.push(fact("Last restored", date(machine.last_restored_at)));
  if (machine.identity?.home) fields.push(fact("VM directory", `${machine.identity.home}/vms/${config.name}`));
  fields.push(node("h4", "SSH Access"), fact("Guest username", config.ssh_user), fact("SSH port", "22"),
    fact("Password Login", config.password_login ? "Requested · Password Not Stored" : "No New Password Requested"),
    fact("Hostname", config.hostname || "Inherited from the template"),
    fact("Public key files", (config.ssh_public_keys || []).join("\n") || "None configured"));
  for (const user of config.users || []) fields.push(fact("Additional User", `${user.username} · ${user.ssh_authorized_keys.length} Public Key(s)${user.password_login ? " · Password Login" : ""} · Passwordless Sudo`));
  fields.push(node("h4", "Network Configuration"), fact("Mode", networkLabel(config)));
  const bridges = config.bridges?.length ? config.bridges : config.bridge ? [config.bridge] : [];
  fields.push(fact("Adapters", config.network === "nat" ? "1 NAT adapter" : `${bridges.length} bridged adapter${bridges.length === 1 ? "" : "s"}`));
  fields.push(node("h4", "Port Forwards"));
  for (const rule of config.port_forwards || []) fields.push(fact(rule.protocol.toUpperCase(), `${forwardLabel(rule)}:${rule.host_port} → VM:${rule.guest_port}`));
  if (!config.port_forwards?.length) fields.push(node("p", "No port forwards configured.", "muted"));
  fields.push(node("h4", "Directory Shares"));
  for (const share of config.directory_shares || []) fields.push(fact(share.guest_path, `${share.host_path} · ${share.read_only ? "read-only" : "read-write"}`));
  if (!config.directory_shares?.length) fields.push(node("p", "No directory shares configured.", "muted"));
  fields.push(node("h4", "Image & Build"), fact("Source type", {cloud: "ARM64 cloud image", golden: "Saved golden image", tart: "Tart image / template", iso: "ARM64 installer ISO"}[config.source_kind] || config.source_kind),
    fact("Source", config.source));
  if (config.sha512 || config.sha256) fields.push(fact(config.sha512 ? "SHA-512" : "SHA-256", config.sha512 || config.sha256));
  else fields.push(fact("Publisher checksum", "Not specified for this source"));
  if (machine.artifact) fields.push(fact("Local source", machine.artifact));
  if (machine.cloud_instance_id) fields.push(fact("Cloud instance", machine.cloud_instance_id));
  const requested = machine.requested_image || {};
  const requestedVersion = requested.version || catalog.images[config.os]?.versions?.find(item => item.source === config.source)?.label;
  if (requestedVersion) fields.push(fact("Requested image release", requestedVersion));
  if (requested.source && requested.source !== config.source) fields.push(fact("Original image source", requested.source));
  if (machine.guest_os?.name) fields.push(fact("Detected guest OS", machine.guest_os.pretty_name || machine.guest_os.name), fact("Guest VERSION_ID", machine.guest_os.version_id || "Not supplied by guest"));
  const image = config.source_kind === "golden" ? (state.images || []).find(item => item.name === config.source) : null;
  if (image) {
    fields.push(fact("Image version", image.version), fact("Image notes", image.notes || "No notes"));
    if (image.sha512 || image.sha256) fields.push(fact(image.sha512 ? "Source SHA-512" : "Source SHA-256", image.sha512 || image.sha256));
    fields.push(fact("Image applications", image.packages.join(", ") || "Not recorded"));
  }
  const packages = [...new Set([...(config.packages || []), ...(config.bundle_packages || [])])];
  fields.push(node("h4", "Application Selections"), fact("Software bundles", (config.software_bundles || []).map(id => catalog.bundles?.find(bundle => bundle.id === id)?.name || id).join(", ") || "None selected"),
    fact("Requested packages", packages.join(", ") || (["tart", "golden"].includes(config.source_kind) ? "Use applications already in the source image" : "None requested")));
  const raw = node("details", undefined, "vm-configuration"); raw.append(node("summary", "Full Deployment Configuration (JSON)"), node("pre", JSON.stringify(config, null, 2)));
  fields.push(raw);
  return fields;
}
let machineDetailsRequest = 0;
async function machineDetails(machine) {
  machine = state.machines.find(item => item.config.name === machine.config.name) || machine;
  const name = machine.config.name, request = ++machineDetailsRequest;
  const inspection = node("section", undefined, "vm-inspection");
  inspection.append(node("h4", "Live Health & Checkpoints"), node("p", "Checking guest health, mounts, disk allocation, and recovery points…", "muted"));
  const refresh = node("div", undefined, "actions");
  refresh.append(button("Refresh details", () => machineDetails(machine)), button("Save deployment profile", () => saveProfileDialog(machine.config)));
  if (machine.exists && machine.owned && machine.phase === "ready" && !vmJob(name)) refresh.append(button("Manage users", () => manageUsers(machine)));
  dialog(name + " · Details", "Resources and deployment settings are shown below. Live guest checks use the privileged agent, with SSH as a fallback.", [...machineConfiguration(machine), refresh, inspection], null);
  $("action-dialog").classList.add("vm-details");
  const [healthResult, recoveryResult, storageResult] = await Promise.allSettled([
    api("/api/health", {name, details: true}), api("/api/checkpoints", {name}), api("/api/storage")]);
  if (request !== machineDetailsRequest || !$("action-dialog").open || !inspection.isConnected) return;
  inspection.replaceChildren(node("h4", "Live Health"));
  if (healthResult.status === "fulfilled") {
    const health = healthResult.value;
    if (health.guest_os?.name) inspection.append(fact("Detected guest OS", health.guest_os.pretty_name || health.guest_os.name), fact("Guest VERSION_ID", health.guest_os.version_id || "Not supplied by guest"));
    inspection.append(fact("Readiness", {"ssh-ready": "SSH ready", "agent-ready": "Guest-agent management ready", booting: "Booting / waiting for network", running: "Running / SSH unavailable", stopped: "Stopped"}[health.status] || health.status),
      fact("Current IP", health.ip || "Unavailable while stopped or waiting for the network"),
      fact("Guest agent", `${health.agent} · version ${health.agent_version || "not recorded"}`),
      fact("Management connection", health.management_transport === "agent" ? "Privileged guest agent · VSOCK" : health.management_transport === "ssh" ? "SSH fallback" : "Unavailable while stopped or booting"),
      fact("Port forwarding", machine.config.port_forwards?.length ? health.forwarding : "Not configured"), fact("Health checked", date(health.checked_at * 1000)));
    const shortcuts = node("div", undefined, "actions");
    if (health.ip) shortcuts.append(button("Copy SSH command", () => sshShortcut(machine)), button("Open SSH terminal", () => sshShortcut(machine, true)));
    if (machine.exists && machine.owned && machine.phase === "ready") shortcuts.append(sshConfigControl(machine, health.ip, false));
    shortcuts.append(button("Refresh health", () => machineDetails(machine))); inspection.append(shortcuts, node("h4", "Guest Network Interfaces"));
    for (const iface of health.interfaces) inspection.append(fact(`${iface.name} · ${iface.state}`, iface.addresses.join(", ") || "No IPv4 address"));
    if (!health.interfaces.length) inspection.append(node("p", health.status === "stopped" ? "Start the VM to inspect its interfaces." : "Interface details require a working guest agent or SSH key authentication.", "muted"));
    if (health.shares.length) {
      inspection.append(node("h4", "Shared Directory Health"));
      for (const share of health.shares) inspection.append(fact(share.guest_path, `Mac ${share.host_available ? "available" : "missing"} · guest ${share.mounted === null ? "unchecked" : share.mounted ? "mounted" : "not mounted"} · ${share.read_only ? "read-only" : "read-write"}`));
    }
    if (health.issues.length) { inspection.append(node("h4", "Needs Attention")); for (const issue of health.issues) inspection.append(node("p", issue, "banner warning")); }
  } else inspection.append(node("p", "Live health unavailable: " + healthResult.reason.message, "banner warning"));
  if (storageResult.status === "fulfilled") {
    const storage = storageResult.value, disk = storage.items.find(item => item.id === "vm/" + name);
    if (disk) inspection.append(node("h4", "Storage On Mac"), fact("Allocated space", bytes(disk.allocated_bytes)), fact("Logical file size", bytes(disk.logical_bytes)), node("p", storage.note, "hint"));
  } else inspection.append(node("p", "Mac disk allocation unavailable: " + storageResult.reason.message, "muted"));
  inspection.append(node("h4", "Checkpoints"));
  if (recoveryResult.status === "fulfilled") {
    const recovery = recoveryResult.value;
    if (!machine.running && machine.phase === "ready" && !vmJob(name)) inspection.append(button("＋ Create checkpoint", () => checkpointDialog(machine)));
    for (const item of recovery.checkpoints) {
      const row = node("div", undefined, "connection-row"); row.append(node("strong", item.label), node("p", `${date(item.created_at)} · ${item.disk_gb} GB disk`), node("p", item.notes));
      const actions = node("div", undefined, "actions");
      if (!machine.running && !vmJob(name)) actions.append(button("Restore", () => restoreDialog(machine, item)), button("Delete", () => deleteCheckpointDialog(machine, item)));
      row.append(actions); inspection.append(row);
    }
    if (!recovery.checkpoints.length) inspection.append(node("p", "No checkpoints", "muted"));
  } else inspection.append(node("p", "Recovery points unavailable: " + recoveryResult.reason.message, "banner warning"));
}
function saveProfileDialog(config, existing) {
  dialog(existing ? "Edit Deployment Profile" : "Save Deployment Profile", "Reuse these resources, packages, network settings, shares, and SSH public-key paths. Passwords and private keys are excluded. Each new VM gets its own name and hostname.",
    [field("Profile name", "text", existing?.config.name || config.name + "-profile", "profile-name"), multiline("Notes", existing?.notes || "", "profile-notes")],
    async () => { await api("/api/profile/save", {name: $("profile-name").value, config, notes: $("profile-notes").value}); await refresh(); }, "Save profile");
  $("profile-name").readOnly = !!existing;
}
async function editProfile(profile) {
  if (!await openWizard(profile.config.os, profile)) return;
  applyProfile(profile);
  $("vm-name").value = profile.config.name;
  $("image-hint").textContent = "Edit the saved settings for " + profile.config.name + ", then review and save changes.";
}
async function saveEditedProfile() {
  $("next").disabled = true;
  try {
    await api("/api/profile/save", {name: editingProfile.config.name, config: config(), notes: $("edit-profile-notes").value});
    clearBuildCredentials(); $("wizard").close(); await refresh(); toast("Profile updated.");
  } catch (error) { banner("wizard-error", error); }
  finally { $("next").disabled = false; }
}
function exportProfile(profile) {
  const content = JSON.stringify(profile, null, 2) + "\n";
  const preview = multiline("Profile JSON", content, "export-profile-json"); preview.querySelector("textarea").readOnly = true;
  const actions = node("div", undefined, "actions");
  actions.append(button("Copy JSON", async () => { try { await navigator.clipboard.writeText(content); toast("Profile JSON copied."); } catch (error) { toast("Select and copy the JSON below."); } }), button("Download JSON", () => downloadProfile(profile)));
  dialog("Export " + profile.config.name, "Save the JSON file or copy it for import on another workspace. Public-key and share paths may need adjustment on another Mac.", [actions, preview], null);
}
function downloadProfile(profile) {
  const blob = new Blob([JSON.stringify(profile, null, 2) + "\n"], {type: "application/json"});
  const url = URL.createObjectURL(blob); const link = node("a"); link.href = url; link.download = profile.config.name + ".json";
  document.body.append(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
}
function importProfileDialog() {
  const file = field("JSON file (optional)", "file", "", "profile-file");
  file.querySelector("input").accept = ".json,application/json";
  file.querySelector("input").addEventListener("change", async (event) => {
    const source = event.target.files[0]; if (!source) return;
    if (source.size > 65536) { banner("action-error", "Profile JSON must be under 64 KB."); return; }
    $("profile-json").value = await source.text();
  });
  dialog("Import Deployment Profile", "Choose a file or paste an exported AppleTart profile. Review its local paths and network settings before deploying.", [file, multiline("Profile JSON", "", "profile-json")],
    async () => { await api("/api/profile/import", {profile: JSON.parse($("profile-json").value)}); await refresh(); }, "Import profile");
}
function deleteProfileDialog(profile) {
  const name = profile.config.name;
  dialog("Delete Profile " + name + "?", "Delete the saved recipe. Existing VMs are preserved.", [field("Type " + name, "text", "", "profile-confirm")],
    async () => { await api("/api/profile/delete", {name, confirmation: $("profile-confirm").value}); await refresh(); }, "Delete profile", true);
}
function renderProfiles() {
  const list = $("profiles"); const rows = [];
  const select = $("deployment-profile"); const selected = select.value; const options = [];
  const blank = node("option", "Platform defaults"); blank.value = ""; blank.setAttribute("data-live-key", ""); options.push(blank);
  for (const profile of state.profiles || []) {
    const config = profile.config;
    const option = node("option", config.name); option.value = config.name; option.setAttribute("data-live-key", config.name); options.push(option);
    const row = node("article", undefined, "library-row"); row.setAttribute("data-live-key", config.name);
    row.append(osIcon(config.os));
    const content = node("div", undefined, "library-content"); content.setAttribute("data-live-key", "content");
    const disk = config.disk_gb ? ` · ${config.disk_gb} GB disk` : "";
    content.append(node("h3", config.name), node("div", `${platformLabel(config.os)} · ${config.cpu} CPU · ${config.memory_mb / 1024} GB RAM${disk} · ${networkLabel(config)}`, "detail"));
    if (config.source_kind === "golden") content.append(node("div", "Golden image: " + config.source, "detail"));
    if (profile.notes) content.append(node("p", profile.notes, "library-notes"));
    content.append(node("small", "Updated " + date(profile.updated_at)));
    const actions = node("div", undefined, "actions"); actions.setAttribute("data-live-key", "actions");
    actions.append(control("Deploy " + config.name, "start", () => deployProfileDialog(profile)),
      control("Customize " + config.name, "configure", async () => { if (await openWizard(config.os)) applyProfile(profile); }),
      control("Edit profile " + config.name, "configure", () => editProfile(profile)),
      control("Edit notes for " + config.name, "details", () => saveProfileDialog(config, profile)),
      control("Export JSON for " + config.name, "download", () => exportProfile(profile)),
      control("Delete profile " + config.name, "destroy", () => deleteProfileDialog(profile)));
    row.append(content, actions); rows.push(row);
  }
  reconcileChildren(select, options);
  if (select.value !== selected) select.value = selected;
  if (!rows.length) rows.push(node("p", "No deployment profiles", "muted"));
  reconcileChildren(list, rows);
}
function deployProfileDialog(profile) {
  const config = profile.config;
  const credentials = credentialFields(config, "profile");
  let index = 1; const prefix = config.name.slice(0, 58); let name;
  do { name = `${prefix}-${index++}`; } while (state.machines.some(machine => machine.config.name === name) || state.jobs.some(job => active(job) && job.name === name));
  dialog("Deploy " + config.name, "Use this profile’s saved image, resources, network, shared directories, applications and SSH keys. Choose the new VM’s name to begin building and deploying.",
    [field("New VM name", "text", name, "profile-vm-name"), ...credentials.fields],
    () => submit("deploy-profile", {profile: config.name, name: $("profile-vm-name").value.trim(), ...credentials.data()}), "Deploy VM");
}
function applyProfile(profile) {
  const config = profile.config;
  if (!catalog.images[config.os]) catalog.images[config.os] = {label: config.os, family: config.guest_family || "linux", source_kind: config.source_kind, source: config.source, ssh_user: config.ssh_user, icon: "other", description: "Saved deployment profile"};
  chooseOS(config.os); $("password").value = "";
  $("guest-password").value = ""; $("guest-password").dataset.passwordLogin = String(!!config.password_login);
  $("guest-password").placeholder = config.password_login ? "Required for this deployment" : "Optional";
  buildUserEditor?.reset(config.users || []);
  $("build-users-options").open = !!config.users?.length;
  $("deployment-profile").value = config.name; $("vm-name").value = "";
  $("source-kind").value = config.source_kind; $("source").value = config.source; $("source").dataset.checksumSource = config.source; $("source-location").value = config.source.startsWith("https://") ? "remote" : "local"; goldenOptions(); $("golden-source").value = config.source;
  $("checksum-algorithm").value = config.sha512 ? "sha512" : "sha256"; $("sha256").value = config.sha512 || config.sha256 || ""; checksumHint();
  $("cpu").value = config.cpu; $("memory").value = config.memory_mb; $("disk").value = config.disk_gb;
  size = config.size || "small";
  document.querySelector(`input[name="network"][value="${config.network}"]`).checked = true;
  $("bridge").value = config.bridge || ""; $("bridge-label").hidden = config.network === "nat";
  for (const id of ["extra-bridges", "port-forwards", "directory-shares"]) $(id).replaceChildren();
  for (const iface of (config.bridges || []).slice(1)) addBridge($("extra-bridges"), iface);
  for (const rule of config.port_forwards || []) addForward($("port-forwards"), rule);
  for (const share of config.directory_shares || []) addShare($("directory-shares"), share);
  $("forward-options").open = !!config.port_forwards?.length; $("share-options").open = !!config.directory_shares?.length;
  $("ssh-user").value = config.ssh_user; $("public-keys").value = config.ssh_public_keys.join("\n"); $("use-keys").setAttribute("aria-checked", String(!!config.ssh_public_keys.length));
  $("hostname").value = ""; renderBundleChoices(config.software_bundles || (config.install_default_packages ? ["kali-essentials"] : [])); $("refresh-source").checked = !!config.refresh_source; $("platform-version").value = config.source; $("packages").value = (config.packages || []).join(", "); syncSource();
  $("image-hint").textContent = "Profile: " + config.name + ". Choose a new machine name and review the saved settings.";
}
function editImageMetadata(image) {
  dialog("Golden Image Details", "Record the version and purpose of this image. Creating an updated disk produces a new golden image.",
    [field("Version", "text", image.version, "golden-edit-version"), multiline("Notes", image.notes, "image-notes"), fact("Built", date(image.created_at)), fact("Source", image.source), fact("Guest agent", image.agent_version), fact("Requested applications", image.packages.join(", ") || "Inherited / not recorded")],
    async () => { await api("/api/image/update", {name: image.name, version: $("golden-edit-version").value, notes: $("image-notes").value}); await refresh(); }, "Save details");
}
function deleteImageDialog(image) {
  const references = image.references || [];
  dialog("Delete " + image.name + "?", references.length ? "This image is still referenced. Remove the listed VM or profile references before deleting it." : "Permanently delete this golden image and its prepared disk. Type the image name to confirm.",
    references.length ? [node("pre", references.join("\n"))] : [field("Type " + image.name, "text", "", "image-confirm")],
    references.length ? async () => {} : () => submit("delete-image", {name: image.name, confirmation: $("image-confirm").value}), references.length ? "Close" : "Delete image", !references.length);
}
async function loadStorage() {
  if (storageLoading) return; storageLoading = true;
  try {
    const storage = await api("/api/storage");
    $("storage-summary").textContent = `${bytes(storage.free_bytes)} free on the data volume · ${bytes(storage.allocated_bytes)} allocated across listed items`;
    $("storage-note").textContent = storage.note;
    const list = $("storage-items"); list.replaceChildren();
    for (const item of storage.items) {
      const row = node("tr"); const selection = node("td"); const checkbox = node("input"); checkbox.type = "checkbox"; checkbox.value = item.id; checkbox.disabled = item.protected; checkbox.setAttribute("aria-label", "Select " + item.name); selection.append(checkbox);
      const title = node("td", item.name.length > 70 ? item.name.slice(0, 24) + "…" + item.name.slice(-16) : item.name); title.title = item.id;
      row.append(selection, title, node("td", item.kind), node("td", bytes(item.allocated_bytes)), node("td", item.protected ? item.reason : "Unused · eligible for cleanup")); list.append(row);
    }
    if (!storage.items.length) $("storage-summary").textContent += " · No storage items";
    $("purge-unused-storage").disabled = !unusedStorageCaches(storage.items).length;
    banner("storage-error", null);
  } catch (error) { banner("storage-error", error); }
  finally { storageLoading = false; }
}
function cleanupStorageDialog() {
  const items = Array.from($("storage-items").querySelectorAll("input:checked"), (input) => input.value);
  if (!items.length) { toast("Select unused storage items first."); return; }
  dialog("Clean Up Storage", "Permanently delete these unused items. Referenced images, VM disks, and checkpoints are protected.",
    [node("pre", items.join("\n")), field("Type DELETE", "text", "", "storage-confirm")],
    async () => { await submit("cleanup", {items, confirmation: $("storage-confirm").value}); }, "Delete selected items", true);
}
function checksumHint() {
  const algorithm = $("checksum-algorithm").value;
  $("sha256").placeholder = `Optional · ${algorithm === "sha512" ? 128 : 64}-character ${algorithm.toUpperCase()} hash`;
  $("sha256").maxLength = algorithm === "sha512" ? 128 : 64;
}
function unusedStorageCaches(items) {
  return items.filter(item => !item.protected && ["downloads", "converted", "tools"].includes(item.kind));
}
async function purgeUnusedStorageDialog() {
  try {
    const unused = unusedStorageCaches((await api("/api/storage")).items);
    if (!unused.length) { toast("No unused caches to purge."); await loadStorage(); return; }
    const size = unused.reduce((total, item) => total + item.allocated_bytes, 0);
    dialog("Purge Unused Storage", `Delete ${unused.length} unused cached ${unused.length === 1 ? "file" : "files"} (${bytes(size)} allocated). Downloads, converted disks and tool archives can be recreated. Golden images, VM disks and checkpoints are kept.`,
      [node("pre", unused.map(item => item.id).join("\n"))],
      () => submit("cleanup", {items: unused.map(item => item.id), confirmation: "DELETE", cache_only: true}), "Purge unused", true);
  } catch (error) { banner("storage-error", error); }
}
function showView(view) {
  currentView = view;
  for (const id of ["vms", "library", "settings"]) { $(id === "vms" ? "vm-view" : id + "-view").hidden = id !== view; $("nav-" + id).classList.toggle("active", id === view); }
  $("section-title").textContent = {vms: "Virtual Machines", library: "Images & Profiles", settings: "Settings"}[view];
  $("page-title").textContent = $("section-title").textContent;
  if (view === "settings") { loadSettings(); loadStorage(); loadSoftwareSettings(); }
}

async function loadSettings() {
  try {
    const settings = await api("/api/settings"); const select = $("ssh-terminal"); select.replaceChildren();
    for (const terminal of settings.terminals) {
      const option = node("option", terminal.label + (terminal.installed ? "" : " · not installed"));
      option.value = terminal.id; option.disabled = !terminal.installed; select.append(option);
    }
    select.value = settings.terminal; $("default-public-key").value = settings.default_public_key || ""; $("default-install-ssh-key").setAttribute("aria-checked", String(settings.install_ssh_key_by_default !== false)); banner("settings-error", null);
  } catch (error) { banner("settings-error", error); }
}
async function saveSettings(event) {
  event.preventDefault(); $("save-settings").disabled = true;
  try { const result = await api("/api/settings", {terminal: $("ssh-terminal").value, default_public_key: $("default-public-key").value.trim(), install_ssh_key_by_default: $("default-install-ssh-key").getAttribute("aria-checked") === "true"}); catalog.default_public_key = result.default_public_key; catalog.install_ssh_key_by_default = result.install_ssh_key_by_default; banner("settings-error", null); toast("SSH preferences saved."); }
  catch (error) { banner("settings-error", error); }
  finally { $("save-settings").disabled = false; }
}
