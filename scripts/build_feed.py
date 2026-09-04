#!/usr/bin/env python3
"""
Privater Podcast-Feed für die SRF-Hörspielreihe "Maloney" (Version 7 - final).

Datenquelle
-----------
SRF betreibt eine öffentliche, unauthentifizierte JSON-API, die auch der
eigene Webplayer benutzt, um die "Alle Folgen"-Liste nachzuladen:

    https://www.srf.ch/aron/api/audio/shows/<SHOW-ID>/latestEpisodes?page=<N>

Diese liefert paginiert (neueste zuerst) Titel, Kurzbeschreibung, komplette
Besetzungsliste, Sendedatum, Laufzeit und die interne Asset-UUID jeder
Folge. Für die echte, abspielbare MP3-URL wird pro neuer Folge zusätzlich
die ebenfalls öffentliche SRG-SSR "mediaComposition"-API abgefragt
(dieselbe, die für Play-SRF-Widgets genutzt wird).

Ablauf
------
1. Show-ID (z. B. "A00361") aus dem Meta-Tag der Sendungsseite lesen -
   damit ist das Script nicht auf eine hartcodierte ID angewiesen.
2. Seite für Seite durch latestEpisodes blättern (neueste zuerst). Sobald
   eine Seite ausschliesslich bereits bekannte Folgen enthält, wird die
   Paginierung abgebrochen (inkrementelle Läufe sind dadurch sehr schnell).
   Beim allerersten Lauf (leerer Cache) wird bis zum Ende des Archivs
   geblättert (Sicherheitslimit siehe MAX_PAGES).
3. Pro neuer Folge die echte MP3-URL über mediaComposition holen.
4. Ergebnis in data/episodes_cache.json cachen und als RSS nach
   docs/<SECRET_TOKEN>/feed.xml schreiben.
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
LATEST_EPISODES_URL_TEMPLATE = (
    "https://www.srf.ch/aron/api/audio/shows/{show_id}/latestEpisodes?page={page}"
)
MEDIA_COMPOSITION_URL = (
    "https://il.srgssr.ch/integrationlayer/2.0/mediaComposition/byUrn/"
    "urn:srf:audio:{uid}.json?onlyChapters=false&vector=portalplay"
)

# Falls die Show-ID nicht dynamisch gefunden wird (z. B. Seitenaufbau
# geändert): bekannter Fallback-Wert für Maloney.
FALLBACK_SHOW_ID = "A00361"

SHOW_TITLE = "Maloney"
SHOW_DESCRIPTION = (
    "Die haarsträubenden Fälle des Philip Maloney. In der Hörspiel-Reihe "
    "ermittelt Privatdetektiv Maloney mit Schalk, Charme und unverkennbarer "
    "Raubeinigkeit."
)
SHOW_LANGUAGE = "de-ch"
SHOW_AUTHOR = "Radio SRF 3"

REQUEST_DELAY = 0.3
MAX_PAGES_FIRST_RUN = 20     # 20 Seiten à ~20 Folgen deckt die 1-Jahres-Verfügbarkeit ab
MAX_PAGES_INCREMENTAL = 5    # normaler Lauf: nur die neusten Seiten prüfen
FEED_MAX_ITEMS = int(os.environ.get("FEED_MAX_ITEMS", "500"))

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_PATH = os.path.join(REPO_ROOT, "data", "episodes_cache.json")
DOCS_DIR = os.path.join(REPO_ROOT, "docs")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


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


def http_get_text(url):
    try:
        return http_get(url, accept="text/html").decode("utf-8", errors="replace"), "OK"
    except urllib.error.HTTPError as exc:
        return None, f"HTTP {exc.code}"
    except Exception as exc:  # noqa: BLE001
        return None, f"{type(exc).__name__}: {exc}"


def http_get_json(url):
    try:
        return json.loads(http_get(url, accept="application/json").decode("utf-8")), "OK"
    except urllib.error.HTTPError as exc:
        return None, f"HTTP {exc.code}"
    except Exception as exc:  # noqa: BLE001
        return None, f"{type(exc).__name__}: {exc}"


# --------------------------------------------------------------------------
# Show-ID
# --------------------------------------------------------------------------

def find_show_id():
    html, status = http_get_text(SHOW_PAGE_URL)
    if not html:
        print(f"  Sendungsseite nicht erreichbar ({status}), nutze Fallback-ID")
        return FALLBACK_SHOW_ID
    for pat in (
        r'<meta[^>]+name=["\']srf:content:id["\'][^>]*content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]*name=["\']srf:content:id["\']',
    ):
        m = re.search(pat, html)
        if m:
            return m.group(1)
    print("  Show-ID nicht im HTML gefunden, nutze Fallback-ID")
    return FALLBACK_SHOW_ID


# --------------------------------------------------------------------------
# Episodendetails über die öffentliche SRG-Media-API (echte MP3-URL)
# --------------------------------------------------------------------------

def pick_audio_resource(resource_list):
    if not resource_list:
        return None, None
    for res in resource_list:
        url = res.get("url") or ""
        if (res.get("format") or "").upper() == "MP3" or url.lower().split("?")[0].endswith(".mp3"):
            return url, res
    for res in resource_list:
        if (res.get("protocol") or "").upper() in ("HTTP", "HTTPS", "PROGRESSIVE"):
            return res.get("url"), res
    return resource_list[0].get("url"), resource_list[0]


def http_head_content_length(url):
    """Ermittelt die Dateigrösse per HTTP HEAD (Content-Length-Header),
    falls die mediaComposition-Antwort selbst keine Grösse liefert."""
    try:
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=20) as resp:
            length = resp.headers.get("Content-Length")
            return int(length) if length else None
    except Exception as exc:  # noqa: BLE001
        print(f"    HEAD-Request fehlgeschlagen für {url}: {exc}")
        return None


def fetch_audio_url(uid):
    data, status = http_get_json(MEDIA_COMPOSITION_URL.format(uid=uid))
    if not data:
        print(f"    mediaComposition {uid}: {status}")
        return None, None, None
    try:
        chapter = data["chapterList"][0]
        block_reason = chapter.get("blockReason")
        if block_reason:
            return None, None, block_reason
        audio_url, res = pick_audio_resource(chapter.get("resourceList") or [])
        if not audio_url:
            return None, None, "NO_RESOURCE"
        length_bytes = (res or {}).get("byteLength")
        if not length_bytes:
            length_bytes = http_head_content_length(audio_url)
        return audio_url, length_bytes, None
    except (KeyError, IndexError, TypeError) as exc:
        print(f"    {uid}: unerwartete Struktur ({exc})")
        return None, None, "PARSE_ERROR"


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


def episodes_only(cache):
    return {
        k: v for k, v in cache.items()
        if k != "_meta" and not v.get("blocked")
    }


def purge_future_dated(cache):
    """Räumt Reste einer früheren Skriptversion auf (Folgen, die vor ihrer
    Ausstrahlung fälschlich übernommen wurden)."""
    now_iso = datetime.now(timezone.utc).isoformat()
    stale = [k for k, v in episodes_only(cache).items() if (v.get("date") or "") > now_iso]
    for k in stale:
        print(f"  Entferne verfrühten Cache-Eintrag: {cache[k].get('title')} ({cache[k].get('date')})")
        del cache[k]
    return len(stale)


# --------------------------------------------------------------------------
# Sync über latestEpisodes
# --------------------------------------------------------------------------

def sync_episodes(cache, show_id):
    is_first_run = len(episodes_only(cache)) == 0
    max_pages = MAX_PAGES_FIRST_RUN if is_first_run else MAX_PAGES_INCREMENTAL
    print(f"Synchronisiere über latestEpisodes (Show-ID {show_id}), "
          f"max. {max_pages} Seite(n) in diesem Lauf ...")

    new_count = 0
    page = 1
    while page <= max_pages:
        url = LATEST_EPISODES_URL_TEMPLATE.format(show_id=show_id, page=page)
        data, status = http_get_json(url)
        time.sleep(REQUEST_DELAY)
        if not data:
            print(f"  Seite {page}: {status} - Ende der Liste erreicht")
            break
        if not isinstance(data, list) or not data:
            print(f"  Seite {page}: leer - Ende der Liste erreicht")
            break

        page_new = 0
        for entry in data:
            uid = entry.get("assetId")
            if not uid:
                m = re.search(r"[0-9a-fA-F-]{36}$", entry.get("assetUrn") or "")
                uid = m.group(0) if m else None
            if not uid or uid in cache:
                continue

            audio_url, length_bytes, block_reason = fetch_audio_url(uid)
            time.sleep(REQUEST_DELAY)
            if block_reason:
                cache[uid] = {"id": uid, "title": entry.get("title"), "blocked": True, "reason": block_reason}
                print(f"    Gesperrt ({block_reason}, dauerhaft übersprungen): {entry.get('title')}")
                continue
            if not audio_url:
                continue

            cache[uid] = {
                "id": uid,
                "title": entry.get("title") or SHOW_TITLE,
                "lead": entry.get("lead") or "",
                "description": entry.get("text") or entry.get("lead") or "",
                "date": entry.get("date") or entry.get("publishedAt"),
                "audio_url": audio_url,
                "audio_length_bytes": length_bytes,
                "duration_ms": entry.get("durationMs"),
                "image_url": entry.get("squareImageUrl") or entry.get("bannerImageUrl"),
            }
            new_count += 1
            page_new += 1
            print(f"    Neu: {entry.get('title')} ({entry.get('date')})")

        print(f"  Seite {page}: {len(data)} Eintrag/Einträge, davon {page_new} neu")

        if not is_first_run and page_new == 0:
            print("  Keine neuen Folgen mehr auf dieser Seite - Abgleich fertig.")
            break

        page += 1

    print(f"Sync fertig: {new_count} neue Episode(n). Insgesamt im Cache: {len(episodes_only(cache))}.")


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
    eps = sorted(episodes_only(cache).values(), key=lambda e: e.get("date") or "", reverse=True)
    eps = eps[:FEED_MAX_ITEMS]
    cover = next((e["image_url"] for e in eps if e.get("image_url")), "")

    items = []
    for ep in eps:
        dur = ms_to_hms(ep.get("duration_ms"))
        img = ep.get("image_url") or cover
        items.append(f"""
    <item>
      <title>{escape(ep.get('title') or SHOW_TITLE)}</title>
      <link>{escape(SHOW_PAGE_URL)}</link>
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
    <image><url>{escape(cover)}</url><title>{escape(SHOW_TITLE)}</title><link>{escape(SHOW_PAGE_URL)}</link></image>
    <itunes:image href="{escape(cover)}"/>"""

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd" xmlns:atom="http://www.w3.org/2005/Atom">
  <channel>
    <title>{escape(SHOW_TITLE)}</title>
    <description>{escape(SHOW_DESCRIPTION)}</description>
    <link>{escape(SHOW_PAGE_URL)}</link>
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


def backfill_missing_lengths(cache, max_requests=60):
    """Ergänzt bei bereits gecachten Folgen die fehlende Dateigrösse
    (Content-Length), z. B. für Folgen, die vor dieser Skript-Version
    hinzugefügt wurden."""
    todo = [
        (uid, ep) for uid, ep in episodes_only(cache).items()
        if not ep.get("audio_length_bytes") and ep.get("audio_url")
    ]
    if not todo:
        return 0
    print(f"Ergänze Dateigrösse für {min(len(todo), max_requests)} von {len(todo)} Folge(n) ohne Längenangabe ...")
    done = 0
    for uid, ep in todo[:max_requests]:
        length = http_head_content_length(ep["audio_url"])
        time.sleep(REQUEST_DELAY)
        if length:
            cache[uid]["audio_length_bytes"] = length
            done += 1
    print(f"  {done} Dateigrösse(n) ergänzt.")
    return done


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
    print(f"Cache geladen: {len(episodes_only(cache))} Episoden bekannt")

    removed = purge_future_dated(cache)
    if removed:
        print(f"{removed} verfrühte(n) Eintrag/Einträge bereinigt")

    show_id = find_show_id()
    print(f"Show-ID: {show_id}")

    sync_episodes(cache, show_id)
    backfill_missing_lengths(cache)
    save_cache(cache)

    eps = episodes_only(cache)
    if not eps:
        print("FEHLER: Cache leer, kein Feed geschrieben.", file=sys.stderr)
        sys.exit(1)

    os.makedirs(feed_dir, exist_ok=True)
    with open(feed_path, "w", encoding="utf-8") as f:
        f.write(build_rss(cache, feed_self_url))
    print(f"Feed geschrieben: {feed_path} ({min(len(eps), FEED_MAX_ITEMS)} von {len(eps)} Episoden)")


if __name__ == "__main__":
    main()
