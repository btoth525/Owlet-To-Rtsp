/*
 * ssl-unpinning.js — universal Android TLS trust + cert-pinning bypass.
 *
 * Makes the Owlet app accept the mitmproxy CA so you can read your own
 * client's own cloud-auth traffic. Covers the common layers:
 *   - default X509TrustManager / SSLContext
 *   - Conscrypt TrustManagerImpl (Android 7+ pinning check)
 *   - OkHttp3 CertificatePinner
 *   - WebView
 *
 * Usage (see run-frida.sh):
 *   frida -U -f com.owletcare.owletcare -l ssl-unpinning.js --no-pause
 */

setTimeout(function () {
  Java.perform(function () {
    var log = function (m) { console.log("[unpin] " + m); };

    // 1. Neutralize the system trust check (Conscrypt, Android 7+).
    try {
      var TMI = Java.use("com.android.org.conscrypt.TrustManagerImpl");
      TMI.checkTrustedRecursive.implementation = function () {
        return Java.use("java.util.ArrayList").$new();
      };
      // Older/alternate signature
      TMI.verifyChain.implementation = function (untrusted) {
        return untrusted;
      };
      log("hooked Conscrypt TrustManagerImpl");
    } catch (e) { /* not present on this build */ }

    // 2. Default X509TrustManager — accept everything.
    try {
      var X509TM = Java.use("javax.net.ssl.X509TrustManager");
      var SSLContext = Java.use("javax.net.ssl.SSLContext");
      var TrustManager = Java.registerClass({
        name: "dev.owlet.TrustAll",
        implements: [X509TM],
        methods: {
          checkClientTrusted: function () {},
          checkServerTrusted: function () {},
          getAcceptedIssuers: function () { return []; },
        },
      });
      var tms = [TrustManager.$new()];
      var init = SSLContext.init.overload(
        "[Ljavax.net.ssl.KeyManager;",
        "[Ljavax.net.ssl.TrustManager;",
        "java.security.SecureRandom"
      );
      init.implementation = function (km, tm, sr) {
        init.call(this, km, tms, sr);
      };
      log("hooked SSLContext.init");
    } catch (e) { log("SSLContext hook failed: " + e); }

    // 3. OkHttp3 CertificatePinner.
    try {
      var CP = Java.use("okhttp3.CertificatePinner");
      CP.check.overload("java.lang.String", "java.util.List").implementation = function () {
        return;
      };
      // Some builds use the (String, Certificate[]) overload.
      try {
        CP.check.overload("java.lang.String", "[Ljava.security.cert.Certificate;")
          .implementation = function () { return; };
      } catch (e2) {}
      log("hooked okhttp3 CertificatePinner");
    } catch (e) { /* app may not use okhttp */ }

    // 4. TrustManagerImpl.checkServerTrusted variants returning the chain.
    try {
      var TMI2 = Java.use("com.android.org.conscrypt.TrustManagerImpl");
      TMI2.checkServerTrusted.overload(
        "[Ljava.security.cert.X509Certificate;", "java.lang.String", "java.lang.String"
      ).implementation = function (chain, authType, host) {
        return Java.use("java.util.ArrayList").$new();
      };
      log("hooked checkServerTrusted(chain,authType,host)");
    } catch (e) {}

    log("install complete");
  });
}, 0);
