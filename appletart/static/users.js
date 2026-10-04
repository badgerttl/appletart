"use strict";

// Shared account editor for provisioning and management of existing guests.
function userEditor(container, initial = []) {
  const rows = node("div", undefined, "user-entries");
  const error = node("p", "", "banner error"); error.hidden = true; error.setAttribute("role", "alert");
  const toolbar = node("div", undefined, "users-toolbar");
  const file = node("input"); file.type = "file"; file.accept = ".yaml,.yml,application/yaml,text/yaml"; file.hidden = true;
  let revision = 0;
  const changed = () => { revision++; error.hidden = true; container.dispatchEvent(new Event("accountschange")); };
  function add(user = {}) {
    const entry = node("div", undefined, "user-entry");
    const username = field("Username", "text", user.username || "", "");
    const name = username.querySelector("input"); name.dataset.user = "username"; name.maxLength = 32; name.placeholder = "developer"; name.autocomplete = "off";
    const secret = field("Password", "password", user.password || "", "");
    const password = secret.querySelector("input"); password.dataset.user = "password"; password.maxLength = 1024; password.autocomplete = "new-password"; password.placeholder = user.password_login ? "Required for this deployment" : "Optional";
    entry.dataset.passwordLogin = String(!!user.password_login);
    const keys = multiline("SSH Public Keys", (user.ssh_authorized_keys || []).join("\n"), "");
    const keyInput = keys.querySelector("textarea"); keyInput.dataset.user = "keys"; keyInput.placeholder = "Optional with a password · one public key per line"; keyInput.rows = 2;
    const heading = node("div", undefined, "users-entry-heading"); heading.append(node("strong", "User Account"), control("Remove User", "cancel", () => { entry.remove(); changed(); }));
    const identity = node("div", undefined, "row"); identity.append(username, secret);
    entry.append(heading, identity, keys); entry.addEventListener("input", event => { if (event.target === password) entry.dataset.passwordLogin = "false"; changed(); }); rows.append(entry); changed();
  }
  const importButton = button("Import YAML", () => file.click());
  file.addEventListener("change", async () => {
    const selected = file.files[0]; if (!selected) return;
    const request = ++revision; importButton.disabled = true; error.hidden = true;
    try {
      if (selected.size > 48 * 1024) throw new Error("User YAML must be under 48 KB.");
      const yaml = await selected.text();
      if (request !== revision || !container.isConnected) return;
      const result = await api("/api/users/preview", {yaml});
      if (request !== revision || !container.isConnected) return;
      rows.replaceChildren(); for (const user of result.users) add(user);
    } catch (failure) { if (request === revision && container.isConnected) { error.textContent = failure.message; error.hidden = false; } }
    finally { importButton.disabled = false; file.value = ""; }
  });
  const template = node("a", "Download YAML Template"); template.href = "/templates/users.yaml"; template.download = "appletart-users.yaml";
  toolbar.append(button("Add User", () => add()), importButton, template, file);
  container.append(rows, toolbar, error);
  function values() {
    return Array.from(rows.children, entry => {
      const values = Object.fromEntries(Array.from(entry.querySelectorAll("[data-user]"), input => [input.dataset.user, input.value]));
      return {username: values.username.trim(), ssh_authorized_keys: values.keys.split("\n").map(key => key.trim()).filter(Boolean),
        ...(values.password || entry.dataset.passwordLogin === "true" ? {password_login: true, password: values.password} : {})};
    });
  }
  const editor = {
    values,
    recipes: () => values().map(({password, ...recipe}) => recipe),
    passwords: () => Object.fromEntries(values().filter(user => user.password_login).map(user => [user.username, user.password])),
    reset: (users = []) => { rows.replaceChildren(); revision++; file.value = ""; error.hidden = true; for (const user of users) add(user); changed(); },
    clear: () => { editor.reset(); },
    count: () => rows.children.length,
  };
  editor.reset(initial); return editor;
}

let buildUserEditor;
function initializeBuildUsers() {
  buildUserEditor = userEditor($("build-users"));
  $("build-users").addEventListener("accountschange", () => { $("build-users-count").textContent = String(buildUserEditor.count()); });
}
function buildCredentials() {
  return {guest_password: $("guest-password").value, user_passwords: buildUserEditor?.passwords() || {}};
}
function clearBuildCredentials() {
  $("password").value = ""; $("guest-password").value = ""; $("guest-password").dataset.passwordLogin = "false"; $("guest-password").placeholder = "Optional"; buildUserEditor?.clear();
}
function credentialFields(config, prefix = "deploy") {
  const fields = [], passwords = new Map();
  if (config.password_login) {
    const label = field("Guest Password · " + config.ssh_user, "password", "", prefix + "-guest-password");
    const input = label.querySelector("input"); input.autocomplete = "new-password"; fields.push(label); passwords.set(config.ssh_user, input);
  }
  for (const user of config.users || []) if (user.password_login) {
    const label = field("Password · " + user.username, "password", "", "");
    const input = label.querySelector("input"); input.autocomplete = "new-password"; fields.push(label); passwords.set(user.username, input);
  }
  return {fields, data: () => ({guest_password: config.password_login ? passwords.get(config.ssh_user).value : "",
    user_passwords: Object.fromEntries([...passwords].filter(([name]) => name !== config.ssh_user).map(([name, input]) => [name, input.value]))})};
}
function manageUsers(machine) {
  const name = machine.config.name;
  const form = node("section", undefined, "users-form"); const editor = userEditor(form);
  const review = node("section", undefined, "users-review"); review.hidden = true;
  const error = node("p", "", "banner error"); error.hidden = true; error.setAttribute("role", "alert");
  let revision = 0, applying = false;
  form.addEventListener("accountschange", () => { revision++; review.hidden = true; review.replaceChildren(); error.hidden = true; });
  const validate = button("Review Users", async () => {
    const request = ++revision; validate.disabled = true; error.hidden = true;
    try {
      const result = await api("/api/users/preview", {users: editor.values()});
      if (request !== revision || !review.isConnected || !$("action-dialog").open) return;
      review.replaceChildren(node("h3", "Review User Access"));
      for (const user of result.summary) review.append(fact(user.username, `${user.key_count} public key(s)${user.password_login ? " · Password Login" : ""}`));
      review.append(node("p", "Every listed user receives passwordless sudo for all commands.", "banner warning"));
      const apply = button("Add Users", async () => {
        if (applying) return; applying = true; apply.disabled = true;
        try {
          if (!state.machines.some(vm => vm.config.name === name && vm.running) || vmJob(name)) throw new Error("Start the VM and wait for its current operation to finish before adding users.");
          await submit("users", {name, users: result.users}); editor.clear();
          if (review.isConnected) $("action-dialog").close();
        } catch (failure) { if (review.isConnected) { error.textContent = failure.message; error.hidden = false; } }
        finally { applying = false; apply.disabled = false; }
      }, true);
      apply.disabled = !machine.running || !!vmJob(name); review.append(apply); review.hidden = false;
    } catch (failure) { if (request === revision && review.isConnected) { error.textContent = failure.message; error.hidden = false; } }
    finally { validate.disabled = false; }
  }, true);
  dialog("Users · " + name, "Add accounts with SSH public keys, passwords, or both. Uses privileged guest-agent management when available.", [form, validate, error, review], null);
  $("action-dialog").classList.add("vm-users");
  $("action-dialog").addEventListener("close", () => { editor.clear(); review.replaceChildren(); }, {once: true});
}
