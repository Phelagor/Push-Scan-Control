#!/usr/bin/env bash
set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

SERVICE_USER="epsonscan"
SERVICE_GROUP="epsonscan"
DEFAULT_SCAN_DIR="/srv/scans"

DAEMON_SERVICE="epson-push-daemon.service"
WEB_SERVICE="epson-push-web.service"

echo -e "${GREEN}===========================================${NC}"
echo -e "${GREEN}   Epson Push-Scan Auto-Installer         ${NC}"
echo -e "${GREEN}===========================================${NC}\n"

# -----------------------------------------------------------------------------
# 1. Root-Rechte prüfen
# -----------------------------------------------------------------------------
if [ "$EUID" -ne 0 ]; then
    echo -e "${RED}[FEHLER] Dieses Skript benötigt Root-Rechte.${NC}"
    echo -e "Bitte starte die Installation erneut mit:"
    echo -e "  ${YELLOW}sudo bash install.sh${NC}\n"
    exit 1
fi

# -----------------------------------------------------------------------------
# 2. Dedicated System-Benutzer & Gruppe anlegen
# -----------------------------------------------------------------------------
if ! getent group "$SERVICE_GROUP" >/dev/null; then
    echo -e "${BLUE}[USER] Erstelle System-Gruppe '$SERVICE_GROUP'...${NC}"
    groupadd -r "$SERVICE_GROUP"
fi

if ! id "$SERVICE_USER" &>/dev/null; then
    echo -e "${BLUE}[USER] Erstelle System-Benutzer '$SERVICE_USER'...${NC}"
    useradd -r -g "$SERVICE_GROUP" -s /usr/sbin/nologin -d /var/lib/epsonscan -m "$SERVICE_USER"
fi

# -----------------------------------------------------------------------------
# 3. Prüfen, ob Services bereits installiert sind oder laufen
# -----------------------------------------------------------------------------
SERVICE_INSTALLED=false
SERVICE_RUNNING=false

if systemctl list-unit-files | grep -q "$DAEMON_SERVICE" || systemctl list-unit-files | grep -q "$WEB_SERVICE"; then
    SERVICE_INSTALLED=true
fi

if systemctl is-active --quiet "$DAEMON_SERVICE" || systemctl is-active --quiet "$WEB_SERVICE"; then
    SERVICE_RUNNING=true
fi

if [ "$SERVICE_INSTALLED" = true ] || [ "$SERVICE_RUNNING" = true ]; then
    echo -e "${YELLOW}[HINWEIS] Bereits vorhandene Installation erkannt!${NC}"
    if [ "$SERVICE_RUNNING" = true ]; then
        echo -e "  - Status: Mindestens ein Service ist aktuell ${GREEN}AKTIV / LÄUFT${NC}."
    else
        echo -e "  - Status: Services sind im System ${YELLOW}REGISTRIERT${NC}."
    fi
    echo -e "  ${RED}ACHTUNG: Eine Neuinstallation stoppt laufende Dienste und überschreibt die Programmdateien.${NC}"
    
    # Standard: N (Nein / Abbrechen)
    read -p "Möchtest du die Installation trotzdem fortsetzen? [j/N] (Standard: N = Abbrechen): " CONTINUE_INSTALL
    CONTINUE_INSTALL=${CONTINUE_INSTALL:-N}

    case "$CONTINUE_INSTALL" in
        [jJ][eE][sS]|[jJ])
            echo -e "${BLUE}Stoppe laufende Dienste für das Update...${NC}"
            systemctl stop "$DAEMON_SERVICE" "$WEB_SERVICE" 2>/dev/null || true
            ;;
        *)
            echo -e "${RED}Installation abgebrochen.${NC}"
            exit 0
            ;;
    esac
    echo ""
fi

# -----------------------------------------------------------------------------
# 4. Behandlung der Konfigurationsdateien
# -----------------------------------------------------------------------------
USE_EXISTING_DAEMON_CFG=false
USE_EXISTING_SCAN_CFG=false

