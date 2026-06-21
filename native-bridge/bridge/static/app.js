const $ = (id) => document.getElementById(id);
const FIELDS = ["region","email","password","camera_dsn","uid","authkey","av_account",
  "av_password","iotype_start","av_channel","license_key","region_code"];

/* ---------- feedback helpers ---------- */
function toast(msg, kind="") {
  const t = document.createElement("div");
  t.className = "toast " + kind;
  t.textContent = msg;
  $("toasts").appendChild(t);
  setTimeout(() => { t.style.opacity = "0"; setTimeout(() => t.remove(), 300); }, 3200);
}
async function withLoading(btn, fn) {
  if (!btn) return fn();
  btn.classList.add("loading"); btn.disabled = true;
  try { return await fn(); }
  finally { btn.classList.remove("loading"); btn.disabled = false; }
}
function flash(msg){ const l=$("log"); l.textContent += `\n>>> ${msg}\n`; l.scrollTop=l.scrollHeight; }

/* ---------- config ---------- */
function collect() {
  const o = {};
  for (const f of FIELDS) { const el = $(f); if (el) o[f] = el.value; }
  return o;
}
async function loadConfig() {
  const cfg = await (await fetch("/api/config")).json();
  for (const f of FIELDS) { const el = $(f); if (el && cfg[f] !== undefined) el.value = cfg[f]; }
  if (cfg.password === "********") $("password").placeholder = "(saved — leave to keep)";
}
async function save(btn) {
  return withLoading(btn, async () => {
    const body = collect();
    if (body.password === "********") delete body.password;
    const r = await fetch("/api/config", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)});
    if (r.ok) toast("Settings saved.", "good");
    else toast("Couldn't save — config folder not writable (see log).", "bad");
    refreshStatus();
  });
}
async function saveAndRestart(btn) {
  return withLoading(btn, async () => {
    await save();
    await fetch("/api/stream/restart", {method:"POST"});
    toast("Saved · stream restarting…", "good");
    setTimeout(refreshStatus, 1500);
  });
}

/* ---------- diagnose ---------- */
async function diagnose(btn) {
  return withLoading(btn, async () => {
    const body = {region:$("region").value, email:$("email").value,
      password:$("password").value, camera_dsn:$("camera_dsn").value};
    if (!body.email || !body.password) { toast("Enter your Owlet email + password first.", "warn"); return; }
    if (!body.camera_dsn) toast("Tip: add your Camera DSN (OCD…) to auto-fetch the camera key.", "warn");
    await fetch("/api/diagnose", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)});
    toast("Running the real Owlet login… watch the log.", "");
    [1500,4000,8000].forEach(t => setTimeout(loadConfig, t));
    pollFindings();
    setTimeout(refreshStatus, 4000);
  });
}
function pollFindings(){ let n=0; const t=setInterval(async()=>{ await refreshFindings(); if(++n>30) clearInterval(t); }, 1500); }
async function refreshFindings() {
  const data = await (await fetch("/api/findings")).json();
  const box = $("findings");
  if (!data.candidates || !data.candidates.length) {
    box.textContent = data.devices === 0
      ? "Login OK but 0 Ayla devices on this account."
      : "Run a diagnostic to populate candidates.";
    return;
  }
  box.innerHTML = "";
  for (const c of data.candidates) {
    const d = document.createElement("div"); d.className = "cand";
    d.innerHTML = `<span class="f">${c.field}</span><span class="v">${c.value}</span>`;
    d.title = "click to use";
    d.onclick = () => {
      if (/auth/i.test(c.field)) $("authkey").value = c.value; else $("uid").value = c.value;
      toast("Filled from candidate.", "good");
    };
    box.appendChild(d);
  }
}

/* ---------- libraries ---------- */
async function extractLibs(btn) {
  return withLoading(btn, async () => {
    toast("Looking for your Owlet APK…", "");
    const r = await (await fetch("/api/extract_libs", {method:"POST"})).json();
    toast(r.message, r.ok ? "good" : "bad");
    refreshStatus();
  });
}
function uploadApk() {
  const f = $("apk-file").files[0];
  if (!f) { toast("Choose your Owlet .apkm / .apk first.", "warn"); return; }
  const btn = $("btn-upload"), prog = $("up-prog"), bar = $("up-bar");
  btn.classList.add("loading"); btn.disabled = true;
  prog.classList.add("show"); bar.style.width = "0%";
  const fd = new FormData(); fd.append("apk", f);
  const xhr = new XMLHttpRequest();
  xhr.open("POST", "/api/upload_apk");
  xhr.upload.onprogress = (e) => {
    if (e.lengthComputable) bar.style.width = Math.round(e.loaded / e.total * 100) + "%";
  };
  const done = (ok, msg) => {
    btn.classList.remove("loading"); btn.disabled = false;
    bar.style.width = "100%"; setTimeout(() => prog.classList.remove("show"), 800);
    toast(msg, ok ? "good" : "bad"); refreshStatus();
  };
  xhr.onload = () => {
    try { const r = JSON.parse(xhr.responseText); done(r.ok, r.message); }
    catch (e) { done(false, xhr.status === 413 ? "File too large." : "Upload failed."); }
  };
  xhr.onerror = () => done(false, "Upload failed — network error.");
  toast("Uploading… this can take a minute for a full APK.", "");
  xhr.send(fd);
}

