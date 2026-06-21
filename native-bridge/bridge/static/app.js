const $ = (id) => document.getElementById(id);
const ACCOUNT_FIELDS = ["region","email","password","av_account","iotype_start",
  "av_channel","region_code","license_key"];

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

/* ---------- copy (works on plain http too, where navigator.clipboard is blocked) ---------- */
async function copyText(text){
  try {
    if (navigator.clipboard && window.isSecureContext){ await navigator.clipboard.writeText(text); return true; }
  } catch(e){}
  try {
    const ta = document.createElement("textarea");
    ta.value = text; ta.setAttribute("readonly","");
    ta.style.position="fixed"; ta.style.top="-1000px"; ta.style.opacity="0";
    document.body.appendChild(ta); ta.select(); ta.setSelectionRange(0, text.length);
    const ok = document.execCommand("copy"); document.body.removeChild(ta); return ok;
  } catch(e){ return false; }
}
/* copy buttons: data-copy=<id>  OR  .copy inside a .url-row (copies its <code>) */
document.addEventListener("click", async (e) => {
  const b = e.target.closest("[data-copy], .copy"); if (!b) return;
  let text = "";
  if (b.dataset.copy) text = $(b.dataset.copy).textContent;
  else { const row = b.closest(".url-row"); if (row) text = row.querySelector("code").textContent; }
  const ok = await copyText(text);
  const o = b.textContent; b.textContent = ok ? "copied!" : "select & copy";
  setTimeout(() => b.textContent = o, 1300);
});

/* ---------- account ---------- */
function collectAccount() {
  const o = {};
  for (const f of ACCOUNT_FIELDS) { const el = $(f); if (el) o[f] = el.value; }
  return o;
}
async function loadAccount() {
  const cfg = await (await fetch("/api/config")).json();
  for (const f of ACCOUNT_FIELDS) { const el = $(f); if (el && cfg[f] !== undefined) el.value = cfg[f]; }
  if (cfg.password === "********") $("password").placeholder = "(saved — leave to keep)";
}
async function saveAccount(btn) {
  return withLoading(btn, async () => {
    const body = collectAccount();
    if (body.password === "********") delete body.password;
    const r = await fetch("/api/config", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)});
    toast(r.ok ? "Account saved." : "Couldn't save — config folder not writable (see log).", r.ok ? "good" : "bad");
    refreshStatus();
  });
}
async function testLogin(btn) {
  return withLoading(btn, async () => {
    const body = {region:$("region").value, email:$("email").value, password:$("password").value};
    if (!body.email || !body.password) { toast("Enter your Owlet email + password first.", "warn"); return; }
    await fetch("/api/diagnose", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)});
    toast("Testing your Owlet login… watch the log.", "");
    pollFindings();
  });
}

/* ---------- cameras ---------- */
function camUrls(name, s){
  const host = location.hostname, rp = s.rtsp_port || "8554", hp = s.http_port || "1984";
  return {
    rtsp: `rtsp://${host}:${rp}/${name}`,
    web:  `http://${host}:${hp}/stream.html?src=${name}`,
    hls:  `http://${host}:${hp}/api/stream.m3u8?src=${name}`,
  };
}
function fmtBytes(n){ if(!n) return ""; const u=["B","KB","MB","GB"]; let i=0;
  while(n>=1024&&i<3){n/=1024;i++;} return n.toFixed(i?1:0)+" "+u[i]; }

