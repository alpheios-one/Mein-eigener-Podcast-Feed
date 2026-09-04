#!/usr/bin/env python3
"""
Baut einen privaten Podcast-RSS-Feed für die SRF-Hörspielreihe "Maloney".

Funktionsweise
--------------
1. SRF liefert unter https://www.srf.ch/audio/episodes/10000183/{limit}/{offset}
   eine paginierte HTML-Liste der Maloney-Folgen (10000183 = interne Sendungs-ID
   von Maloney bei SRF). Daraus extrahieren wir die Episoden-IDs.
2. Für jede neue Episoden-ID holen wir die Detailmetadaten (Titel, Beschreibung,
   Datum, echte Audio-URL, Bild) über die öffentliche, unauthentifizierte
   SRG-SSR "mediaComposition"-API:
   https://il.srgssr.ch/integrationlayer/2.0/mediaComposition/byUrn/urn:srf:audio:{id}.json
   Das ist dieselbe API, die der SRF-eigene Webplayer benutzt - kein API-Key nötig.
3. Bereits bekannte Episoden werden in data/episodes_cache.json zwischengespeichert,
   damit nicht bei jedem Lauf alle ~400 Folgen neu abgefragt werden müssen.
4. Aus dem Cache wird eine Standard-Podcast-RSS-Datei (RSS 2.0 + iTunes-Tags)
   erzeugt und unter docs/<SECRET_TOKEN>/feed.xml abgelegt.

Der SECRET_TOKEN im Pfad ist der "Passwortschutz": GitHub Pages kann keine
echte Serverauthentifizierung (HTTP Basic Auth) prüfen, da es rein statisches
Hosting ist. Die gängige Lösung für private Podcast-Feeds auf statischem
Hosting ist daher eine lange, geheime, nicht erratbare URL - genau das,
was Podcast-Apps unter der Haube auch bei "echtem" passwortgeschütztem
Feeds letztlich abspeichern (die URL selbst ist das Geheimnis).
Diese URL nie öffentlich verlinken oder posten.
"""

import json
import os
import re
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from email.utils import format_datetime
from xml.sax.saxutils import escape

# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------

SHOW_INTERNAL_ID = "10000183"          # interne SRF-ID für die Sendung Maloney
PAGE_SIZE = 10
BASE_URL = "https://www.srf.ch"
EPISODES_LIST_URL = f"{BASE_URL}/audio/episodes/{SHOW_INTERNAL_ID}/{PAGE_SIZE}/{{offset}}"
MEDIA_COMPOSITION_URL = (
    "https://il.srgssr.ch/integrationlayer/2.0/mediaComposition/byUrn/"
    "urn:srf:audio:{episode_id}.json?onlyChapters=false&vector=portalplay"
)

SHOW_TITLE = "Maloney"
SHOW_DESCRIPTION = (
    "Die haarsträubenden Fälle des Philip Maloney. In der Hörspiel-Reihe "
    "ermittelt Privatdetektiv Maloney mit Schalk, Charme und unverkennbarer "
    "Raubeinigkeit."
)
SHOW_LINK = f"{BASE_URL}/audio/maloney"
SHOW_LANGUAGE = "de-ch"
SHOW_AUTHOR = "Radio SRF 3"

# Sicherheitslimits, damit ein einzelner Lauf nicht ausufert
MAX_PAGES_PER_RUN_INCREMENTAL = 6     # normaler Lauf: nur die neusten Seiten prüfen
MAX_PAGES_PER_RUN_FULL = 200          # erster Lauf (leerer Cache): ganzer Katalog
REQUEST_DELAY_SECONDS = 0.4           # kleine Pause zwischen Requests (Höflichkeit)
FEED_MAX_ITEMS = int(os.environ.get("FEED_MAX_ITEMS", "500"))

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_PATH = os.path.join(REPO_ROOT, "data", "episodes_cache.json")
DOCS_DIR = os.path.join(REPO_ROOT, "docs")

USER_AGENT = (
    "Mozilla/5.0 (compatible; maloney-private-feed/1.0; "
    "+https://github.com/) private podcast feed generator for personal use"
)


# ---------------------------------------------------------------------------
# HTTP Hilfsfunktion
# ---------------------------------------------------------------------------

