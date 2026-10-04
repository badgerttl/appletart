let logViewer = null;

function logSourceOptions(viewer, result) {
  const options = result.sources.map((source) => {
    const option = node("option", source.source === "runtime" ? "Tart / serial console · all boots" : `${source.action} · ${source.status} · ${date(source.started_at)}`);
    option.value = source.source; return option;
  });
  reconcileChildren(viewer.select, options);
  viewer.select.value = result.source || "";
  viewer.selected = result.sources.find((item) => item.source === result.source);
}
function logRange(viewer) {
  setLiveText(viewer.range, `${bytes(viewer.start)}–${bytes(viewer.end)} of ${bytes(viewer.size)} · Copy and download include the full selected log.`);
  viewer.earlier.disabled = !viewer.start || viewer.pre.textContent.length >= 1024 * 1024;
  viewer.copy.disabled = viewer.download.disabled = !viewer.source;
}
function appendSavedLog(viewer, text) {
  if (!text) return;
  const pre = viewer.pre, following = pre.scrollHeight - pre.clientHeight - pre.scrollTop <= 4;
  let top = pre.scrollTop;
  if (pre.firstChild?.nodeType === 3) pre.firstChild.appendData(text);
  else pre.textContent = text;
  if (pre.textContent.length > 1024 * 1024) {
    const start = pre.textContent.indexOf("\n", pre.textContent.length - 1024 * 1024);
    const removed = pre.textContent.slice(0, start < 0 ? pre.textContent.length - 1024 * 1024 : start + 1);
    const height = pre.scrollHeight;
    pre.textContent = pre.textContent.slice(removed.length);
    viewer.start += new TextEncoder().encode(removed).length;
    top = Math.max(0, top - (height - pre.scrollHeight));
  }
  pre.scrollTop = following ? pre.scrollHeight : top;
}
async function loadSavedLog(viewer, mode = "latest") {
  if (viewer.loading || logViewer !== viewer || !$("action-dialog").open) return;
  viewer.loading = true;
  const generation = viewer.generation;
  try {
    const result = await api("/api/log", {name: viewer.name, source: viewer.source,
      ...(mode === "earlier" ? {before: viewer.start} : mode === "append" ? {after: viewer.end} : {})});
    if (logViewer !== viewer || !$("action-dialog").open || generation !== viewer.generation) return;
    if (mode === "earlier") {
      viewer.live.checked = false;
      const height = viewer.pre.scrollHeight, top = viewer.pre.scrollTop;
      viewer.pre.textContent = result.log + viewer.pre.textContent;
      viewer.pre.scrollTop = top + viewer.pre.scrollHeight - height;
      viewer.start = result.start;
    } else if (mode === "append" && result.size_bytes >= viewer.end) {
      appendSavedLog(viewer, result.log); viewer.end = result.end;
    } else {
      viewer.pre.textContent = result.log; viewer.pre.scrollTop = viewer.pre.scrollHeight;
      viewer.start = result.start; viewer.end = result.end;
    }
    viewer.source = result.source; viewer.size = result.size_bytes;
    logSourceOptions(viewer, result); logRange(viewer); banner("action-error", null);
  } catch (error) { if (logViewer === viewer) banner("action-error", error); }
  finally { viewer.loading = false; if (logViewer === viewer && generation !== viewer.generation) loadSavedLog(viewer); }
}
function refreshLogViewer() {
  const viewer = logViewer;
  if (viewer?.live.checked && viewer.source && $("action-dialog").open) loadSavedLog(viewer, "append");
}
async function savedLogFile(viewer) {
  const response = await fetch("/api/log/download", {method: "POST", headers: {"X-Appletart-Token": token, "Content-Type": "application/json"}, body: JSON.stringify({name: viewer.name, source: viewer.source})});
  if (!response.ok) throw new Error((await response.json()).error || "Could not download this log.");
  return response.blob();
}
async function copySavedLog(viewer) {
  try {
    const blob = await savedLogFile(viewer);
    await navigator.clipboard.writeText(await blob.text());
    viewer.copy.classList.add("copied"); viewer.copy.title = "Copied"; viewer.copy.setAttribute("aria-label", "Copied full log");
    setTimeout(() => { viewer.copy.classList.remove("copied"); viewer.copy.title = "Copy full log"; viewer.copy.setAttribute("aria-label", "Copy full log"); }, 1800);
  } catch (error) { if (logViewer === viewer) banner("action-error", error); }
}
async function downloadSavedLog(viewer) {
  try {
    const blob = await savedLogFile(viewer), url = URL.createObjectURL(blob), link = node("a");
    link.href = url; link.download = `${viewer.name}-${viewer.selected?.action || "runtime"}-${viewer.source}.log`;
    document.body.append(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  } catch (error) { if (logViewer === viewer) banner("action-error", error); }
}
async function openMachineLogs(name, source) {
  const viewer = {name, source, start: 0, end: 0, size: 0, loading: false, generation: 0};
  const label = node("label", "Saved operation or console log");
  viewer.select = node("select"); viewer.select.setAttribute("aria-label", "Saved log"); label.append(viewer.select);
  viewer.select.onchange = () => { viewer.source = viewer.select.value; viewer.generation++; loadSavedLog(viewer); };
  const controls = node("div", undefined, "log-toolbar");
  viewer.earlier = button("Load earlier", () => loadSavedLog(viewer, "earlier"));
  viewer.earlier.disabled = true;
  const latest = button("Latest output", () => loadSavedLog(viewer));
  const collect = button("Collect guest logs", async () => {
    try {
      const job = await api("/api/action", {action:"diagnostics", name});
      viewer.source = job.id; viewer.generation++; await loadSavedLog(viewer); await refresh();
    } catch (error) { if (logViewer === viewer) banner("action-error", error); }
  });
  const machine = state.machines.find((item) => item.config.name === name);
  collect.disabled = !machine?.managed || !machine.running || !!vmJob(name);
  collect.title = collect.disabled ? "Start the VM to collect current guest logs." : "Capture guest setup, package-manager and service logs through the privileged agent, with SSH as a fallback.";
  const liveLabel = node("label", undefined, "check"); viewer.live = node("input"); viewer.live.type = "checkbox"; viewer.live.checked = true;
  viewer.live.onchange = () => { if (viewer.live.checked) loadSavedLog(viewer, "append"); };
  liveLabel.append(viewer.live, "Live updates");
  viewer.copy = control("Copy full log", "copy", () => copySavedLog(viewer));
  viewer.download = control("Download full log", "download", () => downloadSavedLog(viewer));
  controls.append(viewer.earlier, latest, collect, liveLabel, viewer.copy, viewer.download);
  viewer.range = node("p", "Loading saved logs…", "hint");
  viewer.pre = node("pre", "Loading…", "diagnostic-log"); viewer.pre.setAttribute("tabindex", "0"); viewer.pre.setAttribute("aria-label", `${name} detailed log`);
  dialog(name + " · Logs", "Build attempts and VM operations are saved separately. Select an attempt to inspect its setup, command results and captured guest diagnostics.", [label, controls, viewer.range, viewer.pre], null);
  $("action-dialog").classList.add("vm-logs"); logViewer = viewer;
  await loadSavedLog(viewer);
}
