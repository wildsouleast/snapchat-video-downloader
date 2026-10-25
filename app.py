#!/usr/bin/env python3
"""Snap Downloader — tiny web server (Python standard library only, no installs).

POST /api/fetch {"url": "..."}  -> public Spotlight / short link / public story -> media list
GET  /media/<token>[?dl=1]      -> streams the clean (no Snapchat logo) copy straight from Snapchat.
Nothing is written to disk; tokens live in memory for 20 minutes.
"""
import json, os, re, time, threading, secrets, html, gzip, mimetypes
import urllib.request, urllib.error, urllib.parse, http.client
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("PORT", "8080"))
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
MAX_STORY_ITEMS = 5
TOKEN_TTL = 20 * 60
GAP_SECONDS = 6            # per IP, between two link lookups
MAX_PER_HOUR = 30          # per IP, link lookups per hour
STATIC = {"/": "index.html", "/index.html": "index.html", "/styles.css": "styles.css",
          "/script.js": "script.js", "/logo.svg": "logo.svg", "/og.png": "og.png", "/favicon.png": "favicon.png"}

ERR_BAD = "Link galat hai ya video delete ho gaya. Snapchat mein Share → Copy Link karke dobara paste karo. (This link is wrong or the video was deleted.)"
ERR_NOT_SNAP = "Ye Snapchat ka link nahi lag raha. Spotlight ya story ka link paste karo. (That doesn't look like a Snapchat link.)"
ERR_PRIVATE_PROFILE = "Is profile pe public story nahi hai, ya account private hai. Private snaps download nahi ho sakte. (No public story on this profile.)"
ERR_PRIVATE = "Is link mein koi public video nahi mili — ye private hai ya public story nahi hai. Sirf public Spotlight / public story chalegi. (No public video found at this link.)"
ERR_BLOCKED = "Snapchat abhi rok raha hai, thodi der baad try karo. (Snapchat is limiting requests right now — try again in a few minutes.)"
ERR_CONNECT = "Snapchat se connect nahi ho paya. 1–2 min baad dobara try karo. (Couldn't reach Snapchat — try again shortly.)"

LINK_RE = re.compile(r"https?://[^\s<>\"']*snapchat\.com/[^\s<>\"']*", re.I)

class SnapError(Exception):
    def __init__(self, msg, status=400):
        super().__init__(msg); self.msg = msg; self.status = status

def find_link(text):
    m = LINK_RE.search(text or "")
    if m:
        return m.group(0).rstrip(").,!")
    m = re.search(r"(?:^|\s)((?:www\.|t\.)?snapchat\.com/\S+)", text or "", re.I)
    return ("https://" + m.group(1)) if m else None

def _open(url, headers=None, timeout=25, method="GET"):
    h = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}
    h.update(headers or {})
    req = urllib.request.Request(url, headers=h, method=method)
    return urllib.request.urlopen(req, timeout=timeout)

def fetch_page(url):
    try:
        r = _open(url, {"Accept": "text/html,application/xhtml+xml", "Accept-Encoding": "gzip"})
        body = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            body = gzip.decompress(body)
        return r.status, r.geturl(), body.decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.geturl() or url, ""
    except Exception:
        raise SnapError(ERR_CONNECT, 502)

def clean_variant(url):
    """'.27.' file carries the Snapchat logo + username; '.1034.' is the same video without it (480p)."""
    base = url.split("?")[0]
    if ".27." in base:
        return base.replace(".27.", ".1034.")
    return None

def url_ok(url):
    try:
        r = _open(url, {"Range": "bytes=0-1023"}, timeout=20)
        ok = r.status in (200, 206) and "video" in (r.headers.get("Content-Type") or "")
        r.close(); return ok
    except Exception:
        return False

def next_data(text):
    m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except Exception:
        return None

