// Small progressive enhancements. Every page works without JavaScript.
(function () {
  "use strict";

  // Live "is this an existing customer?" check on the quick-entry form.
  var phone = document.querySelector("[data-phone-lookup]");
  var hint = document.getElementById("phone-hint");
  if (phone && hint) {
    var timer = null;
    var nameInput = document.getElementById("customer_name");
    var render = function (data) {
      hint.className = "lookup-hint";
      hint.textContent = "";
      if (!data) return;
      if (!data.valid) {
        hint.classList.add("is-invalid");
        hint.textContent = "Not a valid phone number yet.";
        return;
      }
      if (!data.customer) {
        hint.classList.add("is-new");
        hint.textContent = "New customer: " + data.display;
        return;
      }
      var c = data.customer;
      hint.classList.add("is-known");
      var text = "Existing customer" + (c.name ? " " + c.name : "") + " (" + c.total_inquiries + " inquiries).";
      hint.appendChild(document.createTextNode(text + " "));
      if (c.open_inquiries.length) {
        hint.appendChild(document.createTextNode("Open: "));
        c.open_inquiries.forEach(function (i, idx) {
          var a = document.createElement("a");
          a.href = "/inquiries/" + i.id;
          a.textContent = "#" + i.id + " " + i.product + " (" + i.status + (i.assigned_to ? ", " + i.assigned_to : "") + ")";
          hint.appendChild(a);
          if (idx < c.open_inquiries.length - 1) hint.appendChild(document.createTextNode(", "));
        });
      }
      if (nameInput && !nameInput.value && c.name) nameInput.value = c.name;
    };
    var lookup = function () {
      var value = phone.value.trim();
      if (value.replace(/\D/g, "").length < 9) { render(null); return; }
      fetch("/customers/lookup?phone=" + encodeURIComponent(value), {
        headers: { Accept: "application/json" },
        credentials: "same-origin",
      })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(render)
        .catch(function () { render(null); });
    };
    phone.addEventListener("input", function () {
      clearTimeout(timer);
      timer = setTimeout(lookup, 350);
    });
    if (phone.value) lookup();
  }

  // Show fields only when they apply (lost reason, next follow-up date, purchase date).
  // An element with data-show-for="<field name>" data-show-when="a,b" is shown only when
  // that field (a <select> or a radio group) has one of those values.
  var currentValue = function (name) {
    var checked = document.querySelector('input[type="radio"][name="' + name + '"]:checked');
    if (checked) return checked.value;
    var field = document.querySelector('[name="' + name + '"]');
    return field && field.type !== "radio" ? field.value : "";
  };
  var syncDependents = function () {
    document.querySelectorAll("[data-show-for]").forEach(function (el) {
      var wanted = el.getAttribute("data-show-when").split(",");
      var match = wanted.indexOf(currentValue(el.getAttribute("data-show-for"))) !== -1;
      var negate = el.hasAttribute("data-show-negate");
      el.classList.toggle("is-hidden", negate ? match : !match);
    });
  };
  document.addEventListener("change", syncDependents);
  syncDependents();

  // Home-screen app: register the service worker on every page.
  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/sw.js").catch(function () {});
  }

  // Phone notifications (Notifications page).
  var pushCard = document.getElementById("push-card");
  if (pushCard) {
    var statusEl = document.getElementById("push-status");
    var enableBtn = document.getElementById("push-enable");
    var iosBox = document.getElementById("ios-install");
    var ua = navigator.userAgent;
    var isIOS = /iPhone|iPad|iPod/.test(ua);
    var standalone = window.matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;
    var supported = "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;
    var show = function (el, on) { el.classList.toggle("is-hidden", !on); };
    var say = function (text) { statusEl.textContent = text; };
    var deviceLabel = function () {
      var os = /iPhone/.test(ua) ? "iPhone" : /iPad/.test(ua) ? "iPad" : /Android/.test(ua) ? "Android phone"
        : /Mac/.test(ua) ? "Mac" : /Windows/.test(ua) ? "Windows PC" : "Device";
      var browser = /Edg\//.test(ua) ? "Edge" : /Chrome\//.test(ua) ? "Chrome" : /Firefox\//.test(ua) ? "Firefox"
        : /Safari\//.test(ua) ? "Safari" : "browser";
      return os + (standalone ? " (home-screen app)" : " · " + browser);
    };
    var keyBytes = function (base64) {
      var padded = (base64 + "===".slice((base64.length + 3) % 4)).replace(/-/g, "+").replace(/_/g, "/");
      var raw = atob(padded), out = new Uint8Array(raw.length);
      for (var i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
      return out;
    };
    var csrf = (document.querySelector('meta[name="csrf-token"]') || {}).content || "";

    var refresh = function () {
      if (isIOS && !standalone) { show(iosBox, true); say("Add the app to your Home Screen to get notifications on iPhone."); return; }
      if (!supported) { say("This browser can't receive notifications. Use Chrome on Android, or the home-screen app on iPhone."); return; }
      if (Notification.permission === "denied") {
        say("Notifications are blocked for this site. Allow them in your phone's settings for this app/browser, then reload.");
        return;
      }
      navigator.serviceWorker.ready.then(function (reg) { return reg.pushManager.getSubscription(); })
        .then(function (sub) {
          if (sub) { say("✅ Notifications are on for this device."); show(enableBtn, true); enableBtn.textContent = "Re-connect this device"; }
          else { say("Notifications are off on this device."); show(enableBtn, true); }
        });
    };

    enableBtn.addEventListener("click", function () {
      enableBtn.disabled = true;
      say("Asking for permission…");
      Notification.requestPermission().then(function (permission) {
        if (permission !== "granted") throw new Error("Permission was not given.");
        return navigator.serviceWorker.ready;
      }).then(function (reg) {
        return reg.pushManager.getSubscription().then(function (existing) {
          return existing || reg.pushManager.subscribe({
            userVisibleOnly: true,
            applicationServerKey: keyBytes(pushCard.getAttribute("data-public-key")),
          });
        });
      }).then(function (sub) {
        var json = sub.toJSON();
        return fetch("/account/notifications/subscribe", {
          method: "POST",
          credentials: "same-origin",
          headers: { "Content-Type": "application/json", Accept: "application/json", "X-CSRF-Token": csrf },
          body: JSON.stringify({ endpoint: json.endpoint, keys: json.keys, device_label: deviceLabel() }),
        });
      }).then(function (res) {
        if (!res.ok) throw new Error("The server didn't accept the device (" + res.status + ").");
        window.location.reload();
      }).catch(function (err) {
        say("Couldn't turn on notifications: " + err.message);
        enableBtn.disabled = false;
      });
    });
    refresh();
  }

  // Confirmation for destructive actions.
  document.querySelectorAll("form[data-confirm]").forEach(function (form) {
    form.addEventListener("submit", function (e) {
      if (!window.confirm(form.getAttribute("data-confirm"))) e.preventDefault();
    });
  });

  // Auto-submit filter forms when a select changes.
  document.querySelectorAll("form[data-autosubmit] select").forEach(function (select) {
    select.addEventListener("change", function () { select.form.submit(); });
  });
})();
