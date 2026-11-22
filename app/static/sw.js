// Service worker: makes the app installable and shows phone notifications.
// Served from /sw.js so it controls the whole site. No offline caching on purpose:
// lead data must always be live.
self.addEventListener("install", function () { self.skipWaiting(); });
self.addEventListener("activate", function (event) { event.waitUntil(self.clients.claim()); });

self.addEventListener("push", function (event) {
  var data = {};
  try { data = event.data ? event.data.json() : {}; }
  catch (e) { data = { body: event.data ? event.data.text() : "" }; }
  var title = data.title || "iDealz Leads";
  event.waitUntil(self.registration.showNotification(title, {
    body: data.body || "",
    icon: "/static/icons/icon-192.png",
    badge: "/static/icons/badge-96.png",
    tag: data.tag || undefined,
    renotify: !!data.tag,
    data: { url: data.url || "/" },
  }));
});

self.addEventListener("notificationclick", function (event) {
  event.notification.close();
  var url = new URL((event.notification.data && event.notification.data.url) || "/", self.location.origin).href;
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then(function (list) {
      for (var i = 0; i < list.length; i++) {
        var client = list[i];
        if (client.url.indexOf(self.location.origin) === 0 && "focus" in client) {
          return client.focus().then(function (c) { return c.navigate ? c.navigate(url) : c; });
        }
      }
      return self.clients.openWindow(url);
    })
  );
});