# 4a. Daemon-Konfiguration (/etc/epson/daemon_config.json)
if [ -f "/etc/epson/daemon_config.json" ]; then
    echo -e "${BLUE}[CONFIG] Bereits vorhandene '/etc/epson/daemon_config.json' gefunden.${NC}"
    
    # Standard: N (Nein = Nicht überschreiben / Bestehende Datei beibehalten)
    read -p "Möchtest du diese bestehende Daemon-Konfiguration ÜBERSCHREIBEN? [j/N] (Standard: N = Beibehalten): " OVERWRITE_D_CFG
    OVERWRITE_D_CFG=${OVERWRITE_D_CFG:-N}

    case "$OVERWRITE_D_CFG" in
        [jJ][eE][sS]|[jJ])
            echo -e "  -> Daemon-Konfiguration wird durch neue lokale 'daemon_config.json' überschrieben."
            ;;
        *)
            USE_EXISTING_DAEMON_CFG=true
            echo -e "  ${GREEN}-> Bestehende '/etc/epson/daemon_config.json' wird beibehalten.${NC}"
            ;;
    esac
fi

if [ "$USE_EXISTING_DAEMON_CFG" = false ]; then
    if [ ! -f "daemon_config.json" ]; then
        echo -e "\n${YELLOW}[HINWEIS] Keine lokale 'daemon_config.json' im Verzeichnis gefunden!${NC}"
        echo -e "Bitte führe folgende Schritte aus:"
        echo -e "  1. Erstelle die Konfigurationsdatei aus dem Template:"
        echo -e "     ${YELLOW}cp daemon_config.template.json daemon_config.json${NC}"
        echo -e "  2. Trage deine Scanner IP-Adresse & Hostnamen ein:"
        echo -e "     ${YELLOW}nano daemon_config.json${NC}"
        echo -e "  3. Starte diesen Installer erneut:"
        echo -e "     ${YELLOW}sudo bash install.sh${NC}\n"
        exit 1
    fi
fi

# 4b. Scan-Profile Konfiguration (/etc/epson/scan_config.json)
if [ -f "/etc/epson/scan_config.json" ]; then
    echo -e "${BLUE}[CONFIG] Bereits vorhandene '/etc/epson/scan_config.json' gefunden.${NC}"
    
    # Standard: N (Nein = Nicht überschreiben / Bestehende Profile beibehalten)
    read -p "Möchtest du deine bestehenden Scan-Profile ÜBERSCHREIBEN? [j/N] (Standard: N = Beibehalten): " OVERWRITE_S_CFG
    OVERWRITE_S_CFG=${OVERWRITE_S_CFG:-N}

    case "$OVERWRITE_S_CFG" in
        [jJ][eE][sS]|[jJ])
            echo -e "  -> Scan-Profile werden auf Standardwerte überschrieben."
            ;;
        *)
            USE_EXISTING_SCAN_CFG=true
            echo -e "  ${GREEN}-> Bestehende '/etc/epson/scan_config.json' wird beibehalten.${NC}"
            ;;
    esac
fi

# -----------------------------------------------------------------------------
# 5. Skript-Dateien prüfen
# -----------------------------------------------------------------------------
for FILE in "epson_scanner_daemon.py" "epson_web_config.py"; do
    if [ ! -f "$FILE" ]; then
        echo -e "\n${RED}[FEHLER] Erforderliche Skript-Datei '$FILE' fehlt im Installationsverzeichnis!${NC}"
        exit 1
    fi
done

echo -e "\n${GREEN}[1/5] Erstelle Systemverzeichnisse & Rechte...${NC}"
mkdir -p /etc/epson
mkdir -p /var/log/epson
mkdir -p /usr/local/bin/epson-push-scan
mkdir -p "$DEFAULT_SCAN_DIR"
mkdir -p /srv/Scans

echo -e "${GREEN}[2/5] Prüfe und installiere Paket-Abhängigkeiten...${NC}"
if command -v apt-get &> /dev/null; then
    apt-get update -qq
    apt-get install -y -qq python3 python3-flask sane-utils > /dev/null
elif command -v dnf &> /dev/null; then
    dnf install -y -q python3 python3-flask sane-backends
