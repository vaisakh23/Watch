(() => {
  const content = document.getElementById("page-content");
  const player = document.getElementById("persistent-player");
  const audio = document.getElementById("persistent-audio");
  const title = document.getElementById("player-title");
  const artist = document.getElementById("player-artist");
  const cover = document.getElementById("player-cover");
  let queue = [];
  let current = -1;

  function play(index) {
    if (index < 0 || index >= queue.length) return;
    current = index;
    const item = queue[current];
    player.hidden = false;
    audio.src = item.src;
    title.textContent = item.title;
    artist.textContent = item.artist;
    if (item.cover) {
      cover.src = item.cover;
      cover.hidden = false;
    } else {
      cover.removeAttribute("src");
      cover.hidden = true;
    }
    audio.play().catch(() => {});
    document.querySelectorAll(".track-row").forEach((row) => {
      row.classList.toggle("playing", row.dataset.src === item.src);
    });
  }

  function navigate(url, push = true) {
    return fetch(url, { headers: { "X-Requested-With": "fetch" } })
      .then((response) => response.text().then((html) => ({ response, html })))
      .then(({ response, html }) => {
        const parsed = new DOMParser().parseFromString(html, "text/html");
        const nextContent = parsed.getElementById("page-content");
        if (!response.ok || !nextContent) {
          window.location.assign(response.url || url);
          return;
        }
        if (window.__librarySyncTimer) clearInterval(window.__librarySyncTimer);
        if (window.__musicStatusTimer) clearInterval(window.__musicStatusTimer);
        content.innerHTML = nextContent.innerHTML;
        document.title = parsed.title;
        if (push) history.pushState({}, "", response.url || url);
        window.scrollTo(0, 0);
        content.querySelectorAll("script").forEach((oldScript) => {
          const newScript = document.createElement("script");
          for (const attr of oldScript.attributes) newScript.setAttribute(attr.name, attr.value);
          newScript.textContent = oldScript.textContent;
          oldScript.replaceWith(newScript);
        });
      })
      .catch(() => window.location.assign(url));
  }

  function startRow(row) {
    if (!row || !row.dataset.src || row.dataset.status !== "ready") return;
    const rows = [...document.querySelectorAll(".track-row[data-src]")]
      .filter((item) => item.dataset.src && item.dataset.status === "ready");
    queue = rows.map((item) => ({
      src: item.dataset.src,
      title: item.dataset.title || "Unknown track",
      artist: item.dataset.artist || "",
      cover: item.querySelector(".track-cover:not(.track-cover-empty)")?.src || "",
    }));
    play(rows.indexOf(row));
  }

  document.addEventListener("click", (event) => {
    const row = event.target.closest(".track-row[role='button']");
    if (row) {
      event.preventDefault();
      startRow(row);
      return;
    }

    const link = event.target.closest("a[href]");
    if (!link || event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey ||
        event.shiftKey || event.altKey || link.target || link.hasAttribute("download")) return;
    const url = new URL(link.href, location.href);
    if (url.origin !== location.origin || url.pathname.startsWith("/stream/") ||
        (url.pathname === location.pathname && url.search === location.search && url.hash)) return;
    event.preventDefault();
    navigate(url.href);
  });

  document.addEventListener("keydown", (event) => {
    const row = event.target.closest?.(".track-row[role='button']");
    if (row && (event.key === "Enter" || event.key === " ")) {
      event.preventDefault();
      startRow(row);
    }
  });

  document.addEventListener("submit", (event) => {
    const form = event.target;
    if (form.target || new URL(form.action, location.href).origin !== location.origin) return;
    event.preventDefault();
    const url = new URL(form.action, location.href);
    const method = (form.method || "GET").toUpperCase();
    const options = { method, headers: { "X-Requested-With": "fetch" } };
    if (method === "GET") {
      new URLSearchParams(new FormData(form)).forEach((value, key) => url.searchParams.append(key, value));
    } else {
      options.body = new FormData(form);
    }
    fetch(url, options)
      .then((response) => response.text().then((html) => ({ response, html })))
      .then(({ response, html }) => {
        const parsed = new DOMParser().parseFromString(html, "text/html");
        if (!response.ok || !parsed.getElementById("page-content")) {
          window.location.assign(response.url || url);
          return;
        }
        if (window.__librarySyncTimer) clearInterval(window.__librarySyncTimer);
        if (window.__musicStatusTimer) clearInterval(window.__musicStatusTimer);
        content.innerHTML = parsed.getElementById("page-content").innerHTML;
        document.title = parsed.title;
        history.pushState({}, "", response.url || url);
        window.scrollTo(0, 0);
        content.querySelectorAll("script").forEach((oldScript) => {
          const script = document.createElement("script");
          for (const attr of oldScript.attributes) script.setAttribute(attr.name, attr.value);
          script.textContent = oldScript.textContent;
          oldScript.replaceWith(script);
        });
      })
      .catch(() => form.submit());
  });

  document.getElementById("player-prev").addEventListener("click", () => play(current - 1));
  document.getElementById("player-next").addEventListener("click", () => play(current + 1));
  audio.addEventListener("ended", () => play(current + 1));
  window.addEventListener("popstate", () => navigate(location.href, false));
})();