def http_get_json(url: str, retries: int = 3):
    last_error = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=20) as resp:
                raw = resp.read()
            return json.loads(raw.decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - bewusst breit, wir loggen & retryen
            last_error = exc
            time.sleep(1.5 * attempt)
    print(f"WARNUNG: Konnte {url} nicht laden: {last_error}", file=sys.stderr)
    return None


# ---------------------------------------------------------------------------
# Schritt 1: Episoden-IDs auflisten
# ---------------------------------------------------------------------------

ID_PATTERN = re.compile(r"/popupaudioplayer\?id=([0-9a-fA-F-]+)\"")


def fetch_episode_ids_page(offset: int):
    """Gibt die Liste der Episoden-IDs (in Reihenfolge) einer Listenseite zurück."""
    url = EPISODES_LIST_URL.format(offset=offset)
    data = http_get_json(url)
    if not data or "content" not in data:
        return []
    html_fragment = data["content"]
    return ID_PATTERN.findall(html_fragment)


# ---------------------------------------------------------------------------
# Schritt 2: Episodendetails holen
# ---------------------------------------------------------------------------

def pick_audio_resource(resource_list):
    """Bevorzugt eine direkte MP3-Ressource, sonst die erste verfügbare."""
    if not resource_list:
        return None, None
    for res in resource_list:
        fmt = (res.get("format") or "").upper()
        url = res.get("url") or ""
        if fmt == "MP3" or url.lower().endswith(".mp3"):
            return url, res
    first = resource_list[0]
    return first.get("url"), first


def fetch_episode_details(episode_id: str):
    url = MEDIA_COMPOSITION_URL.format(episode_id=episode_id)
    data = http_get_json(url)
    if not data:
        return None
    try:
        chapter = data["chapterList"][0]
        audio_url, resource = pick_audio_resource(chapter.get("resourceList") or [])
        if not audio_url:
            return None

        image_url = (data.get("episode") or {}).get("imageUrl") or chapter.get("imageUrl")
        duration_ms = chapter.get("duration") or resource.get("duration") if resource else None

        return {
            "id": episode_id,
            "title": chapter.get("title") or "Maloney",
            "lead": chapter.get("lead") or "",
            "description": chapter.get("description") or chapter.get("lead") or "",
            "date": chapter.get("date"),  # ISO-8601 Format
            "audio_url": audio_url,
            "audio_length_bytes": resource.get("byteLength") if resource else None,
            "duration_ms": duration_ms,
            "image_url": image_url,
        }
    except (KeyError, IndexError, TypeError) as exc:
        print(f"WARNUNG: Unerwartete Datenstruktur für {episode_id}: {exc}", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

def load_cache():
    if os.path.exists(CACHE_PATH):
        with open(CACHE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_cache(cache: dict):
    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2, sort_keys=True)


# ---------------------------------------------------------------------------
# Hauptlogik: neue Episoden einsammeln
# ---------------------------------------------------------------------------

def collect_episodes(cache: dict):
    is_first_run = len(cache) == 0
    max_pages = MAX_PAGES_PER_RUN_FULL if is_first_run else MAX_PAGES_PER_RUN_INCREMENTAL

    new_count = 0
    offset = 0
    pages_checked = 0

    while pages_checked < max_pages:
        ids = fetch_episode_ids_page(offset)
        pages_checked += 1
        time.sleep(REQUEST_DELAY_SECONDS)

        if not ids:
            break  # keine weiteren Seiten mehr

        found_new_on_page = False
        for episode_id in ids:
            if episode_id in cache:
                continue
            found_new_on_page = True
            details = fetch_episode_details(episode_id)
            time.sleep(REQUEST_DELAY_SECONDS)
            if details:
                cache[episode_id] = details
                new_count += 1
                print(f"Neu: {details['title']} ({episode_id})")

        offset += PAGE_SIZE

        # Im Normalbetrieb (nicht erster Lauf): sobald eine Seite komplett
        # aus bereits bekannten Episoden besteht, gibt es nichts Neues mehr.
        if not is_first_run and not found_new_on_page:
            break

    print(f"Fertig. {new_count} neue Episode(n) gefunden. Insgesamt im Cache: {len(cache)}.")
    return new_count


# ---------------------------------------------------------------------------
# RSS-Feed erzeugen
# ---------------------------------------------------------------------------

def iso_to_rfc2822(iso_date: str) -> str:
    if not iso_date:
        return format_datetime(datetime.now(timezone.utc))
    try:
        dt = datetime.fromisoformat(iso_date.replace("Z", "+00:00"))
        return format_datetime(dt)
    except ValueError:
        return format_datetime(datetime.now(timezone.utc))


def ms_to_hhmmss(duration_ms):
    if not duration_ms:
        return None
    total_seconds = int(duration_ms) // 1000
    h, rem = divmod(total_seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def build_rss(cache: dict, feed_self_url: str) -> str:
    episodes = list(cache.values())
    episodes.sort(key=lambda e: e.get("date") or "", reverse=True)
    episodes = episodes[:FEED_MAX_ITEMS]

    cover_image = next((e["image_url"] for e in episodes if e.get("image_url")), "")

    items_xml = []
    for ep in episodes:
        pub_date = iso_to_rfc2822(ep.get("date"))
        title = escape(ep.get("title") or "Maloney")
        description = escape(ep.get("description") or "")
        lead = escape(ep.get("lead") or "")
        audio_url = escape(ep.get("audio_url") or "")
        length_bytes = ep.get("audio_length_bytes") or 0
        duration_str = ms_to_hhmmss(ep.get("duration_ms"))
        image_url = escape(ep.get("image_url") or cover_image or "")
        guid = escape(ep["id"])

        duration_tag = f"<itunes:duration>{duration_str}</itunes:duration>" if duration_str else ""
        image_tag = f'<itunes:image href="{image_url}"/>' if image_url else ""

        items_xml.append(f"""
    <item>
      <title>{title}</title>
      <link>{escape(SHOW_LINK)}</link>
      <description>{description}</description>
      <itunes:subtitle>{lead}</itunes:subtitle>
      <itunes:summary>{description}</itunes:summary>
      <enclosure url="{audio_url}" length="{length_bytes}" type="audio/mpeg"/>
      <guid isPermaLink="false">{guid}</guid>
      <pubDate>{pub_date}</pubDate>
      <itunes:author>{escape(SHOW_AUTHOR)}</itunes:author>
      {duration_tag}
      {image_tag}
      <itunes:explicit>false</itunes:explicit>
    </item>""")

    channel_image_tag = ""
    if cover_image:
        channel_image_tag = f"""
    <image>
      <url>{escape(cover_image)}</url>
      <title>{escape(SHOW_TITLE)}</title>
      <link>{escape(SHOW_LINK)}</link>
    </image>
    <itunes:image href="{escape(cover_image)}"/>"""

    rss = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"
     xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"
     xmlns:atom="http://www.w3.org/2005/Atom">
  <channel>
    <title>{escape(SHOW_TITLE)}</title>
    <description>{escape(SHOW_DESCRIPTION)}</description>
    <link>{escape(SHOW_LINK)}</link>
    <language>{escape(SHOW_LANGUAGE)}</language>
    <generator>maloney-private-feed</generator>
    <itunes:author>{escape(SHOW_AUTHOR)}</itunes:author>
    <itunes:summary>{escape(SHOW_DESCRIPTION)}</itunes:summary>
    <itunes:explicit>false</itunes:explicit>
    <itunes:category text="Society &amp; Culture"/>
    <atom:link href="{escape(feed_self_url)}" rel="self" type="application/rss+xml"/>{channel_image_tag}
{"".join(items_xml)}
  </channel>
</rss>
"""
    return rss


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    secret_token = os.environ.get("FEED_SECRET_TOKEN")
    if not secret_token:
        print(
            "FEHLER: Umgebungsvariable FEED_SECRET_TOKEN ist nicht gesetzt.\n"
            "Das ist das Geheimnis, das den Feed 'passwortschützt' - "
            "siehe README.md.",
            file=sys.stderr,
        )
        sys.exit(1)

    pages_base_url = os.environ.get("PAGES_BASE_URL", "").rstrip("/")
    feed_self_url = f"{pages_base_url}/{secret_token}/feed.xml" if pages_base_url else ""

    cache = load_cache()
    collect_episodes(cache)
    save_cache(cache)

    rss_xml = build_rss(cache, feed_self_url)

    feed_dir = os.path.join(DOCS_DIR, secret_token)
    os.makedirs(feed_dir, exist_ok=True)
    feed_path = os.path.join(feed_dir, "feed.xml")
    with open(feed_path, "w", encoding="utf-8") as f:
        f.write(rss_xml)

    print(f"Feed geschrieben nach: {feed_path}")


if __name__ == "__main__":
    main()