elif command -v pacman &> /dev/null; then
    pacman -Sy --noconfirm python python-flask sane
fi

echo -e "${GREEN}[3/5] Kopiere Programmdateien & Setze Zugriffsrechte...${NC}"
cp epson_scanner_daemon.py /usr/local/bin/epson-push-scan/epson_scanner_daemon.py
cp epson_web_config.py /usr/local/bin/epson-push-scan/epson_web_config.py

if [ "$USE_EXISTING_DAEMON_CFG" = false ]; then
    cp daemon_config.json /etc/epson/daemon_config.json
fi

if [ "$USE_EXISTING_SCAN_CFG" = false ]; then
    if [ -f "scan_config.json" ]; then
        cp scan_config.json /etc/epson/scan_config.json
    fi
fi

chown -R "$SERVICE_USER:$SERVICE_GROUP" /etc/epson
chown -R "$SERVICE_USER:$SERVICE_GROUP" /var/log/epson
chown -R "$SERVICE_USER:$SERVICE_GROUP" /usr/local/bin/epson-push-scan
chown -R "$SERVICE_USER:$SERVICE_GROUP" "$DEFAULT_SCAN_DIR"
chown -R "$SERVICE_USER:$SERVICE_GROUP" /srv/Scans

chmod 755 /usr/local/bin/epson-push-scan/*.py
chmod 775 /etc/epson
chmod 664 /etc/epson/*.json 2>/dev/null || true
chmod 775 /var/log/epson
chmod 777 "$DEFAULT_SCAN_DIR"
chmod 777 /srv/Scans

echo -e "${GREEN}[4/5] Erstelle Non-Root Systemd-Dienste...${NC}"

cat << EOF > /etc/systemd/system/epson-push-daemon.service
[Unit]
Description=Epson Network Scanner Push Daemon
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
Group=$SERVICE_GROUP
ExecStart=/usr/bin/python3 /usr/local/bin/epson-push-scan/epson_scanner_daemon.py
Environment="EPSON_DAEMON_CONFIG=/etc/epson/daemon_config.json"
Environment="EPSON_SCAN_CONFIG=/etc/epson/scan_config.json"
Environment="EPSON_DAEMON_LOG=/var/log/epson/daemon.log"
Environment="PYTHONUNBUFFERED=1"
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

cat << EOF > /etc/systemd/system/epson-push-web.service
[Unit]
Description=Epson Push-Scan Web Configuration Service
After=network.target

[Service]
Type=simple
User=$SERVICE_USER
Group=$SERVICE_GROUP
ExecStart=/usr/bin/python3 /usr/local/bin/epson-push-scan/epson_web_config.py
Environment="EPSON_DAEMON_CONFIG=/etc/epson/daemon_config.json"
Environment="EPSON_SCAN_CONFIG=/etc/epson/scan_config.json"
Environment="EPSON_DAEMON_LOG=/var/log/epson/daemon.log"
Environment="EPSON_WEB_LOG=/var/log/epson/web.log"
Environment="PYTHONUNBUFFERED=1"
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

echo -e "${GREEN}[5/5] Aktiviere und starte Services...${NC}"
systemctl daemon-reload
systemctl enable --now epson-push-daemon.service
systemctl enable --now epson-push-web.service

LOCAL_IP=$(hostname -I | awk '{print $1}')

echo -e "\n${GREEN}===========================================${NC}"
echo -e "${GREEN}   Installation erfolgreich abgeschlossen!  ${NC}"
echo -e "${GREEN}===========================================${NC}\n"
echo -e "Die Dienste laufen geschützt unter dem Benutzer: ${YELLOW}$SERVICE_USER${NC}"
echo -e "Standard-Speicherort für Scans: ${YELLOW}$DEFAULT_SCAN_DIR${NC}"
echo -e "Die Web-Oberfläche ist erreichbar unter:"
echo -e "  ${YELLOW}http://${LOCAL_IP}:8080${NC}\n"
echo -e "Status prüfen:"
echo -e "  ${YELLOW}systemctl status epson-push-daemon.service${NC}"
echo -e "  ${YELLOW}systemctl status epson-push-web.service${NC}\n"
