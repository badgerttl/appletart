"use strict";

function platformEntries() {
  return Object.entries(catalog.images).sort(([leftId, left], [rightId, right]) => {
    if (leftId === "other" || rightId === "other") return Number(leftId === "other") - Number(rightId === "other");
    return left.label.localeCompare(right.label, "en", {sensitivity: "base"});
  });
}
function renderPlatforms() {
  $("os-options").replaceChildren(); $("images").replaceChildren();
  for (const [id, image] of platformEntries()) {
    const tile = node("button", undefined, "tile"); tile.type = "button"; tile.dataset.os = id;
    tile.classList.toggle("selected", id === guest);
    const sourceLabel = {tart: "Tart template", cloud: "Cloud image", iso: "Installer ISO"}[image.source_kind] || "Custom image";
    tile.append(osIcon(id), node("strong", image.label), node("small", sourceLabel));
    tile.addEventListener("click", () => chooseOS(id)); $("os-options").append(tile);
    const row = node("article", undefined, "library-row");
    const content = node("div", undefined, "library-content");
    content.append(node("h3", image.label), node("div", sourceLabel, "detail"));
    if (image.versions?.length) content.append(node("small", `${image.versions.length} ${image.versions.length === 1 ? "version / variant" : "versions / variants"}`));
    const actions = node("div", undefined, "actions"); actions.append(control("Configure " + image.label, "start", () => openWizard(id)));
    row.append(osIcon(id), content, actions); $("images").append(row);
  }
}
function populateVersions() {
  const select = $("platform-version"); select.replaceChildren(node("option", "Custom image source")); select.firstChild.value = "";
  const image = catalog.images[guest], groups = new Map();
  for (const version of image.versions || []) {
    const option = node("option", version.label); option.value = version.source;
    const release = version.label.includes(" · ") ? version.label.split(" · ")[0] : "";
    if (release) {
      if (!groups.has(release)) { const group = node("optgroup"); group.label = release; groups.set(release, group); select.append(group); }
      groups.get(release).append(option);
    } else select.append(option);
  }
  select.value = image.versions?.some(version => version.source === image.source) ? image.source : "";
  $("platform-version-label").hidden = !image.versions?.length;
}
function minimumImageDisk() {
  const image = catalog.images[guest] || {};
  const version = image.versions?.find(item => item.source === $("source").value);
  return version?.minimum_disk_gb || ($("source").value === image.source ? image.minimum_disk_gb : 1) || 1;
}
function chooseVersion() {
  const image = catalog.images[guest]; const version = image.versions?.find(item => item.source === $("platform-version").value);
  if (!version) return;
  $("source-kind").value = version.source_kind || image.source_kind; $("source").value = version.source;
  const hashes = version.sha512 || version.sha256 ? version : version.source === image.source ? image : {};
  $("source").dataset.checksumSource = version.source; $("sha256").value = hashes.sha512 || hashes.sha256 || "";
  $("checksum-algorithm").value = hashes.sha512 ? "sha512" : "sha256"; checksumHint();
  $("source-location").value = ["cloud", "iso"].includes($("source-kind").value) && !version.source.includes("://") ? "local" : "remote";
  $("password").value = $("source-kind").value === "tart" ? image.bootstrap_password || "" : ""; $("disk").value = Math.max(Number($("disk").value), minimumImageDisk()); syncSource();
}
function selectedBundles() { return Array.from($("bundle-options").querySelectorAll("button")).filter(toggle => toggle.getAttribute("aria-checked") === "true").map(toggle => toggle.value); }
function renderBundleChoices(selected = []) {
  const list = $("bundle-options"); list.replaceChildren();
  const family = catalog.images[guest]?.family || "linux";
  const available = (catalog.bundles || []).filter(bundle => bundle.platforms.some(platform => [guest, family, "all"].includes(platform)));
  for (const bundle of available) {
    const choice = node("div", undefined, "bundle-choice");
    const toggle = switchControl("Install " + bundle.name, selected.includes(bundle.id), (on, control) => {
      control.setAttribute("aria-checked", String(on)); updateSoftwareWarning();
    }); toggle.value = bundle.id;
    const content = node("span"); content.append(node("strong", bundle.name), node("small", `${bundle.packages.length} ${bundle.packages.length === 1 ? "package" : "packages"} · ${bundle.packages.join(", ") || "Empty bundle"}`)); choice.append(content, toggle); list.append(choice);
  }
  for (const id of selected.filter(id => !available.some(bundle => bundle.id === id))) {
    list.append(node("p", `Bundle ${id} is missing or incompatible. Choose another bundle before saving this profile.`, "banner warning"));
  }
  if (!available.length) list.append(node("p", "No compatible software bundles", "muted"));
  updateSoftwareWarning();
}
function updateSoftwareWarning() {
  const initialLinux = catalog.images[guest]?.family !== "macos" && $("source-kind").value !== "golden";
  const hasPackages = Boolean(selectedBundles().length || $("packages").value.trim());
  const warning = $("package-build-warning"); warning.textContent = initialLinux ? catalog.linux_build_notice : catalog.software_notice;
  warning.hidden = !initialLinux && !hasPackages;
  $("ssh-fields").hidden = false;
  $("public-key-fields").hidden = !sshKeysEnabled();
}
async function loadSoftwareSettings() {
  try {
    catalog.bundles = (await api("/api/bundles")).bundles;
    renderSoftwareBundles(); banner("bundles-error", null);
  } catch (error) { banner("bundles-error", error); }
}
function renderSoftwareBundles() {
  const rows = [];
  for (const bundle of catalog.bundles || []) {
    const row = node("article", undefined, "library-row"); row.setAttribute("data-live-key", bundle.id);
    const content = node("div", undefined, "library-content");
    content.append(node("h3", bundle.name), node("div", `${bundle.packages.length} packages · ${bundle.platforms.map(id => ({all: "All platforms", linux: "Linux", macos: "macOS"}[id] || platformLabel(id))).join(", ")}`, "detail"), node("small", bundle.packages.join(", ") || "No packages"));
    const actions = node("div", undefined, "actions"); actions.append(control("Edit " + bundle.name, "configure", () => bundleDialog(bundle)), control("Delete " + bundle.name, "destroy", () => deleteBundleDialog(bundle)));
    row.append(osIcon(bundle.platforms.find(id => catalog.images[id]) || "other"), content, actions); rows.push(row);
  }
  if (!rows.length) rows.push(node("p", "No software bundles", "muted"));
  reconcileChildren($("software-bundles"), rows);
}
function bundleDialog(bundle = {}) {
  const platforms = node("div", undefined, "bundle-platforms");
  for (const [id, label] of [["linux", "All Linux platforms"], ["macos", "macOS (Homebrew)"], ...platformEntries().filter(([id]) => id !== "macos").map(([id, image]) => [id, image.label])]) {
    const choice = node("label", label, "check"); const input = node("input"); input.type = "checkbox"; input.value = id;
    input.checked = (bundle.platforms || ["linux"]).some(value => value === id || value === "all"); choice.prepend(input); platforms.append(choice);
  }
  const help = node("details"); help.append(node("summary", "Compatible platforms"), platforms); help.open = true;
  dialog(bundle.id ? "Edit Software Bundle" : "New Software Bundle", "Choose a name, compatible platforms and native package names. Bundles are optional when provisioning; editing one affects future deployments.",
    [field("Bundle name", "text", bundle.name || "", "bundle-name"), help, multiline("Packages · commas or whitespace", (bundle.packages || []).join("\n"), "bundle-packages")], async () => {
      await api("/api/bundle/save", {bundle: {...(bundle.id ? {id: bundle.id} : {}), name: $("bundle-name").value, platforms: Array.from(platforms.querySelectorAll("input:checked"), input => input.value), packages: $("bundle-packages").value.split(/[\s,]+/).filter(Boolean)}});
      await loadSoftwareSettings(); toast("Software bundle saved.");
    }, "Save bundle");
}
function deleteBundleDialog(bundle) {
  dialog("Delete " + bundle.name + "?", "Remove this bundle from future selections. Existing VMs retain their recorded packages. Profiles selecting this bundle must be updated before another deployment.",
    [field("Type " + bundle.name, "text", "", "bundle-confirm")], async () => {
      if ($("bundle-confirm").value !== bundle.name) throw new Error("The name must match exactly.");
      await api("/api/bundle/delete", {id: bundle.id}); await loadSoftwareSettings();
    }, "Delete bundle", true);
}
async function catalogDialog() {
  try {
    const images = (await api("/api/catalog")).images;
    const input = multiline("Platform definitions (JSON)", JSON.stringify(images, null, 2), "catalog-json"); input.classList.add("catalog-editor");
    dialog("Edit Image Catalog", "Platform definitions describe the guest family, image source, available versions, icon and template login. Provisioning detects guest tools independently of these platform labels.", [input], async () => {
      let value; try { value = JSON.parse($("catalog-json").value); } catch (_) { throw new Error("Enter valid catalog JSON."); }
      await api("/api/catalog", {images: value}); catalog = await api("/api/choices"); renderPlatforms(); renderSoftwareBundles(); await refresh(); toast("Image catalog saved.");
    }, "Save catalog");
    $("action-dialog").classList.add("catalog-dialog");
  } catch (error) { banner("bundles-error", error); }
}
function withImageVersion(images, platform, version) {
  const image = images[platform];
  if (!image) throw new Error("This platform is no longer in the image catalog.");
  if (!version.label || !version.source) throw new Error("Enter a version name and image source.");
  if ((image.versions || []).some(item => item.source === version.source || item.label.toLowerCase() === version.label.toLowerCase())) throw new Error("That version name or image source is already in this platform's catalog. Use Edit catalog to change it.");
  const updated = {...image, versions: [...(image.versions || []), version]};
  if (!image.source) {
    updated.source = version.source; updated.source_kind = version.source_kind;
    updated.sha256 = version.sha256 || ""; updated.sha512 = version.sha512 || "";
  }
  return {...images, [platform]: updated};
}
function imageVersionDialog(platform = guest) {
  const platformSelect = node("select"); platformSelect.id = "version-platform";
  for (const [id, image] of platformEntries()) { const option = node("option", image.label); option.value = id; platformSelect.append(option); }
  platformSelect.value = catalog.images[platform] ? platform : Object.keys(catalog.images)[0];
  const kind = node("select"); kind.id = "version-kind";
  const algorithm = node("select"); algorithm.id = "version-hash-algorithm";
  for (const [value, label] of [["sha256", "SHA-256"], ["sha512", "SHA-512"]]) { const option = node("option", label); option.value = value; algorithm.append(option); }
  algorithm.value = "sha256";
  const label = (text, input) => { const wrapper = node("label", text); wrapper.append(input); return wrapper; };
  const updateKinds = () => {
    const image = catalog.images[platformSelect.value]; kind.replaceChildren();
    for (const [value, text] of [["tart", "Tart template"], ["cloud", "ARM64 cloud image"], ["iso", "ARM64 installer ISO"]]) {
      if (image.family === "macos" && value !== "tart") continue;
      const option = node("option", text); option.value = value; kind.append(option);
    }
    kind.value = image.source_kind;
  };
  updateKinds(); platformSelect.addEventListener("change", updateKinds);
  dialog("Add Image Version", "Save a named image source in this workspace's version picker. The current default stays selected for new VMs. Add an ARM64 image and, optionally, its publisher's checksum.",
    [label("Platform", platformSelect), field("Version name", "text", "", "version-name"), label("Image type", kind),
     field("Image source · HTTPS URL, local path or Tart reference", "text", "", "version-source"), label("Checksum algorithm", algorithm),
     field("Publisher checksum · optional", "text", "", "version-checksum")], async () => {
      const platform = platformSelect.value;
      const version = {label: $("version-name").value.trim(), source: $("version-source").value.trim(), source_kind: kind.value};
      if ($("version-checksum").value.trim()) version[algorithm.value] = $("version-checksum").value.trim();
      const images = (await api("/api/catalog")).images;
      await api("/api/catalog", {images: withImageVersion(images, platform, version)});
      catalog = await api("/api/choices"); renderPlatforms(); renderSoftwareBundles();
      if ($("wizard").open && guest === platform) {
        const source = $("source").value; populateVersions();
        $("platform-version").value = catalog.images[guest].versions.some(item => item.source === source) ? source : "";
        syncSource();
      }
      toast("Image version saved. Select it in the version picker.");
    }, "Save version");
}
function initializeSoftwareSettings() {
  $("platform-version").addEventListener("change", chooseVersion);
  $("new-bundle").addEventListener("click", () => bundleDialog());
  $("edit-catalog").addEventListener("click", catalogDialog);
  $("add-catalog-version").addEventListener("click", () => imageVersionDialog());
  $("add-platform-version").addEventListener("click", () => imageVersionDialog(guest));
}
