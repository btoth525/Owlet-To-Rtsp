const $ = (id) => document.getElementById(id);
const FIELDS = ["region","email","password","uid","authkey","av_account","av_password","iotype_start","av_channel"];

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

async function save() {
  const body = collect();
  if (body.password === "********") delete body.password;
  await fetch("/api/config", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)});
  flash("Saved.");
}

async function saveAndRestart() {
  await save();
  await fetch("/api/stream/restart", {method:"POST"});
  flash("Saved · stream restarting.");
}

async function diagnose() {
  const body = {region:$("region").value, email:$("email").value, password:$("password").value};
  $("btn-diagnose").disabled = true;
  await fetch("/api/diagnose", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)});
  setTimeout(() => { $("btn-diagnose").disabled = false; refreshFindings(); }, 1500);
  pollFindings();
}

function pollFindings(){ let n=0; const t=setInterval(async()=>{ await refreshFindings(); if(++n>40) clearInterval(t); }, 1500); }

async function refreshFindings() {
  const data = await (await fetch("/api/findings")).json();
  const box = $("findings");
  if (!data.candidates || !data.candidates.length) {
    box.textContent = data.devices === 0
      ? "Login OK but 0 Ayla devices — the Cam is likely on a different endpoint. Share the log."
      : "No candidates yet. Run a diagnostic.";
    return;
  }
  box.innerHTML = "";
  for (const c of data.candidates) {
    const d = document.createElement("div");
    d.className = "cand";
    d.innerHTML = `<span class="f">${c.field}</span><span class="v">${c.value}</span>`;
    d.title = "click to use as UID";
    d.onclick = () => {
      if (/auth/i.test(c.field)) $("authkey").value = c.value; else $("uid").value = c.value;
      flash("Filled from candidate.");
    };
    box.appendChild(d);
  }
}

async function refreshStatus() {
  try {
    const s = await (await fetch("/api/status")).json();
    $("status").innerHTML =
      dot("login", s.have_login) + dot("uid", s.have_uid) + dot("stream", s.stream_up);
    const host = location.hostname;
    $("rtsp-hint").textContent = `rtsp://${host}:8554/owlet`;
    const gl = $("go2rtc-link"); if (gl) gl.href = `http://${host}:1984/`;
  } catch(e){}
}
function dot(label, on){ return `<span class="dot ${on?'on':'off'}">${label}</span>`; }

function flash(msg){ const l=$("log"); l.textContent += `\n>>> ${msg}\n`; l.scrollTop=l.scrollHeight; }

function startLogStream() {
  const es = new EventSource("/api/logs");
  const l = $("log");
  es.onmessage = (e) => { l.textContent += e.data + "\n"; l.scrollTop = l.scrollHeight; };
}

$("btn-save").onclick = save;
$("btn-save2").onclick = saveAndRestart;
$("btn-diagnose").onclick = diagnose;
$("btn-clear").onclick = () => $("log").textContent = "";
$("btn-copy").onclick = () => navigator.clipboard.writeText($("log").textContent).then(()=>flash("Log copied."));

loadConfig();
startLogStream();
refreshStatus();
refreshFindings();
setInterval(refreshStatus, 5000);
