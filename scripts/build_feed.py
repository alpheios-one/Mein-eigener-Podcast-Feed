#!/usr/bin/env python3
"""
Privater Podcast-Feed für die SRF-Hörspielreihe "Maloney" (Version 4).

Hintergrund
-----------
Die Sendungsseite https://www.srf.ch/audio/maloney zeigt zwei Bereiche:
  - "Vorschau kommender Folgen": noch nicht ausgestrahlt, wird per
    JavaScript geladen, ist aber teilweise (mit temporären UUIDs) bereits
    im rohen HTML eingebettet - nützlich nur, um TITEL und AUDI-CODE
    kommender Folgen im Voraus zu kennen (nicht für den Feed selbst, da
    die MP3-Datei zum Ausstrahlungszeitpunkt noch nicht uebre online ist).
  - "Alle Folgen" (das eigentliche Archiv, ca. 400 Folgen): jede Folge hat
    eine eigene, statische (nicht JS-abhängige) Seite unter
    /audio/maloney/<slug>?id=AUDI<Datum>_NR_<Nr>, die u. a. einen Verweis
    auf die 10 jeweils vorangehenden (älteren) Folgen enthält
    ("Mehr von «Maloney»"). Über diese Verkettung lässt sich rückwärts
    durchs ganze Archiv wandern.

Strategie dieses Scripts
------------------------
1. Aus einem bekannten "Startpunkt" (gespeichert in data/episodes_cache.json
   unter "_meta.newest_page", oder beim allerersten Lauf ein fest
   hinterlegter Bootstrap-Startpunkt) rückwärts durch die "Mehr von"-Kette
   wandern und alle noch unbekannten Folgen einsammeln (begrenzt pro Lauf,
   damit die Laufzeit nicht ausufert - läuft über mehrere Tage vollständig
   durch).
2. Für neue, noch nicht im Cache befindliche kommende Folgen (aus der
   "Vorschau", deren Sendedatum inzwischen in der Vergangenheit liegt) wird
   die Archiv-URL aus dem Titel vorhergesagt (deutsche Slug-Regeln) und
   geprüft, ob die Seite existiert. Klappt das nicht, wird die Folge
   spätestens dann gefunden, wenn eine spätere Folge in ihrer "Mehr
   von"-Liste auf sie zurückverweist (selbstheilend).
3. Pro gefundener Folge liefert die öffentliche, unauthentifizierte
   SRG-SSR "mediaComposition"-API (dieselbe, die der SRF-Webplayer nutzt)
   Titel, Beschreibung, Datum, echte MP3-URL und Bild.
4. Ergebnis wird in data/episodes_cache.json zwischengespeichert und als
   RSS nach docs/<SECRET_TOKEN>/feed.xml geschrieben.
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

SHOW_BASE_URL = "https://www.srf.ch/audio/maloney"
MEDIA_COMPOSITION_URL = (
    "https://il.srgssr.ch/integrationlayer/2.0/mediaComposition/byUrn/"
    "urn:srf:audio:{uid}.json?onlyChapters=false&vector=portalplay"
)

# Bootstrap-Startpunkt für den allerersten Lauf (leerer Cache). Kann bei
# Bedarf durch eine aktuellere Folgen-URL ersetzt werden - danach speichert
# das Script seinen eigenen Fortschritt selbst und braucht das nicht mehr.
BOOTSTRAP_SLUG = "jugendsuenden"
BOOTSTRAP_AUDI_ID = "AUDI20260830_NR_0007"

SHOW_TITLE = "Maloney"
SHOW_DESCRIPTION = (
    "Die haarsträubenden Fälle des Philip Maloney. In der Hörspiel-Reihe "
    "ermittelt Privatdetektiv Maloney mit Schalk, Charme und unverkennbarer "
    "Raubeinigkeit."
)
SHOW_LANGUAGE = "de-ch"
SHOW_AUTHOR = "Radio SRF 3"

REQUEST_DELAY = 0.3
MAX_PAGE_FETCHES_PER_RUN = 80   # Sicherheitslimit gegen ausufernde Laufzeit
FEED_MAX_ITEMS = int(os.environ.get("FEED_MAX_ITEMS", "500"))

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_PATH = os.path.join(REPO_ROOT, "data", "episodes_cache.json")
DOCS_DIR = os.path.join(REPO_ROOT, "docs")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

UUID_RE = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
AUDI_RE = r"AUDI\d{8}_NR_\d+"
EPISODE_LINK_RE = re.compile(r"/audio/maloney/([a-z0-9\-]+)\?id=(" + AUDI_RE + r")")
PREVIEW_URN_RE = re.compile(r"urn:srf:(?:ais:)?audio:(" + UUID_RE + r")")

UMLAUT_MAP = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})


def slugify(title: str) -> str:
    s = title.lower().translate(UMLAUT_MAP)
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


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
    """(text|None, status)."""
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
# Episodenseite parsen
# --------------------------------------------------------------------------

def find_meta(html, name):
    for pat in (
        rf'<meta[^>]+name=["\']{re.escape(name)}["\'][^>]*content=["\']([^"\']*)["\']',
        rf'<meta[^>]+content=["\']([^"\']*)["\'][^>]*name=["\']{re.escape(name)}["\']',
    ):
        m = re.search(pat, html)
        if m:
            return m.group(1)
    return None


def fetch_episode_page(slug, audi_id):
    """Liefert (uuid, neighbor_list) oder (None, []) falls Seite nicht existiert."""
    url = f"{SHOW_BASE_URL}/{slug}?id={audi_id}"
    html, status = http_get_text(url)
    if not html:
        print(f"    Seite nicht erreichbar ({status}): {url}")
        return None, []
    uid = find_meta(html, "pdp:ais:id")
    if not uid or not re.fullmatch(UUID_RE, uid):
        print(f"    Keine gültige UUID (pdp:ais:id) auf {url} gefunden")
        return None, []
    neighbors = []
    seen = set()
    for m in EPISODE_LINK_RE.finditer(html):
        key = (m.group(1), m.group(2))
        if key not in seen and key != (slug, audi_id):
            seen.add(key)
            neighbors.append(key)
    return uid, neighbors


# --------------------------------------------------------------------------
# Episodendetails über die öffentliche SRG-Media-API
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


def fetch_episode_details(uid):
    data, status = http_get_json(MEDIA_COMPOSITION_URL.format(uid=uid))
    if not data:
        print(f"    mediaComposition {uid}: {status}")
        return None
    try:
        chapter = data["chapterList"][0]
        audio_url, res = pick_audio_resource(chapter.get("resourceList") or [])
        if not audio_url:
            print(f"    {uid}: keine Audio-Ressource")
            return None
        return {
            "id": uid,
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
        print(f"    {uid}: unerwartete Struktur ({exc})")
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


def episodes_only(cache):
    return {k: v for k, v in cache.items() if k != "_meta"}


def purge_future_dated(cache):
    """Entfernt Cache-Einträge mit einem Sendedatum in der Zukunft - z. B.
    Reste aus einer früheren Skriptversion, die 'Vorschau'-Folgen
    faelschlich uebernommen hat, bevor sie tatsaechlich ausgestrahlt
    wurden."""
    now_iso = datetime.now(timezone.utc).isoformat()
    stale = [k for k, v in episodes_only(cache).items() if (v.get("date") or "") > now_iso]
    for k in stale:
        print(f"  Entferne verfrühten Cache-Eintrag: {cache[k].get('title')} ({cache[k].get('date')})")
        del cache[k]
    return len(stale)


# --------------------------------------------------------------------------
# Crawl
# --------------------------------------------------------------------------

def discover_forward(cache):
    """Findet neu ausgestrahlte Folgen, die noch nicht im Archiv-Crawl
    aufgetaucht sind: scannt die 'Vorschau kommender Folgen' (teilweise
    schon im rohen HTML der Sendungsseite eingebettet) und übernimmt jene
    Einträge direkt, deren Sendedatum inzwischen in der Vergangenheit
    liegt. Aktualisiert auch den gespeicherten 'newest_page'-Ankerpunkt,
    damit der Rückwärts-Crawl beim nächsten Lauf von dort aus die Brücke
    zum Archiv schlagen kann."""
    html, status = http_get_text(SHOW_BASE_URL)
    if not html:
        print(f"  Vorschau-Scan: Sendungsseite nicht erreichbar ({status})")
        return

    known_uuids = set(episodes_only(cache).keys())
    candidates = [u for u in dict.fromkeys(PREVIEW_URN_RE.findall(html)) if u not in known_uuids]
    if not candidates:
        print("  Vorschau-Scan: keine neuen Kandidaten")
        return
    print(f"  Vorschau-Scan: {len(candidates)} neue Kandidat(en)")

    meta = cache.setdefault("_meta", {})
    newest_date = meta.get("newest_date")
    now_iso = datetime.now(timezone.utc).isoformat()

    for uid in candidates:
        details = fetch_episode_details(uid)
        time.sleep(REQUEST_DELAY)
        if not details:
            continue
        ep_date = details.get("date") or ""
        if ep_date > now_iso:
            print(f"    {details['title']}: erst am {ep_date} - noch nicht übernommen")
            continue
        cache[uid] = details
        known_uuids.add(uid)
        print(f"    Neu (Vorschau->aktuell): {details['title']} ({ep_date})")
        if not newest_date or ep_date > newest_date:
            newest_date = ep_date
            audi_match = re.search(AUDI_RE, details.get("audio_url") or "")
            if audi_match:
                meta["newest_page"] = {
                    "slug": slugify(details["title"]),
                    "audi_id": audi_match.group(0),
                }
            meta["newest_date"] = newest_date


def crawl(cache):
    fetched_this_run = 0
    known_uuids = set(episodes_only(cache).keys())
    known_pages = set()  # (slug, audi_id) bereits besucht in diesem Lauf

    meta = cache.setdefault("_meta", {})
    queue = []

    newest = meta.get("newest_page")
    if newest:
        queue.append((newest["slug"], newest["audi_id"]))
    else:
        queue.append((BOOTSTRAP_SLUG, BOOTSTRAP_AUDI_ID))
        print(f"Kein gespeicherter Startpunkt - nutze Bootstrap: {BOOTSTRAP_SLUG}")

    newest_date = meta.get("newest_date")

    while queue and fetched_this_run < MAX_PAGE_FETCHES_PER_RUN:
        slug, audi_id = queue.pop(0)
        if (slug, audi_id) in known_pages:
            continue
        known_pages.add((slug, audi_id))

        print(f"  Seite: /{slug}?id={audi_id}")
        uid, neighbors = fetch_episode_page(slug, audi_id)
        fetched_this_run += 1
        time.sleep(REQUEST_DELAY)

        if uid and uid not in known_uuids:
            details = fetch_episode_details(uid)
            time.sleep(REQUEST_DELAY)
            if details:
                cache[uid] = details
                known_uuids.add(uid)
                print(f"    Neu: {details['title']} ({details.get('date')})")
                if not newest_date or (details.get("date") or "") > newest_date:
                    newest_date = details.get("date")
                    meta["newest_page"] = {"slug": slug, "audi_id": audi_id}
                    meta["newest_date"] = newest_date

        for n_slug, n_audi in neighbors:
            if (n_slug, n_audi) not in known_pages:
                queue.append((n_slug, n_audi))

    remaining = len(queue)
    print(f"Crawl-Lauf beendet: {fetched_this_run} Seite(n) abgerufen, "
          f"{remaining} noch offen (nächster Lauf macht weiter).")


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
      <link>{escape(SHOW_BASE_URL)}</link>
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
    <image><url>{escape(cover)}</url><title>{escape(SHOW_TITLE)}</title><link>{escape(SHOW_BASE_URL)}</link></image>
    <itunes:image href="{escape(cover)}"/>"""

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd" xmlns:atom="http://www.w3.org/2005/Atom">
  <channel>
    <title>{escape(SHOW_TITLE)}</title>
    <description>{escape(SHOW_DESCRIPTION)}</description>
    <link>{escape(SHOW_BASE_URL)}</link>
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
    print(f"Cache geladen: {len(episodes_only(cache))} Episoden bekannt")

    removed = purge_future_dated(cache)
    if removed:
        print(f"{removed} verfrühte(n) Eintrag/Einträge bereinigt")

    print("Suche neu ausgestrahlte Folgen (Vorschau-Abgleich) ...")
    discover_forward(cache)

    print("Durchsuche Archiv rückwärts (Mehr-von-Kette) ...")
    crawl(cache)
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
