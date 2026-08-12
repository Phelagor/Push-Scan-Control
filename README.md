# Epson Network Push-Scan Control Center

Eine lightweight, entkoppelte Linux-Lösung, um Push-Button-Scans von Epson-Netzwerkscannern (z. B. Expression- und WorkForce-Serien) abzufangen und automatisiert auf einem Linux-Rechner oder Server zu speichern.

Das System besteht aus einem hochperformanten **Kommunikations-Daemon** für die Netzwerkabwicklung sowie einem modernen **Web-Configuration Service** inklusive server-seitigem Ordner-Browser, Eingabe-Sanitierung und Live-Log-Viewer. Beide Dienste laufen aus Sicherheitsgründen vollständig in einem isolierten **Non-Root-Kontext**.

---

## 📸 Funktionsweise

Wenn am LCD-Display des Scanners eine Taste gedrückt wird (z. B. *"Scan auf Computer (JPEG)"*), sendet das Gerät eine Nachricht im Epson-Netzwerkprotokoll (**ENSP / ESC/I-net**).

1. **Service Discovery (UDP 2968):** Der Scanner fragt per SLP-Multicast nach registrierten Rechnern. Der Daemon antwortet mit dem konfigurierten Hostnamen.


2. **Event Trigger (TCP 1865):** Der Scanner sendet einen HTTP/SOAP-Event an den Daemon, der die spezifische `PushScanID` (Taste 1, 2 oder 3) enthält.


3. **Bildübertragung (SANE):** Der Daemon liest **on-the-fly** das zugewiesene Scan-Profil (Format, Modus, Auflösung, Speicherort) aus und führt die Digitalisierung via `scanimage` aus.



---

## 💻 Hardware-Kompatibilität & Voraussetzungen

### Getestete Hardware

* **Epson Expression Premium XP-352** *(vollständig getestet und verifiziert)*

* *Generell kompatibel mit allen Epson Multifunktionsgeräten und Scannern, die das **Epson ENSP / ESC/I-net Protocol** für Push-Scans nutzen.*


### Systemvoraussetzungen

* **Betriebssystem:** Linux (getestet unter Linux Mint / Ubuntu / Debian, RHEL, Arch)


* **Init-System:** `systemd` (für automatischen Hintergrundbetrieb)


* **Pakete:**
* `python3` (inkl. `python3-flask`)


* `sane` / `sane-utils` (Backend `epson2` muss den Scanner im Netzwerk finden können)




* **Netzwerk:** Rechner/Server und Scanner müssen im selben Subnetz stehen (Multicast `239.255.255.253` erforderlich).



---

## 🔒 Sicherheitsarchitektur (Non-Root Execution)

Sämtliche Dienste laufen streng getrennt vom Root-Benutzer unter einem dedizierten System-Benutzer `epsonscan`.

```
HTTP-POST Nachricht / UI-Eingabe
       │
       ▼
[1] Python Whitelist & Sanitierung ──(Ungültige Zeichen / '..')──► ABGEBROCHEN
       │
       ▼
[2] Server Schreibtest (epsonscan) ──(Keine Schreibrechte)───────► ABGEBROCHEN
       │
       ▼
[3] Linux OS Rechte-Grenzen       ──(System-User ohne Root)──────► ISOLIERT
       │
       ▼
  GESPEICHERT IN /srv/scans

```

* **Client-Unabhängigkeit:** Das System vertraut keinen Formular-Eingaben des Browsers. Sämtliche Pfade werden serverseitig in Python validiert und auf Schreibrechte geprüft.


* **Pfad-Traversierungsschutz:** Das Ausbrechen aus Zielverzeichnissen (z. B. via `..` oder Backslashes) ist serverseitig unterbunden.


* **System-Isolierung:** Selbst im Falle einer theoretischen Sicherheitslücke im Webserver besitzt der Dienst keinerlei administrative Systemrechte.



---

## 🚀 Installation

Die Installation erfolgt vollautomatisch über das interaktive Skript `install.sh`.

### 1. Repository klonen oder entpacken

```bash
git clone https://github.com/dein-username/epson-push-scan.git
cd epson-push-scan

```

### 2. Daemon-Konfiguration anlegen

Erstelle aus dem Template deine initiale Hardware-Konfiguration:

```bash
cp daemon_config.template.json daemon_config.json
nano daemon_config.json

```

Passe dort mindestens die **IP-Adresse** deines Scanners an:

```json
{
  "scanner_ip": "192.168.178.XXX",
  "client_name": "LINUX-WORKSTATION",
  "event_port": 1865,
  "slp_port": 2968,
  "mcast_grp": "239.255.255.253"
}

```

### 3. Installer ausführen

Starte das Installationsskript mit Root-Rechten:

```bash
sudo bash install.sh

```

Das Installer-Skript führt automatisch folgende Schritte durch:

* Erstellung des geschützten System-Benutzers und der Gruppe `epsonscan`.


* Installation fehlender Systempakete (`python3-flask`, `sane-utils`).


* Anlegen und Berechtigen der Systemordner `/etc/epson/`, `/var/log/epson/` und des Standard-Scan-Ordners `/srv/scans/`.


* Erstellung und Start der zwei Non-Root `systemd`-Dienste (`epson-push-daemon` und `epson-push-web`).



---

## 🛠️ Benutzung & Web-Oberfläche