def items_from_page(final_url, data):
    pp = (data or {}).get("props", {}).get("pageProps", {}) or {}
    out = []
    if "/spotlight/" in final_url:
        want = final_url.split("/spotlight/")[1].split("?")[0].split("/")[0]
        feed = (pp.get("spotlightFeed") or {}).get("spotlightStories") or []
        for st in feed:
            meta = st.get("metadata") or {}
            sid = (((st.get("story") or {}).get("storyId") or {}) or {}).get("value", "")
            link = (meta.get("oneLinkParams") or {}).get("deepLinkUrl") or ""
            snaps = (st.get("story") or {}).get("snapList") or []
            if snaps and (want in link or want == sid or not out):
                u = (snaps[0].get("snapUrls") or {}).get("mediaUrl")
                vm = meta.get("videoMetadata") or {}
                creator = ((vm.get("creator") or {}).get("personCreator") or {}).get("username") or ""
                thumb = vm.get("thumbnailUrl") or ""
                if u:
                    out = [{"url": u, "video": True, "title": vm.get("description") or vm.get("name") or "", "by": creator, "thumb": thumb}]
                if want in link or want == sid:
                    break
        if not out:
            vm = pp.get("videoMetadata") or {}
            if vm.get("contentUrl"):
                out = [{"url": vm["contentUrl"], "video": True, "title": vm.get("description", ""), "by": "", "thumb": vm.get("thumbnailUrl") or ""}]
        return out
    story = pp.get("story") or {}
    for s in (story.get("snapList") or [])[-MAX_STORY_ITEMS:]:
        u = (s.get("snapUrls") or {}).get("mediaUrl")
        if u:
            out.append({"url": u, "video": s.get("snapMediaType") == 1, "title": "", "by": "",
                        "thumb": (s.get("snapUrls") or {}).get("mediaPreviewUrl", {}).get("value", "") if isinstance((s.get("snapUrls") or {}).get("mediaPreviewUrl"), dict) else ""})
    return out

def ytdlp_items(url):
    try:
        import yt_dlp  # optional; used only if present on the server
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True}) as y:
            info = y.extract_info(url, download=False)
        out = []
        for e in (info.get("entries") or [info])[:MAX_STORY_ITEMS]:
            if e and e.get("url"):
                out.append({"url": e["url"], "video": True, "title": e.get("description") or "", "by": e.get("uploader") or "", "thumb": e.get("thumbnail") or ""})
        return out
    except Exception:
        return []

def resolve(link):
    status, final, text = fetch_page(link)
    if status == 404:
        raise SnapError(ERR_BAD, 404)
    if status in (403, 429):
        raise SnapError(ERR_BLOCKED, 429)
    if status >= 500:
        raise SnapError(ERR_CONNECT, 502)
    items = items_from_page(final, next_data(text))
    if not items and "/spotlight/" in final:
        items = ytdlp_items(final)
    if not items:
        if re.search(r"snapchat\.com(:\d+)?/(add/|@)", final):
            raise SnapError(ERR_PRIVATE_PROFILE, 404)
        if re.search(r"snapchat\.com/?(\?|$)", final) or "/unlock" in final:
            raise SnapError(ERR_BAD, 404)
        raise SnapError(ERR_PRIVATE, 404)
    return final, items

# ---------------- in-memory tokens + rate limit ----------------
_lock = threading.Lock()
_tokens = {}   # token -> {url, exp, video, clean, name}
_hits = {}     # ip -> [timestamps]

def _sweep():
    now = time.time()
    with _lock:
        for k in [k for k, v in _tokens.items() if v["exp"] < now]:
            _tokens.pop(k, None)
        for ip in [ip for ip, h in _hits.items() if not h or now - h[-1] > 3600]:
            _hits.pop(ip, None)

def rate_check(ip):
    now = time.time()
    with _lock:
        h = [t for t in _hits.get(ip, []) if now - t < 3600]
        if h and now - h[-1] < GAP_SECONDS:
            return f"Thoda ruko — {int(GAP_SECONDS - (now - h[-1])) + 1} sec baad agla link. (Please wait a few seconds.)"
        if len(h) >= MAX_PER_HOUR:
            return f"1 ghante mein max {MAX_PER_HOUR} links. Thodi der baad aao. (Hourly limit reached.)"
        h.append(now); _hits[ip] = h
    return None

def make_token(url, video, clean, name):
    t = secrets.token_urlsafe(16)
    with _lock:
        _tokens[t] = {"url": url, "exp": time.time() + TOKEN_TTL, "video": video, "clean": clean, "name": name}
    return t

def lookup(link):
    final, items = resolve(link)
    out = []
    for i, it in enumerate(items, 1):
        src, clean = it["url"], False
        if it["video"]:
            cv = clean_variant(src)
            if cv and url_ok(cv):
                src, clean = cv, True
            elif ".1034." in src:
                clean = True
        ext = "mp4" if it["video"] else "jpg"
        name = f"snapdownloader_{int(time.time())}_{i}.{ext}"
        tok = make_token(src, it["video"], clean, name)
        thumb_tok = make_token(it["thumb"], False, False, "thumb.jpg") if it.get("thumb", "").startswith("https://") else None
        title = (it.get("title") or "").strip()
        if title.lower().startswith("another spotlight snap"):
            title = ""
        out.append({"id": tok, "video": it["video"], "clean": clean if it["video"] else None,
                    "title": title[:200], "by": it.get("by") or "",
                    "media": f"/media/{tok}", "download": f"/media/{tok}?dl=1",
                    "poster": f"/media/{thumb_tok}" if thumb_tok else None})
    return out

