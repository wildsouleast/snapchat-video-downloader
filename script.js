(function () {
  "use strict";
  var form = document.getElementById("form");
  var input = document.getElementById("url");
  var go = document.getElementById("go");
  var statusEl = document.getElementById("status");
  var results = document.getElementById("results");
  var tpl = document.getElementById("item-tpl");
  var pasteBtn = document.getElementById("paste");

  function setStatus(text, kind) {
    statusEl.textContent = text || "";
    statusEl.className = "status" + (kind ? " " + kind : "");
  }

  if (!(navigator.clipboard && navigator.clipboard.readText)) {
    pasteBtn.hidden = true;
  }
  pasteBtn.addEventListener("click", function () {
    navigator.clipboard.readText().then(function (t) {
      input.value = (t || "").trim();
      input.focus();
    }).catch(function () { input.focus(); });
  });

  function render(items) {
    results.innerHTML = "";
    items.forEach(function (it, i) {
      var node = tpl.content.firstElementChild.cloneNode(true);
      var media = node.querySelector(".media");
      var meta = node.querySelector(".meta");
      var note = node.querySelector(".note");
      var dl = node.querySelector(".dl");
      if (it.video) {
        var v = document.createElement("video");
        v.controls = true; v.playsInline = true; v.preload = "metadata";
        v.setAttribute("playsinline", "");
        if (it.poster) v.poster = it.poster;
        v.src = it.media;
        media.appendChild(v);
        dl.textContent = items.length > 1 ? "Download MP4 (" + (i + 1) + ")" : "Download MP4";
        if (it.clean) {
          note.textContent = "✓ Bina Snapchat logo wali clean copy (480p). No Snapchat watermark.";
          note.className = "note clean";
        } else {
          note.textContent = "Is video ki bina-logo copy Snapchat pe available nahi hai, isliye normal file mil rahi hai (logo ho sakta hai). Clean copy not available — this is the regular file.";
          note.className = "note warn";
        }
      } else {
        var img = document.createElement("img");
        img.src = it.media; img.alt = "Snap photo " + (i + 1);
        media.appendChild(img);
        dl.textContent = "Download photo (" + (i + 1) + ")";
      }
      var bits = [];
      if (it.by) bits.push("@" + it.by);
      if (it.title) bits.push(it.title);
      meta.textContent = bits.join(" · ");
      dl.href = it.download;
      results.appendChild(node);
    });
    results.hidden = false;
  }

  form.addEventListener("submit", function (e) {
    e.preventDefault();
    var url = input.value.trim();
    if (!url) {
      setStatus("Pehle Snapchat link paste karo. (Paste a link first.)", "err");
      input.focus();
      return;
    }
    go.disabled = true;
    results.hidden = true;
    setStatus("Video dhoondh rahe hain… (Finding your video)", "busy");
    fetch("/api/fetch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url: url })
    }).then(function (r) {
      return r.json().catch(function () { return { ok: false, error: "Server se jawab nahi aaya, dobara try karo. (No response, try again.)" }; });
    }).then(function (data) {
      if (data && data.ok && data.items && data.items.length) {
        render(data.items);
        setStatus(data.items.length > 1 ? data.items.length + " snaps mile 👇" : "Video mil gaya 👇", "ok");
        results.scrollIntoView({ behavior: "smooth", block: "start" });
      } else {
        setStatus((data && data.error) || "Kuch gadbad hui, dobara try karo. (Something went wrong.)", "err");
      }
    }).catch(function () {
      setStatus("Internet check karo aur dobara try karo. (Network error.)", "err");
    }).then(function () { go.disabled = false; });
  });
})();
