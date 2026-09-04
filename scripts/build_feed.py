#!/usr/bin/env python3
"""
Privater Podcast-Feed für die SRF-Hörspielreihe "Maloney" (Version 3, Plan B).

Maloney hat keinen offiziellen SRF-Podcast-Feed (HTTP 404). Darum:

1. Show-ID aus der Sendungsseite lesen (<meta name="ais:show:id">).
2. Episodenliste über die öffentliche SRG-SSR Integration-Layer-API holen
   (dieselbe, die der SRF-Webplayer nutzt - kein API-Key). Es werden mehrere
   bekannte Endpunkt-Varianten der Reihe nach probiert; zusätzlich werden
   Episoden-URNs direkt aus dem HTML der Sendungsseite gelesen.
3. Pro Episode die MP3-URL ermitteln: entweder direkt aus dem Listeneintrag
   (podcastHdUrl/podcastSdUrl) oder über die mediaComposition-API.
4. Ergebnis in data/episodes_cache.json cachen (nur neue Folgen werden
   nachgeladen) und als RSS nach docs/<SECRET_TOKEN>/feed.xml schreiben.

Jeder Schritt wird ausführlich geloggt, damit man im Actions-Log sieht,
welcher Weg funktioniert hat.
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from email.utils import format_datetime
from xml.sax.saxutils import escape

SHOW_PAGE_URL = "https://www.srf.ch/audio/maloney"
IL_BASE = "https://il.srgssr.ch/integrationlayer/2.0"
MEDIA_COMPOSITION_URL = (
    IL_BASE + "/mediaComposition/byUrn/urn:srf:audio:{episode_id}.json"
    "?onlyChapters=false&vector=portalplay"
)

SHOW_TITLE = "Maloney"
SHOW_DESCRIPTION = (
    "Die haarsträubenden Fälle des Philip Maloney. In der Hörspiel-Reihe "
    "ermittelt Privatdetektiv Maloney mit Schalk, Charme und unverkennbarer "
    "Raubeinigkeit."
)
SHOW_LINK = SHOW_PAGE_URL
SHOW_LANGUAGE = "de-ch"
SHOW_AUTHOR = "Radio SRF 3"

REQUEST_DELAY = 0.3
MAX_LIST_PAGES = 60          # Sicherheitslimit für Paginierung
FEED_MAX_ITEMS = int(os.environ.get("FEED_MAX_ITEMS", "500"))

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_PATH = os.path.join(REPO_ROOT, "data", "episodes_cache.json")
DOCS_DIR = os.path.join(REPO_ROOT, "docs")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

UUID_RE = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

def http_get(url, accept=None, timeout=30):
    headers = {"User-Agent": USER_AGENT}
    if accept:
        headers["Accept"] = accept
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def http_get_json(url):
    """Gibt (json|None, statusinfo) zurück, wirft nie."""
    try:
        raw = http_get(url, accept="application/json")
        return json.loads(raw.decode("utf-8")), "OK"
    except urllib.error.HTTPError as exc:
        return None, f"HTTP {exc.code}"
    except Exception as exc:  # noqa: BLE001
        return None, f"{type(exc).__name__}: {exc}"


# --------------------------------------------------------------------------
# Schritt 1: Show-ID
# --------------------------------------------------------------------------

def find_show_id(html):
    for pat in (
        r'<meta[^>]+name=["\']ais:show:id["\'][^>]*content=["\'](' + UUID_RE + r')["\']',
        r'<meta[^>]+content=["\'](' + UUID_RE + r')["\'][^>]*name=["\']ais:show:id["\']',
    ):
        m = re.search(pat, html)
        if m:
            return m.group(1)
    marker = html.find("ais:show:id")
    if marker != -1:
        m = re.search(UUID_RE, html[marker:marker + 300])
        if m:
            return m.group(0)
    raise RuntimeError("Show-ID (ais:show:id) nicht auf der Sendungsseite gefunden.")


# --------------------------------------------------------------------------
# Schritt 2: Episodenliste
# --------------------------------------------------------------------------

def list_via_il(show_id):
    """Probiert bekannte Integration-Layer-Endpunkte. Gibt Liste von
    Media-Dicts zurück (mit mindestens 'id')."""
    candidates = [
        f"{IL_BASE}/srf/mediaList/audio/latestByShow/{show_id}.json?pageSize=100",
        f"{IL_BASE}/mediaList/audio/latestByShow/urn:srf:show:radio:{show_id}.json?pageSize=100",
        f"{IL_BASE}/srf/mediaList/audio/latestByShowUrn/urn:srf:show:radio:{show_id}.json?pageSize=100",
        f"{IL_BASE}/srf/mediaList/audio/episodesByShow/{show_id}.json?pageSize=100",
    ]
    for url in candidates:
        print(f"  Versuche Listen-Endpunkt: {url}")
        data, status = http_get_json(url)
        time.sleep(REQUEST_DELAY)
        if not data:
            print(f"    -> {status}")
            continue
        media = data.get("mediaList") or data.get("episodeList") or []
        if not media:
            print(f"    -> Antwort ohne mediaList/episodeList (Keys: {list(data.keys())[:8]})")
            continue
        print(f"    -> OK, {len(media)} Einträge auf Seite 1")
        results = list(media)
        next_url = data.get("next")
        pages = 1
        while next_url and pages < MAX_LIST_PAGES:
            data, status = http_get_json(next_url)
            time.sleep(REQUEST_DELAY)
            if not data:
                print(f"    Paginierung abgebrochen: {status}")
                break
            page_media = data.get("mediaList") or data.get("episodeList") or []
            results.extend(page_media)
            next_url = data.get("next")
            pages += 1
        print(f"    Total über {pages} Seite(n): {len(results)} Einträge")
        return results
    return []


def list_via_html(html):
    """Fallback: Episoden-UUIDs direkt aus dem Seiten-HTML ziehen."""
    ids = []
    seen = set()
    for m in re.finditer(r"urn:srf:(?:ais:)?audio:(" + UUID_RE + r")", html):
        if m.group(1) not in seen:
            seen.add(m.group(1))
            ids.append(m.group(1))
    print(f"  HTML-Fallback: {len(ids)} Episoden-URNs im Seitenquelltext gefunden")
    return [{"id": i} for i in ids]


# --------------------------------------------------------------------------
# Schritt 3: Episodendetails
# --------------------------------------------------------------------------

def pick_audio_from_resources(resource_list):
    if not resource_list:
        return None, None
    for res in resource_list:
        url = res.get("url") or ""
        fmt = (res.get("format") or "").upper()
        if fmt == "MP3" or url.lower().split("?")[0].endswith(".mp3"):
            return url, res
    for res in resource_list:
        if (res.get("protocol") or "").upper() in ("HTTP", "HTTPS", "PROGRESSIVE"):
            return res.get("url"), res
    return resource_list[0].get("url"), resource_list[0]


def details_from_list_entry(entry):
    """Wenn der Listeneintrag schon alles enthält, brauchen wir keinen 2. Request."""
    audio = entry.get("podcastHdUrl") or entry.get("podcastSdUrl")
    if not audio:
        return None
    return {
        "id": entry.get("id"),
        "title": entry.get("title") or SHOW_TITLE,
        "lead": entry.get("lead") or "",
        "description": entry.get("description") or entry.get("lead") or "",
        "date": entry.get("date"),
        "audio_url": audio,
        "audio_length_bytes": None,
        "duration_ms": entry.get("duration"),
        "image_url": entry.get("imageUrl"),
    }


def details_via_media_composition(episode_id):
    data, status = http_get_json(MEDIA_COMPOSITION_URL.format(episode_id=episode_id))
    if not data:
        print(f"    mediaComposition {episode_id}: {status}")
        return None
    try:
        chapter = data["chapterList"][0]
        audio_url, res = pick_audio_from_resources(chapter.get("resourceList") or [])
        if not audio_url:
            print(f"    {episode_id}: keine Audio-Ressource")
            return None
        return {
            "id": episode_id,
            "title": chapter.get("title") or SHOW_TITLE,
            "lead": chapter.get("lead") or "",
            "description": chapter.get("description") or chapter.get("lead") or "",
            "date": chapter.get("date"),
            "audio_url": audio_url,
            "audio_length_bytes": (res or {}).get("byteLength"),
            "duration_ms": chapter.get("duration"),
            "image_url": (data.get("episode") or {}).get("imageUrl") or chapter.get("imageUrl"),
        }
    except (KeyError, IndexError, TypeError) as exc:
        print(f"    {episode_id}: unerwartete Struktur ({exc})")
        return None


# --------------------------------------------------------------------------
# Cache
# --------------------------------------------------------------------------

def load_cache():
    if os.path.exists(CACHE_PATH):
        with open(CACHE_PATH, "r", encoding="utf-8") as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                return {}
    return {}


def save_cache(cache):
    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2, sort_keys=True)


# --------------------------------------------------------------------------
# RSS
# --------------------------------------------------------------------------

def iso_to_rfc2822(iso):
    try:
        return format_datetime(datetime.fromisoformat(iso.replace("Z", "+00:00")))
    except Exception:  # noqa: BLE001
        return format_datetime(datetime.now(timezone.utc))


def ms_to_hms(ms):
    if not ms:
        return None
    s = int(ms) // 1000
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def build_rss(cache, feed_self_url):
    eps = sorted(cache.values(), key=lambda e: e.get("date") or "", reverse=True)[:FEED_MAX_ITEMS]
    cover = next((e["image_url"] for e in eps if e.get("image_url")), "")

    items = []
    for ep in eps:
        dur = ms_to_hms(ep.get("duration_ms"))
        img = ep.get("image_url") or cover
        items.append(f"""
    <item>
      <title>{escape(ep.get('title') or SHOW_TITLE)}</title>
      <link>{escape(SHOW_LINK)}</link>
      <description>{escape(ep.get('description') or '')}</description>
      <itunes:subtitle>{escape(ep.get('lead') or '')}</itunes:subtitle>
      <enclosure url="{escape(ep['audio_url'])}" length="{ep.get('audio_length_bytes') or 0}" type="audio/mpeg"/>
      <guid isPermaLink="false">{escape(ep['id'])}</guid>
      <pubDate>{iso_to_rfc2822(ep.get('date') or '')}</pubDate>
      <itunes:author>{escape(SHOW_AUTHOR)}</itunes:author>
      {f'<itunes:duration>{dur}</itunes:duration>' if dur else ''}
      {f'<itunes:image href="{escape(img)}"/>' if img else ''}
      <itunes:explicit>false</itunes:explicit>
    </item>""")

    cover_xml = ""
    if cover:
        cover_xml = f"""
    <image><url>{escape(cover)}</url><title>{escape(SHOW_TITLE)}</title><link>{escape(SHOW_LINK)}</link></image>
    <itunes:image href="{escape(cover)}"/>"""

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd" xmlns:atom="http://www.w3.org/2005/Atom">
  <channel>
    <title>{escape(SHOW_TITLE)}</title>
    <description>{escape(SHOW_DESCRIPTION)}</description>
    <link>{escape(SHOW_LINK)}</link>
    <language>{SHOW_LANGUAGE}</language>
    <itunes:author>{escape(SHOW_AUTHOR)}</itunes:author>
    <itunes:summary>{escape(SHOW_DESCRIPTION)}</itunes:summary>
    <itunes:explicit>false</itunes:explicit>
    <itunes:category text="Fiction"/>
    {f'<atom:link href="{escape(feed_self_url)}" rel="self" type="application/rss+xml"/>' if feed_self_url else ''}{cover_xml}
{''.join(items)}
  </channel>
</rss>
"""


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main():
    token = os.environ.get("FEED_SECRET_TOKEN")
    if not token:
        print("FEHLER: FEED_SECRET_TOKEN nicht gesetzt.", file=sys.stderr)
        sys.exit(1)
    base = os.environ.get("PAGES_BASE_URL", "").rstrip("/")
    feed_self_url = f"{base}/{token}/feed.xml" if base else ""
    feed_dir = os.path.join(DOCS_DIR, token)
    feed_path = os.path.join(feed_dir, "feed.xml")

    cache = load_cache()
    print(f"Cache geladen: {len(cache)} Episoden bekannt")

    print(f"Lade Sendungsseite: {SHOW_PAGE_URL}")
    html = http_get(SHOW_PAGE_URL, accept="text/html").decode("utf-8", errors="replace")
    show_id = find_show_id(html)
    print(f"Show-ID: {show_id}")

    print("Ermittle Episodenliste ...")
    entries = list_via_il(show_id)
    if not entries:
        entries = list_via_html(html)
    if not entries:
        print("FEHLER: Keine Episoden über keinen Weg gefunden.", file=sys.stderr)
        if os.path.exists(feed_path):
            print("Bestehender Feed bleibt unverändert online.", file=sys.stderr)
            sys.exit(0)
        sys.exit(1)

    new = 0
    for entry in entries:
        eid = entry.get("id")
        if not eid:
            urn = entry.get("urn") or ""
            m = re.search(UUID_RE, urn)
            eid = m.group(0) if m else None
        if not eid or eid in cache:
            continue
        det = details_from_list_entry(entry) or details_via_media_composition(eid)
        time.sleep(REQUEST_DELAY)
        if det:
            det["id"] = eid
            cache[eid] = det
            new += 1
            print(f"  Neu: {det['title']}")

    print(f"{new} neue Episode(n). Cache total: {len(cache)}")
    save_cache(cache)

    if not cache:
        print("FEHLER: Cache leer, kein Feed geschrieben.", file=sys.stderr)
        sys.exit(1)

    os.makedirs(feed_dir, exist_ok=True)
    with open(feed_path, "w", encoding="utf-8") as f:
        f.write(build_rss(cache, feed_self_url))
    print(f"Feed geschrieben: {feed_path} ({min(len(cache), FEED_MAX_ITEMS)} Episoden)")


if __name__ == "__main__":
    main()