Nach erfolgreicher Installation ist die Web-Oberfläche im lokalen Netzwerk von jedem beliebigen PC oder Gerät erreichbar unter:

```text
http://<IP-DES-SERVERS>:8080

```

### Hauptseite: Scan-Profile & Ordner-Browser

Auf der Startseite können die Zielordner und Scan-Parameter für alle Tasten des Scanners angepasst werden:

* **Haupt-Zielpfad (Server-Ordner-Browser):**
* Der Pfad kann aus Sicherheitsgründen nicht mehr frei als Text getippt werden.


* Über den Button **`📁 Ordner wählen`** öffnet sich ein server-seitiges Browser-Popup. Es werden ausschließlich Verzeichnisse aufgelistet, auf die der Service-User `epsonscan` Schreibrechte besitzt.




* **Tasten-Zuweisung (PushScanIDs 01, 02, 03):**
* Dateiformat (`JPEG`, `PDF`, `PNG`, `TIFF`)


* Farbmodus (`Color`, `Gray`, `Lineart`)


* Auflösung (`150`, `300`, `600` DPI)


* **Unterordner für diese Taste (Optional):** Ermöglicht die Angabe von relativen Subordnern (z. B. `Rechnungen` $\rightarrow$ speichert unter `/srv/scans/Rechnungen`). Eingaben werden streng sanitisiert (max. 64 Zeichen, Whitelist für Buchstaben/Zahlen/Umlaute, keine Pfadtraversierungen).





*Änderungen an den Scan-Profilen werden vom Daemon **sofort beim nächsten Tastendruck** ohne Service-Neustart übernommen!*

### ⚙️ Navigation & Menü (Oben Rechts)

Klicke auf das Zahnrad-Icon `⚙️` oben rechts (inkl. Tooltip), um das Dropdown-Menü zu öffnen:

* **📄 Scan Profile Einstellungen:** Zurück zur Hauptseite.


* **⚙️ Daemon Einstellungen:** Ändern der Scanner-IP, des Anzeigenamens auf dem LCD oder der Netzwerk-Ports.


* **📋 Daemon Log anzeigen:** Live-Einsicht in die letzten 50 Log-Einträge des Scanners inklusive vollständiger Error-Tracebacks.


* **📋 Web Service Log anzeigen:** Live-Einsicht in die Zugriffe und Validierungsmeldungen des Web-Interfaces.


* **💾 Log-Download:** In den Log-Sichten kann über einen Button das vollständige Logfile als Datei heruntergeladen werden.



---

## 🗂️ Verzeichnisstruktur & Dateien

Nach der Installation stehen die Dateien an folgenden Orten im System:

| Pfad | Beschreibung |
| --- | --- |
| `/usr/local/bin/epson-push-scan/` | Skriptverzeichnis (`epson_scanner_daemon.py`, `epson_web_config.py`) |
| `/etc/epson/daemon_config.json` | Hardware- & Netzwerkeinstellungen (Scanner IP, ClientName, Ports) |
| `/etc/epson/scan_config.json` | Profil-Einstellungen (Formate, Auflösungen, Subordner) |
| `/var/log/epson/daemon.log` | Protokoll der Scan-Vorgänge, SANE-Ausgaben und Netzwerk-Events |
| `/var/log/epson/web.log` | Protokoll des Web-Interfaces & Pfad-Validierungen |
| `/srv/scans/` | Standard-Speicherort für alle gescannten Dokumente |

---

## 🔧 Service-Steuerung

Die Dienste laufen als Hintergrunddienste unter dem System-Benutzer `epsonscan` und starten automatisch beim Systemstart. Sie können über `systemctl` verwaltet werden:

```bash
# Status der Dienste prüfen
sudo systemctl status epson-push-daemon.service
sudo systemctl status epson-push-web.service

# Dienste neustarten
sudo systemctl restart epson-push-daemon.service
sudo systemctl restart epson-push-web.service

# Live-Logs im System-Journal verfolgen
journalctl -u epson-push-daemon.service -f

```

---

## ❓ Troubleshooting

1. **Der Scanner findet den PC/Server nicht am Display:**
* Stelle sicher, dass die IP-Adresse in den Daemon-Einstellungen korrekt angegeben ist.


* Prüfe, ob eine Firewall (z. B. `ufw`) den UDP-Port `2968` oder TCP-Port `1865` blockiert.
* Der Server und der Scanner müssen sich im selben Layer-2-Netzwerk befinden (Multicast-Anfragen können geroutete VLANs nicht ohne Weiteres durchqueren).


2. **Fehlermeldung bei Schreibrechten / Speichern im Web-Interface abgebrochen:**
* Falls du einen benutzerdefinierten Pfad außerhalb von `/srv/scans` nutzen möchtest, musst du dem Benutzer `epsonscan` Schreibrechte darauf erteilen:
```bash
sudo chown -R epsonscan:epsonscan /dein/zielpfad
sudo chmod 775 /dein/zielpfad

```




3. **Fehlermeldung bei `scanimage`:**
* Teste im Terminal, ob SANE deinen Scanner manuell erkennt: `scanimage -L`.


* Falls der Scanner per IP nicht gefunden wird, passe die Konfigurationsdatei `/etc/sane.d/epson2.conf` an und trage dort `net 192.168.178.XX` ein.
