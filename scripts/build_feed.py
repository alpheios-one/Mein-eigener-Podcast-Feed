#!/usr/bin/env python3
"""
Baut einen privaten Podcast-RSS-Feed für die SRF-Hörspielreihe "Maloney".

Funktionsweise (Version 2 - vereinfacht)
-----------------------------------------
SRF betreibt für praktisch jede Sendung bereits einen eigenen, offiziellen
Podcast-RSS-Feed unter dem Muster:

    https://www.srf.ch/feed/podcast/sd/<SHOW-UUID>.xml

Die SHOW-UUID steht als <meta name="ais:show:id" content="..."> direkt im
(serverseitig gerenderten, nicht JS-abhängigen) HTML der Sendungsseite
https://www.srf.ch/audio/maloney.

Das Script:
1. Lädt die Sendungsseite und liest die Show-UUID aus dem Meta-Tag.
2. Lädt darüber den offiziellen SRF-Podcast-Feed.
3. Passt nur den <atom:link rel="self">-Verweis an (zeigt neu auf unsere
   eigene, geheime Feed-URL) und schreibt das Ergebnis nach
   docs/<SECRET_TOKEN>/feed.xml.

Kein API-Key nötig, keine hunderten Einzelabfragen mehr. Falls SRF die
Struktur der Sendungsseite oder des Feeds ändert, bricht der Lauf mit einer
klaren Fehlermeldung ab, statt einen leeren/kaputten Feed zu schreiben -
ein vorher funktionierender Feed bleibt dann online erhalten.
"""

import os
import re
import sys
import urllib.request
import urllib.error

SHOW_PAGE_URL = "https://www.srf.ch/audio/maloney"
FEED_URL_TEMPLATE = "https://www.srf.ch/feed/podcast/sd/{show_id}.xml"

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS_DIR = os.path.join(REPO_ROOT, "docs")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 "
    "private-podcast-feed-generator/2.0 (personal, non-commercial use)"
)

# Verschiedene Schreibweisen des Meta-Tags abdecken:
# <meta name="ais:show:id" content="XXXX">
# <meta content="XXXX" name="ais:show:id">
SHOW_ID_PATTERNS = [
    re.compile(
        r'<meta[^>]+name=["\']ais:show:id["\'][^>]*content=["\']'
        r'([0-9a-fA-F-]{36})["\']'
    ),
    re.compile(
        r'<meta[^>]+content=["\']([0-9a-fA-F-]{36})["\'][^>]*'
        r'name=["\']ais:show:id["\']'
    ),
]


def http_get(url: str, accept: str = None) -> bytes:
    headers = {"User-Agent": USER_AGENT}
    if accept:
        headers["Accept"] = accept
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def find_show_id(html: str) -> str:
    for pattern in SHOW_ID_PATTERNS:
        match = pattern.search(html)
        if match:
            return match.group(1)

    # Fallback: robuster gegen unerwartete Attribut-Reihenfolge/Whitespace -
    # einfach die Textstelle "ais:show:id" suchen und in der Nähe nach einer
    # UUID Ausschau halten, statt exakte Tag-Syntax vorauszusetzen.
    marker = html.find("ais:show:id")
    if marker != -1:
        window = html[marker: marker + 200]
        uuid_match = re.search(r"[0-9a-fA-F]{8}-[0-9a-fA-F-]{27}", window)
        if uuid_match:
            return uuid_match.group(0)

    raise RuntimeError(
        "Konnte die Show-ID (Meta-Tag 'ais:show:id') nicht auf "
        f"{SHOW_PAGE_URL} finden. SRF hat vermutlich die Seitenstruktur "
        "geändert."
    )


def rewrite_self_link(xml_text: str, feed_self_url: str) -> str:
    if not feed_self_url:
        return xml_text
    pattern = re.compile(
        r'(<atom:link[^>]*rel=["\']self["\'][^>]*href=["\'])[^"\']*(["\'])'
    )
    new_xml, count = pattern.subn(
        lambda m: m.group(1) + feed_self_url + m.group(2), xml_text, count=1
    )
    return new_xml if count else xml_text


def main():
    secret_token = os.environ.get("FEED_SECRET_TOKEN")
    if not secret_token:
        print(
            "FEHLER: Umgebungsvariable FEED_SECRET_TOKEN ist nicht gesetzt.",
            file=sys.stderr,
        )
        sys.exit(1)

    pages_base_url = os.environ.get("PAGES_BASE_URL", "").rstrip("/")
    feed_self_url = f"{pages_base_url}/{secret_token}/feed.xml" if pages_base_url else ""

    feed_dir = os.path.join(DOCS_DIR, secret_token)
    feed_path = os.path.join(feed_dir, "feed.xml")

    try:
        print(f"Lade Sendungsseite: {SHOW_PAGE_URL}")
        show_html = http_get(SHOW_PAGE_URL, accept="text/html").decode(
            "utf-8", errors="replace"
        )

        show_id = find_show_id(show_html)
        print(f"Gefundene Show-ID: {show_id}")

        feed_url = FEED_URL_TEMPLATE.format(show_id=show_id)
        print(f"Lade offiziellen SRF-Podcast-Feed: {feed_url}")
        feed_bytes = http_get(feed_url, accept="application/rss+xml, application/xml, text/xml")
        feed_text = feed_bytes.decode("utf-8", errors="replace")

        if "<rss" not in feed_text[:2000] and "<?xml" not in feed_text[:200]:
            raise RuntimeError(
                "Antwort von SRF sieht nicht wie ein gültiger RSS-Feed aus "
                f"(erste 200 Zeichen: {feed_text[:200]!r})"
            )

        feed_text = rewrite_self_link(feed_text, feed_self_url)

    except (urllib.error.URLError, urllib.error.HTTPError, RuntimeError) as exc:
        print(f"FEHLER beim Aktualisieren: {exc}", file=sys.stderr)
        if os.path.exists(feed_path):
            print(
                "Bestehender Feed bleibt unverändert online "
                "(alter Stand wird nicht überschrieben).",
                file=sys.stderr,
            )
            sys.exit(0)
        else:
            print("Kein bestehender Feed vorhanden - Lauf schlägt fehl.", file=sys.stderr)
            sys.exit(1)

    os.makedirs(feed_dir, exist_ok=True)
    with open(feed_path, "w", encoding="utf-8") as f:
        f.write(feed_text)

    item_count = feed_text.count("<item>")
    print(f"Feed geschrieben nach: {feed_path} ({item_count} Episoden)")


if __name__ == "__main__":
    main()