# ---------------- HTTP ----------------
SEC_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; media-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
}

class H(BaseHTTPRequestHandler):
    server_version = "SnapDownloader"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # no request logs kept (no URLs, no IPs)
        pass

    def client_ip(self):
        for h in ("CF-Connecting-IP", "X-Real-IP"):
            v = self.headers.get(h)
            if v:
                return v.strip()
        xf = self.headers.get("X-Forwarded-For")
        if xf:
            return xf.split(",")[0].strip()
        return self.client_address[0]

    def _send(self, code, body, ctype, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in {**SEC_HEADERS, **(extra or {})}.items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj).encode(), "application/json; charset=utf-8", {"Cache-Control": "no-store"})

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        if path == "/healthz":
            return self._send(200, b"ok", "text/plain")
        if path in STATIC:
            fp = os.path.join(HERE, STATIC[path])
            if os.path.isfile(fp):
                with open(fp, "rb") as f:
                    body = f.read()
                ctype = mimetypes.guess_type(fp)[0] or "application/octet-stream"
                if ctype.startswith("text/") or ctype in ("application/javascript", "image/svg+xml"):
                    ctype += "; charset=utf-8"
                return self._send(200, body, ctype, {"Cache-Control": "public, max-age=300"})
        if path.startswith("/media/"):
            return self.media(path[7:], "dl=1" in (urllib.parse.urlsplit(self.path).query or ""))
        return self._send(404, b"Not found", "text/plain; charset=utf-8")

    def do_POST(self):
        path = urllib.parse.urlsplit(self.path).path
        if path != "/api/fetch":
            return self._json(404, {"ok": False, "error": "Not found"})
        try:
            n = min(int(self.headers.get("Content-Length") or 0), 4096)
            data = json.loads(self.rfile.read(n) or b"{}")
            raw = str(data.get("url") or "")[:1000]
        except Exception:
            return self._json(400, {"ok": False, "error": ERR_NOT_SNAP})
        link = find_link(raw.strip())
        if not link:
            return self._json(400, {"ok": False, "error": ERR_NOT_SNAP})
        wait = rate_check(self.client_ip())
        if wait:
            return self._json(429, {"ok": False, "error": wait})
        _sweep()
        try:
            items = lookup(link)
        except SnapError as e:
            return self._json(e.status, {"ok": False, "error": e.msg})
        except Exception:
            return self._json(502, {"ok": False, "error": ERR_CONNECT})
        return self._json(200, {"ok": True, "items": items})

    def media(self, tok, dl):
        with _lock:
            rec = _tokens.get(tok)
        if not rec or rec["exp"] < time.time():
            return self._send(410, "Link expire ho gaya — page pe dobara Download dabao. (Expired, fetch again.)".encode(), "text/plain; charset=utf-8")
        hdrs = {}
        rng = self.headers.get("Range")
        if rng and re.fullmatch(r"bytes=\d*-\d*", rng.strip()):
            hdrs["Range"] = rng.strip()
        try:
            r = _open(rec["url"], hdrs, timeout=60)
        except urllib.error.HTTPError as e:
            if e.code == 416:
                return self._send(416, b"", "text/plain")
            return self._send(502, ERR_BAD.encode(), "text/plain; charset=utf-8")
        except Exception:
            return self._send(502, ERR_CONNECT.encode(), "text/plain; charset=utf-8")
        try:
            self.send_response(r.status)
            ctype = r.headers.get("Content-Type") or ("video/mp4" if rec["video"] else "image/jpeg")
            self.send_header("Content-Type", ctype)
            for k in ("Content-Length", "Content-Range", "Accept-Ranges"):
                if r.headers.get(k):
                    self.send_header(k, r.headers.get(k))
            if not r.headers.get("Accept-Ranges"):
                self.send_header("Accept-Ranges", "bytes")
            self.send_header("Cache-Control", "private, no-store")
            disp = "attachment" if dl else "inline"
            self.send_header("Content-Disposition", f'{disp}; filename="{rec["name"]}"')
            for k, v in SEC_HEADERS.items():
                self.send_header(k, v)
            self.end_headers()
            if self.command == "HEAD":
                return
            while True:
                chunk = r.read(1 << 16)
                if not chunk:
                    break
                self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            r.close()

if __name__ == "__main__":
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), H)
    srv.daemon_threads = True
    print(f"Snap Downloader listening on {PORT}", flush=True)
    srv.serve_forever()
