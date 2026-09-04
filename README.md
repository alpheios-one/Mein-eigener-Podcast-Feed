# Maloney – privater Podcast-Feed

Erzeugt automatisch (täglich, via GitHub Actions) einen Podcast-RSS-Feed aus
den Folgen der SRF-Hörspielreihe **Maloney** (https://www.srf.ch/audio/maloney)
und veröffentlicht ihn über GitHub Pages – geschützt durch eine geheime,
nicht erratbare URL.

## Funktionsweise

- `scripts/build_feed.py` holt die Episodenliste über die von SRF selbst
  genutzte, unauthentifizierte Schnittstelle und lädt pro Episode die
  Metadaten (Titel, Beschreibung, Datum, echte MP3-URL, Bild) über die
  öffentliche SRG-SSR "mediaComposition"-API. Es ist **kein** API-Key nötig.
- Bereits bekannte Episoden werden in `data/episodes_cache.json`
  zwischengespeichert – jeder Lauf holt nur wirklich neue Folgen nach.
- Aus dem Cache wird `docs/<GEHEIMES-TOKEN>/feed.xml` erzeugt.
- GitHub Pages veröffentlicht den Inhalt von `docs/`.

## "Passwortschutz"

GitHub Pages ist rein statisches Hosting und kann keine echte
Serverauthentifizierung (z. B. HTTP Basic Auth) prüfen. Der Schutz besteht
daher aus einer **langen, zufälligen, geheimen URL** – die Feed-Adresse
selbst ist das Passwort, direkt im Feed-Link integriert
(`https://DEINNAME.github.io/DEIN-REPO/<TOKEN>/feed.xml`).
Das ist dieselbe Methode, die z. B. Patreon oder andere Anbieter für private
Podcast-Feeds verwenden.

**Wichtig:**
- Diese URL nie öffentlich teilen, posten oder verlinken.
- `docs/robots.txt` verbietet Suchmaschinen das Crawlen zusätzlich.
- Repository am besten als **privat** anlegen (siehe unten) – dann findet
  niemand das Token z. B. über die GitHub-Code-Suche.
- Es handelt sich nicht um echte Kryptografie/Authentifizierung, sondern um
  "security through obscurity". Für einen rein privaten Feed (Familie,
  eigene Geräte) ist das der praxisübliche und ausreichende Ansatz.

## Einrichtung

### 1. Repository erstellen

- Neues Repository auf GitHub anlegen (Empfehlung: **privat**), diesen Code
  hineinpushen.

### 2. Geheimes Token erzeugen

Ein langes, zufälliges Token generieren, z. B. lokal im Terminal:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

### 3. Repository-Secret & Variable setzen

Unter **Settings → Secrets and variables → Actions**:

- **Secrets → New repository secret**
  - Name: `FEED_SECRET_TOKEN`
  - Wert: das eben generierte Token
- **Variables → New repository variable**
  - Name: `PAGES_BASE_URL`
  - Wert: `https://DEINNAME.github.io/DEIN-REPO` (ohne Slash am Ende) –
    wird nur für den `<atom:link>`-Self-Verweis im Feed benutzt, rein
    kosmetisch, kann notfalls auch leer bleiben.

### 4. GitHub Pages aktivieren

**Settings → Pages → Build and deployment → Source: "GitHub Actions"**
auswählen (nicht "Deploy from a branch").

### 5. Workflow-Rechte prüfen

**Settings → Actions → General → Workflow permissions**:
"Read and write permissions" aktivieren, damit die Action den aktualisierten
Cache/Feed zurück ins Repo committen darf.

### 6. Ersten Lauf starten

Im Tab **Actions** den Workflow "Update Maloney podcast feed" auswählen und
manuell über **Run workflow** starten. Der erste Lauf lädt den ganzen
Episodenkatalog (mehrere hundert Folgen) und dauert daher spürbar länger als
die täglichen Folgeläufe, die nur nach neuen Folgen schauen.

### 7. Feed-URL in die Podcast-App eintragen

Nach dem ersten erfolgreichen Lauf ist der Feed erreichbar unter:

```
https://DEINNAME.github.io/DEIN-REPO/<DEIN-TOKEN>/feed.xml
```

Diese URL in einer beliebigen Podcast-App als "Feed per URL abonnieren"
eintragen (z. B. Apple Podcasts: "Nach URL abonnieren").

## Konfiguration

In `scripts/build_feed.py` lassen sich u. a. anpassen:

- `FEED_MAX_ITEMS` (Umgebungsvariable, Standard 500) – wie viele der
  neusten Episoden im Feed erscheinen.
- Cronzeitplan in `.github/workflows/update-feed.yml`.

## Hinweis zur Nutzung

Dieses Projekt ruft ausschliesslich öffentlich zugängliche SRF/SRG-SSR
Schnittstellen ab, wie sie auch der offizielle Webplayer nutzt, und dient
dem persönlichen, nicht-kommerziellen Gebrauch (z. B. Offline-Hören in der
eigenen Podcast-App). Es umgeht keine Bezahlschranken oder DRM – die Inhalte
sind auf srf.ch ohnehin frei und kostenlos abrufbar.
