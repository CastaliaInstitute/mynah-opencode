const SCOPE = self.registration.scope;
const SHELL = ["", "index.html", "style.css", "app.js", "manifest.webmanifest"].map((path) => SCOPE + path);

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open("mynah-shell-v6").then((cache) => cache.addAll(SHELL)));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((key) => key !== "mynah-shell-v6").map((key) => caches.delete(key)))
    )
  );
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== "GET" || !url.href.startsWith(SCOPE) || url.href.startsWith(SCOPE + "api/")) return;

  event.respondWith(
    caches.match(event.request).then((cached) => {
      const network = fetch(event.request)
        .then((response) => {
          if (response.ok && SHELL.includes(url.href)) {
            const copy = response.clone();
            caches.open("mynah-shell-v6").then((cache) => cache.put(event.request, copy));
          }
          return response;
        })
        .catch(() => cached);
      return cached || network;
    })
  );
});