function buildCard(cam, ports){
  const el = document.createElement("section");
  el.className = "card cam"; el.dataset.name = cam.name;
  const u = camUrls(cam.name, ports);
  el.innerHTML = `
    <div class="cam-head">
      <h2>📹 <span class="cam-title">${cam.name}</span></h2>
      <span class="live-badge"><span class="dot"></span><span class="lt">offline</span></span>
      <span class="spacer"></span><span class="stats"></span>
    </div>
    <div class="urls">
      <div class="url-row"><span class="lbl">RTSP</span><code class="u-rtsp">${u.rtsp}</code><button class="mini copy">copy</button></div>
      <div class="url-row"><span class="lbl">WebRTC</span><code class="u-web">${u.web}</code><button class="mini copy">copy</button></div>
      <div class="url-row"><span class="lbl">HLS</span><code class="u-hls">${u.hls}</code><button class="mini copy">copy</button></div>
    </div>
    <div class="row">
      <button class="connect primary"><span class="spin"></span>↻ Connect / refresh key</button>
      <button class="snap">📷 Snapshot ↗</button>
      <button class="adv-toggle mini">Advanced</button>
      <span class="spacer"></span>
      <button class="remove mini danger">Remove</button>
    </div>
    <div class="cam-banner"></div>
    <div class="cam-adv hidden">
      <p class="hint">Auto-filled by Connect. You normally don't touch these.</p>
      <div class="grid">
        <label>Camera DSN <input class="f-dsn"></label>
        <label>UID <input class="f-uid"></label>
        <label>AuthKey <input class="f-authkey"></label>
        <label>AV password <input type="password" class="f-avpw" placeholder="(saved)"></label>
        <label>Security mode
          <select class="f-sec">
            <option value="">auto-probe</option><option value="2">Auto</option>
            <option value="1">DTLS</option><option value="0">Simple</option>
          </select></label>
      </div>
      <div class="row"><button class="save-adv">Save camera settings</button></div>
    </div>`;

  // advanced inputs — set once on creation so polling never clobbers typing
  el.querySelector(".f-dsn").value = cam.camera_dsn || "";
  el.querySelector(".f-uid").value = cam.uid || "";
  el.querySelector(".f-authkey").value = cam.authkey || "";
  el.querySelector(".f-sec").value = cam.av_security_mode || "";

  el.querySelector(".connect").onclick = (e) => withLoading(e.currentTarget, async () => {
    await fetch(`/api/cameras/${cam.name}/diagnose`, {method:"POST"});
    toast(`Connecting ${cam.name}… watch the log.`, "");
    setTimeout(refreshCameras, 2500);
  });
  el.querySelector(".snap").onclick = () => {
    const w = window.open(`/img/${cam.name}.jpg`, "_blank", "noopener");
    if (!w) toast("Popup blocked — open /img/" + cam.name + ".jpg", "warn");
  };
  el.querySelector(".adv-toggle").onclick = (e) => {
    const adv = el.querySelector(".cam-adv"); adv.classList.toggle("hidden");
    e.currentTarget.textContent = adv.classList.contains("hidden") ? "Advanced" : "Hide";
  };
  el.querySelector(".remove").onclick = async () => {
    if (!confirm(`Remove camera "${cam.name}"? Its stream stops immediately.`)) return;
    await fetch(`/api/cameras/${cam.name}`, {method:"DELETE"});
    toast(`Removed ${cam.name}.`, "good"); refreshCameras();
  };
  el.querySelector(".save-adv").onclick = (e) => withLoading(e.currentTarget, async () => {
    const body = {
      camera_dsn: el.querySelector(".f-dsn").value,
      uid: el.querySelector(".f-uid").value,
      authkey: el.querySelector(".f-authkey").value,
      av_security_mode: el.querySelector(".f-sec").value,
    };
    const avpw = el.querySelector(".f-avpw").value;
    if (avpw) body.av_password = avpw;
    const r = await fetch(`/api/cameras/${cam.name}`, {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)});
    toast(r.ok ? "Camera settings saved · stream restarting…" : "Couldn't save (see log).", r.ok ? "good" : "bad");
    setTimeout(refreshCameras, 1500);
  });
  return el;
}

function updateCard(el, cam){
  const up = cam.stream_up;
  const lb = el.querySelector(".live-badge"), lt = el.querySelector(".lt");
  lb.classList.toggle("live", up);
  lt.textContent = cam.busy ? "connecting…" : (up ? "live" : (cam.have_key ? "starting…" : "no key"));
  const st = el.querySelector(".stats");
  let h = "";
  if (up){ h += `<span class="chip ok">● live</span>`;
    if (cam.codec) h += `<span class="chip">${cam.codec}</span>`;
    if (cam.recv) h += `<span class="chip">${fmtBytes(cam.recv)}</span>`; }
  st.innerHTML = h;
  el.querySelector(".connect").classList.toggle("loading", !!cam.busy);

  const b = el.querySelector(".cam-banner");
  if (!cam.camera_dsn) b.innerHTML = banner("warn","✏️","Set this camera's DSN in <b>Advanced</b>, then Connect.");
  else if (cam.busy) b.innerHTML = banner("","⏳","Logging in and fetching the camera key…");
  else if (!cam.have_key) b.innerHTML = banner("warn","🔑","No camera key yet. Click <b>Connect / refresh key</b>.");
  else if (!up) b.innerHTML = banner("","⏳","Key set — stream is starting (first connect takes ~10s).");
  else b.innerHTML = banner("good","✅","Live. Point Frigate at the RTSP URL above.");
}

async function refreshCameras() {
  let data; try { data = await (await fetch("/api/cameras")).json(); } catch(e){ return; }
  window._ports = data;
  const list = $("camera-list");
  const have = new Set(data.cameras.map(c => c.name));
  // remove cards for cameras that are gone
  [...list.children].forEach(el => { if (!have.has(el.dataset.name)) el.remove(); });
  // add / update
  for (const cam of data.cameras) {
    let el = [...list.children].find(c => c.dataset.name === cam.name);
    if (!el) { el = buildCard(cam, data); list.appendChild(el); }
    updateCard(el, cam);
  }
  $("cameras-empty").style.display = data.cameras.length ? "none" : "flex";
  $("cam-count").textContent = data.cameras.length
    ? `${data.cameras.length} camera${data.cameras.length>1?"s":""}` : "";
}

