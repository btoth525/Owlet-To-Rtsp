/*
 * hook-tutk-ioctl.js — dump the Owlet app's live TUTK/Kalay control protocol.
 *
 * This is GATE 2. A valid AV session is not enough: Kalay cameras only start
 * streaming after the client sends a vendor-specific IOCTL. This hooks the
 * native TUTK AV API so you can SEE exactly which IOCTL types + payloads the
 * real app sends to start video/audio — then replay them from the bridge.
 *
 * It hooks (by export name) in libAVAPIs.so / libIOTCAPIs.so:
 *   avSendIOCtrl(av_index, type, data, len)         -> client -> camera
 *   avRecvIOCtrl(av_index, type, data, len, timeout)-> camera -> client
 *   avClientStart / avClientStart2 / avClientStartEx -> shows the auth args
 *   IOTC_Connect_ByUID_Parallel                      -> shows the UID used
 *
 * Usage:
 *   frida -U -n com.owletcare.owletcare -l hook-tutk-ioctl.js
 *   (attach AFTER the app is live-viewing, or spawn and open the camera)
 *
 * Note: function names/signatures vary by TUTK SDK version. We resolve exports
 * dynamically and log whatever is present; adjust arg indexes if a build differs.
 */

function hexdump_ptr(p, len) {
  if (p.isNull() || len <= 0) return "<null>";
  len = Math.min(len, 256);
  try {
    return hexdump(p, { length: len, ansi: false });
  } catch (e) {
    return "<unreadable>";
  }
}

function findExport(name) {
  // Search all loaded modules for the export (lib name varies).
  var addr = Module.findExportByName(null, name);
  if (addr) return addr;
  var mods = ["libAVAPIs.so", "libIOTCAPIs.so", "libTUTKGlobalAPIs.so"];
  for (var i = 0; i < mods.length; i++) {
    try {
      addr = Module.findExportByName(mods[i], name);
      if (addr) return addr;
    } catch (e) {}
  }
  return null;
}

function hookSend() {
  var a = findExport("avSendIOCtrl");
  if (!a) { console.log("[tutk] avSendIOCtrl not found yet"); return; }
  Interceptor.attach(a, {
    onEnter: function (args) {
      var avIndex = args[0].toInt32();
      var type = args[1].toInt32();
      var data = args[2];
      var len = args[3].toInt32();
      console.log("\n[tutk] >>> avSendIOCtrl av=" + avIndex +
        " type=0x" + type.toString(16) + " len=" + len);
      console.log(hexdump_ptr(data, len));
    },
  });
  console.log("[tutk] hooked avSendIOCtrl @ " + a);
}

function hookRecv() {
  var a = findExport("avRecvIOCtrl");
  if (!a) { console.log("[tutk] avRecvIOCtrl not found yet"); return; }
  Interceptor.attach(a, {
    onEnter: function (args) {
      this.type = args[1].toInt32();
      this.data = args[2];
      this.len = args[3].toInt32();
    },
    onLeave: function (ret) {
      console.log("\n[tutk] <<< avRecvIOCtrl type=0x" + this.type.toString(16) +
        " ret=" + ret.toInt32());
      console.log(hexdump_ptr(this.data, this.len));
    },
  });
  console.log("[tutk] hooked avRecvIOCtrl @ " + a);
}

function hookStart() {
  ["avClientStart2", "avClientStart", "avClientStartEx"].forEach(function (name) {
    var a = findExport(name);
    if (!a) return;
    Interceptor.attach(a, {
      onEnter: function (args) {
        // For start2: (sid, account*, password*, timeout, &servtype, channel, &resend)
        function rd(p) { try { return p.readUtf8String(); } catch (e) { return "<bin>"; } }
        console.log("\n[tutk] " + name +
          " account=" + rd(args[1]) + " password/authkey=" + rd(args[2]));
      },
    });
    console.log("[tutk] hooked " + name + " @ " + a);
  });
}

function hookConnect() {
  var a = findExport("IOTC_Connect_ByUID_Parallel");
  if (!a) return;
  Interceptor.attach(a, {
    onEnter: function (args) {
      var uid = "<bin>";
      try { uid = args[0].readUtf8String(); } catch (e) {}
      console.log("\n[tutk] IOTC_Connect_ByUID_Parallel UID=" + uid);
    },
  });
  console.log("[tutk] hooked IOTC_Connect_ByUID_Parallel @ " + a);
}

// Native libs may load lazily; retry a few times.
var tries = 0;
var timer = setInterval(function () {
  tries++;
  if (findExport("avSendIOCtrl") || tries > 30) {
    clearInterval(timer);
    hookConnect();
    hookStart();
    hookSend();
    hookRecv();
    console.log("[tutk] hook install complete (tries=" + tries + ")");
  }
}, 1000);