/* ---------- status / stream ---------- */
function setPill(id, state){ const el = $(id); if (el) el.className = "pill " + state; }
function banner(kind, ico, html){
  return `<div class="banner ${kind}"><span class="ico">${ico}</span><div>${html}</div></div>`;
}
async function refreshStatus() {
  let s;
  try { s = await (await fetch("/api/status")).json(); } catch(e){ return; }
  const host = location.hostname;

  setPill("pill-libs",  s.have_libs ? "on" : "off");
  setPill("pill-login", s.have_login ? "on" : "off");
  setPill("pill-uid",   s.have_uid ? "on" : "off");
  setPill("pill-stream", s.stream_up ? "on" : (s.have_libs && s.have_uid ? "warn" : "off"));

  $("card-account").classList.toggle("done", s.have_login);
  $("card-camera").classList.toggle("done", s.have_uid);
  $("card-libs").classList.toggle("done", s.have_libs);

  $("u-rtsp").textContent = `rtsp://${host}:8554/owlet`;
  $("u-web").textContent  = `http://${host}:1984/stream.html?src=owlet`;
  $("u-hls").textContent  = `http://${host}:1984/api/stream.m3u8?src=owlet`;
  $("rtsp-hint").textContent = `rtsp://${host}:8554/owlet`;
  const gl = $("go2rtc-link"); if (gl) gl.href = `http://${host}:1984/`;

  const lb = $("live-badge"), lt = $("live-text");
  lb.classList.toggle("live", s.stream_up);
  lt.textContent = s.stream_up ? "live" : "offline";
  const img = $("preview-img"), ph = $("preview-ph");
  if (s.stream_up) {
    img.style.display = "block"; ph.style.display = "none";
    img.src = `http://${host}:1984/api/frame.jpeg?src=owlet&t=${Date.now()}`;
  } else {
    img.style.display = "none"; ph.style.display = "block";
  }

  const sb = $("stream-banner");
  if (!s.config_writable) {
    sb.innerHTML = banner("bad", "⛔",
      "The config folder isn't writable. On Unraid: <code>chmod -R 777 /mnt/user/appdata/owlet/config</code> then restart.");
  } else if (!s.have_libs) {
    sb.innerHTML = banner("warn", "🧩", "TUTK libraries missing — see the <b>TUTK libraries</b> section below.");
    ph.textContent = "Libraries missing";
  } else if (!s.have_uid) {
    sb.innerHTML = banner("warn", "🔑", "Enter your account + Camera DSN in step 1 and click <b>Connect &amp; Diagnose</b>.");
    ph.textContent = "No camera key yet";
  } else if (!s.stream_up) {
    sb.innerHTML = banner("", "⏳", "Credentials are set. Click <b>Restart stream</b> — the first connect takes ~10s.");
    ph.textContent = "Connecting…";
  } else {
    sb.innerHTML = banner("good", "✅", "Live. Point Frigate at the RTSP URL above.");
  }

  $("libs-banner").innerHTML = s.have_libs
    ? banner("good", "✅", "TUTK libraries loaded.")
    : banner("warn", "📦",
        "Not found. Put your Owlet <code>.apkm</code> (or <code>.apk</code>) in the mounted "
        + "<b>config</b> folder, then click <b>Extract</b> below. They're never bundled here — "
        + "you supply the app you downloaded.");
}

/* ---------- log ---------- */
function startLogStream() {
  const es = new EventSource("/api/logs");
  const l = $("log");
  es.onmessage = (e) => { l.textContent += e.data + "\n"; l.scrollTop = l.scrollHeight; };
}

/* ---------- discovery ---------- */
async function discover(btn) {
  return withLoading(btn, async () => {
    const ip = $("cam_ip").value.trim();
    await fetch("/api/discover", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({ip})});
    toast("Probing the LAN on UDP 63616…", "");
    setTimeout(refreshFindings, 9000);
  });
}

/* ---------- copy buttons ---------- */
document.addEventListener("click", (e) => {
  const b = e.target.closest("[data-copy]"); if (!b) return;
  const txt = $(b.dataset.copy).textContent;
  navigator.clipboard.writeText(txt).then(() => { const o=b.textContent; b.textContent="copied!"; setTimeout(()=>b.textContent=o,1200); });
});

/* ---------- wire up ---------- */
$("btn-save").onclick    = (e) => save(e.currentTarget);
$("btn-save2").onclick   = (e) => saveAndRestart(e.currentTarget);
$("btn-diagnose").onclick = (e) => diagnose(e.currentTarget);
$("btn-extract").onclick = (e) => extractLibs(e.currentTarget);
$("btn-upload").onclick  = () => uploadApk();
$("btn-discover").onclick = (e) => discover(e.currentTarget);
$("btn-restart").onclick = (e) => withLoading(e.currentTarget, async () => {
  await fetch("/api/stream/restart", {method:"POST"}); toast("Stream restarting…", ""); setTimeout(refreshStatus, 1500);
});
$("btn-clear").onclick = () => $("log").textContent = "";
$("btn-copy").onclick  = () => navigator.clipboard.writeText($("log").textContent).then(()=>toast("Log copied.", "good"));

loadConfig();
startLogStream();
refreshStatus();
refreshFindings();
setInterval(refreshStatus, 5000);