async function addCamera(btn) {
  return withLoading(btn, async () => {
    const dsn = $("new-dsn").value.trim(), name = $("new-name").value.trim();
    if (!dsn) { toast("Enter the camera DSN (OCD…).", "warn"); return; }
    const r = await fetch("/api/cameras", {method:"POST", headers:{"Content-Type":"application/json"},
      body:JSON.stringify({name, camera_dsn:dsn})});
    const j = await r.json();
    if (!r.ok) { toast(j.error || "Couldn't add camera.", "bad"); return; }
    $("new-dsn").value = ""; $("new-name").value = "";
    toast(`Added ${j.name} — connecting… watch the log.`, "good");
    refreshCameras();
    [1500,4000,8000].forEach(t => setTimeout(refreshCameras, t));
  });
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

/* ---------- status ---------- */
function setPill(id, state){ const el = $(id); if (el) el.className = "pill " + state; }
function banner(kind, ico, html){
  return `<div class="banner ${kind}"><span class="ico">${ico}</span><div>${html}</div></div>`;
}
function updateSetup(s){
  const steps = [["libs",s.have_libs],["login",s.have_login],
    ["uid",s.cameras_with_key>0],["stream",s.streams_live>0]];
  let activeSet=false, allDone=true;
  for (const [name,done] of steps){
    const li=document.querySelector(`#checklist li[data-step="${name}"]`); if(!li) continue;
    li.classList.remove("done","active","pending");
    if(done) li.classList.add("done");
    else if(!activeSet){ li.classList.add("active"); activeSet=true; allDone=false; }
    else { li.classList.add("pending"); allDone=false; }
  }
  $("setup").classList.toggle("complete", allDone);
  const fresh = !s.have_libs && !s.have_login;
  $("setup-title").textContent = allDone ? "🦉 Owlet bridge"
    : (fresh ? "👋 First time? Let's get your camera streaming" : "🚀 Get your camera streaming");
}
async function refreshStatus() {
  let s; try { s = await (await fetch("/api/status")).json(); } catch(e){ return; }
  setPill("pill-libs",  s.have_libs ? "on" : "off");
  setPill("pill-login", s.have_login ? "on" : "off");
  setPill("pill-cams",  s.cameras_with_key>0 ? "on" : (s.cameras>0 ? "warn" : "off"));
  setPill("pill-live",  s.streams_live>0 ? "on" : (s.cameras>0 ? "warn" : "off"));
  $("card-account").classList.toggle("done", s.have_login);
  $("card-libs").classList.toggle("done", s.have_libs);
  $("card-cameras").classList.toggle("done", s.streams_live>0);
  updateSetup(s);

  const lb = $("libs-banner");
  if (lb) lb.innerHTML = s.have_libs
    ? banner("good", "✅", "TUTK libraries loaded.")
    : banner("warn", "📦",
        "Not found. Upload your Owlet <code>.apkm</code> (or <code>.apk</code>) below — "
        + "they're never bundled here, you supply the app you downloaded.");
  if (!s.config_writable) {
    toastOnce("⛔ Config folder not writable — run chmod -R 777 on your appdata config folder and restart.");
  }
}
let _warned=false;
function toastOnce(msg){ if(_warned) return; _warned=true; toast(msg,"bad"); }

/* ---------- log + findings ---------- */
function startLogStream() {
  const es = new EventSource("/api/logs");
  const l = $("log");
  es.onmessage = (e) => { l.textContent += e.data + "\n"; l.scrollTop = l.scrollHeight; };
}
function pollFindings(){ let n=0; const t=setInterval(async()=>{ await refreshFindings(); if(++n>30) clearInterval(t); }, 1500); }
async function refreshFindings() {
  const data = await (await fetch("/api/findings")).json();
  const box = $("findings"); if (!box) return;
  if (!data.candidates || !data.candidates.length) {
    box.textContent = data.devices === 0
      ? "Login OK but 0 Ayla devices on this account (the cam isn't an Ayla device — that's expected)."
      : "Run a login test to populate candidates.";
    return;
  }
  box.innerHTML = "";
  for (const c of data.candidates) {
    const d = document.createElement("div"); d.className = "cand";
    d.innerHTML = `<span class="f">${c.field}</span><span class="v">${c.value}</span>`;
    box.appendChild(d);
  }
}
async function discover(btn) {
  return withLoading(btn, async () => {
    const ip = $("cam_ip").value.trim();
    await fetch("/api/discover", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({ip})});
    toast("Probing the LAN on UDP 63616…", "");
    setTimeout(refreshFindings, 9000);
  });
}

/* ---------- wire up ---------- */
$("btn-save-account").onclick = (e) => saveAccount(e.currentTarget);
$("btn-testlogin").onclick = (e) => testLogin(e.currentTarget);
$("btn-add-cam").onclick = (e) => addCamera(e.currentTarget);
$("btn-extract").onclick = (e) => extractLibs(e.currentTarget);
$("btn-upload").onclick  = () => uploadApk();
$("btn-discover").onclick = (e) => discover(e.currentTarget);
$("btn-clear").onclick = () => $("log").textContent = "";
$("btn-copy").onclick  = async () => {
  const ok = await copyText($("log").textContent);
  toast(ok ? "Log copied." : "Couldn't copy — select the text and Ctrl+C.", ok ? "good" : "warn");
};

loadAccount();
startLogStream();
refreshStatus();
refreshCameras();
refreshFindings();
setInterval(refreshStatus, 5000);
setInterval(refreshCameras, 5000);
