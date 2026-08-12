#!/usr/bin/env python3
from collections import deque
import json
import logging
import os
import re
import socket
import subprocess
from flask import Flask, request, render_template_string, send_file, redirect, jsonify

DAEMON_CONFIG_PATH = os.environ.get("EPSON_DAEMON_CONFIG", "/etc/epson/daemon_config.json")
SCAN_CONFIG_PATH = os.environ.get("EPSON_SCAN_CONFIG", "/etc/epson/scan_config.json")

DAEMON_LOG_PATH = os.environ.get("EPSON_DAEMON_LOG", "/var/log/epson/daemon.log")
WEB_LOG_PATH = os.environ.get("EPSON_WEB_LOG", "/var/log/epson/web.log")

app = Flask(__name__)

def setup_web_logging():
    log_dir = os.path.dirname(WEB_LOG_PATH)
    try:
        os.makedirs(log_dir, exist_ok=True)
        target_log = WEB_LOG_PATH
    except PermissionError:
        fallback_dir = os.path.expanduser("~/.config/epson/logs")
        os.makedirs(fallback_dir, exist_ok=True)
        target_log = os.path.join(fallback_dir, "web.log")

    file_handler = logging.FileHandler(target_log, encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    app.logger.addHandler(file_handler)
    app.logger.setLevel(logging.INFO)
    return target_log

ACTUAL_WEB_LOG = setup_web_logging()

def check_path_writable(path):
    """Prüft, ob der Service-User epsonscan Schreibrechte auf den Pfad hat."""
    expanded_path = os.path.expanduser(path.strip())
    try:
        os.makedirs(expanded_path, exist_ok=True)
        test_file = os.path.join(expanded_path, ".write_test")
        with open(test_file, "w") as f:
            f.write("test")
        os.remove(test_file)
        return True, expanded_path
    except PermissionError:
        return False, f"Keine Schreibrechte für den Benutzer 'epsonscan' in '{expanded_path}'."
    except Exception as e:
        return False, f"Fehler beim Pfad '{expanded_path}': {e}"

def sanitize_subfolder(name):
    """Sanitiert und validiert die Freitext-Eingabe für Tasten-Subordner."""
    if not name:
        return True, "", ""
    
    clean_name = name.strip().lstrip("/\\")
    
    if len(clean_name) > 64:
        return False, clean_name, "Der Subordner-Name darf maximal 64 Zeichen lang sein."
        
    if ".." in clean_name or "//" in clean_name or "\\" in clean_name:
        return False, clean_name, "Ungültiger Pfad: Pfad-Traversierungen ('..') oder Backslashes sind nicht erlaubt."
        
    if not re.match(r'^[a-zA-Z0-9äöüÄÖÜß_\-\s/]+$', clean_name):
        return False, clean_name, "Der Subordner enthält ungültige Sonderzeichen. Erlaubt sind nur Buchstaben, Zahlen, Leerzeichen, - und _."
        
    clean_name = "/".join([part.strip() for part in clean_name.split("/") if part.strip()])
    return True, clean_name, ""

def is_scanner_online(ip_address, port=1865, timeout=1.5):
    """Prüft schnell, ob die Scanner-IP im Netzwerk antwortet."""
    if not ip_address:
        return False
    try:
        with socket.create_connection((ip_address, port), timeout=timeout):
            return True
    except (OSError, TimeoutError):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(timeout)
            s.connect((ip_address, 80))
            s.close()
            return True
        except Exception:
            return False

def query_sane_info(scanner_ip):
    """Führt scanimage -A aus und liefert die Hardware-Details zurück."""
    device_name = f"epson2:net:{scanner_ip}"
    cmd = ["scanimage", f"--device-name={device_name}", "-A"]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=6)
        output = res.stdout if res.stdout else res.stderr
        return True, output.strip()
    except subprocess.TimeoutExpired:
        return False, "Zeitüberschreitung beim Abfragen des Scanners via SANE (Timeout)."
    except Exception as e:
        return False, f"Fehler beim Ausführen von scanimage: {e}"

def parse_sane_info(raw_text):
    """Parst die SANE -A Ausgabe, filtert inaktive Optionen und strukturiert die Daten."""
    sections = []
    current_section = {"title": "Allgemeine Hardware-Optionen", "options": []}
    
    lines = raw_text.splitlines()
    for line in lines:
        line_str = line.strip()
        if not line_str or line_str.startswith("All options specific to device"):
            continue
        
        # SANE Sektions-Header (z. B. "Scan Mode:", "Geometry:")
        if line_str.endswith(":") and not line_str.startswith("-"):
            if current_section["options"]:
                sections.append(current_section)
            current_section = {"title": line_str[:-1].strip(), "options": []}
            continue
        
        # SANE Optionen parsen: --name range/choices [default_val] oder -x range [val]
        opt_match = re.match(r'^-{1,2}([a-zA-Z0-9\-_]+)(?:\s+(.*?))?(?:\s+\[(.*?)\])?$', line_str)
        if opt_match:
            opt_name = opt_match.group(1)
            opt_spec = (opt_match.group(2) or "").strip()
            opt_val = (opt_match.group(3) or "").strip()
            
            # Inaktive Optionen ausfiltern
            if opt_val.lower() == "inactive" or "inactive" in opt_spec.lower():
                continue
            
            current_section["options"].append({
                "name": opt_name,
                "spec": opt_spec,
                "value": opt_val,
                "description": ""
            })
        else:
            # Beschreibungszeilen der vorangegangenen Option zuordnen
            if current_section["options"]:
                prev_opt = current_section["options"][-1]
                if prev_opt["description"]:
                    prev_opt["description"] += " " + line_str
                else:
                    prev_opt["description"] = line_str

    if current_section["options"]:
        sections.append(current_section)
        
    return sections


BASE_LAYOUT = """
<!DOCTYPE html>
<html lang="de">
<head>
    <meta charset="UTF-8">
    <title>Epson Push-Scan Control Center</title>
    <style>
        :root {
            --primary: #2b6cb0;
            --primary-hover: #2c5282;
            --bg: #f7fafc;
            --card-bg: #ffffff;
            --border: #e2e8f0;
            --text: #2d3748;
        }
        body { font-family: system-ui, -apple-system, sans-serif; background: var(--bg); color: var(--text); margin: 0; padding: 0; }
        header { background: #ffffff; border-bottom: 1px solid var(--border); padding: 15px 30px; display: flex; justify-content: space-between; align-items: center; }
        
        header h1 { margin: 0; font-size: 20px; }
        header h1 a { color: var(--primary); text-decoration: none; transition: color 0.2s; }
        header h1 a:hover { color: var(--primary-hover); }
        
        .settings-menu { position: relative; display: inline-block; }
        .gear-btn { 
            background: none; 
            border: 1px solid var(--border); 
            font-size: 20px; 
            cursor: pointer; 
            padding: 6px 10px; 
            border-radius: 6px; 
            line-height: 1;
            transition: background 0.2s, border-color 0.2s; 
        }
        .gear-btn:hover { background: #edf2f7; border-color: #cbd5e0; }
        
        .dropdown-content { 
            display: none; 
            position: absolute; 
            right: 0; 
            top: 125%; 
            background-color: #ffffff; 
            min-width: 240px; 
            box-shadow: 0px 8px 16px rgba(0,0,0,0.12); 
            border-radius: 6px; 
            border: 1px solid var(--border); 
            z-index: 1000; 
            overflow: hidden; 
        }
        .dropdown-content.show { display: block; }
        .dropdown-content a { color: var(--text); padding: 12px 16px; text-decoration: none; display: block; font-size: 14px; border-bottom: 1px solid #f0f0f0; }
        .dropdown-content a:last-child { border-bottom: none; }
        .dropdown-content a:hover { background-color: #edf2f7; }

        .container { max-width: 900px; margin: 30px auto; background: var(--card-bg); padding: 30px; border-radius: 8px; box-shadow: 0 4px 6px rgba(0,0,0,0.05); border: 1px solid var(--border); }
        
        .alert-banner {
            padding: 14px 18px;
            background-color: #c6f6d5;
            color: #22543d;
            border: 1px solid #9ae6b4;
            border-radius: 6px;
            margin-bottom: 25px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            font-size: 14px;
            font-weight: 500;
        }
        .alert-banner.error {
            background-color: #fed7d7;
            color: #9b2c2c;
            border-color: #feb2b2;
        }
        .alert-banner.warning {
            background-color: #feebc8;
            color: #744210;
            border-color: #fbd38d;
        }
        .alert-banner .close-btn {
            background: none;
            border: none;
            font-size: 18px;
            color: inherit;
            cursor: pointer;
            line-height: 1;
            padding: 0 4px;
        }

        .status-badge {
            display: inline-flex;
            align-items: center;
            gap: 8px;
            padding: 6px 12px;
            border-radius: 20px;
            font-size: 13px;
            font-weight: 600;
        }
        .status-badge.online { background: #c6f6d5; color: #22543d; }
        .status-badge.offline { background: #fed7d7; color: #9b2c2c; }

        .form-group { margin-bottom: 18px; }
        label { display: block; font-weight: 600; margin-bottom: 6px; font-size: 14px; }
        input, select { width: 100%; padding: 10px; box-sizing: border-box; border: 1px solid var(--border); border-radius: 6px; font-size: 14px; }
        .card { background: #f8fafc; border: 1px solid var(--border); padding: 20px; margin-bottom: 20px; border-radius: 6px; }
        .card h3 { margin-top: 0; font-size: 16px; color: var(--primary); border-bottom: 1px solid var(--border); padding-bottom: 8px; }
        button.btn-submit { background: var(--primary); color: white; border: none; padding: 12px 20px; border-radius: 6px; cursor: pointer; font-size: 15px; font-weight: 600; width: 100%; }
        button.btn-submit:hover { background: var(--primary-hover); }
        .hint { font-size: 12px; color: #718096; margin-top: 4px; }
        
        .log-area { width: 100%; height: 420px; background-color: #1a202c; color: #48bb78; font-family: 'Courier New', Courier, monospace; font-size: 13px; padding: 15px; box-sizing: border-box; border-radius: 6px; border: 1px solid #2d3748; resize: vertical; white-space: pre; overflow-x: auto; }
        .btn-download { display: inline-block; background: #38a169; color: white; text-decoration: none; padding: 10px 16px; border-radius: 6px; font-weight: 600; font-size: 14px; margin-top: 15px; }
        .btn-download:hover { background: #2f855a; }

        .modal { display: none; position: fixed; z-index: 2000; left: 0; top: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.5); align-items: center; justify-content: center; }
        .modal.show { display: flex; }
        .modal-content { background: white; width: 90%; max-width: 600px; border-radius: 8px; padding: 20px; box-shadow: 0 10px 25px rgba(0,0,0,0.2); }
        .modal-header { display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid var(--border); padding-bottom: 10px; margin-bottom: 15px; }
        .modal-header h3 { margin: 0; font-size: 18px; color: var(--primary); }
        .folder-list { max-height: 300px; overflow-y: auto; border: 1px solid var(--border); border-radius: 6px; margin: 15px 0; padding: 0; list-style: none; }
        .folder-item { padding: 10px 15px; border-bottom: 1px solid #f0f0f0; cursor: pointer; display: flex; align-items: center; gap: 10px; font-size: 14px; }
        .folder-item:hover { background: #edf2f7; }
        .folder-item:last-child { border-bottom: none; }
        .path-display { font-family: monospace; background: #edf2f7; padding: 8px; border-radius: 4px; font-size: 13px; word-break: break-all; }
        .modal-footer { display: flex; justify-content: flex-end; gap: 10px; margin-top: 15px; }

        /* Style für formatierte Scanner-Hardware-Kacheln */
        .opt-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 12px; margin-top: 12px; }
        .opt-card { background: #ffffff; border: 1px solid var(--border); padding: 12px 14px; border-radius: 6px; }
        .opt-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px; }
        .opt-name { font-weight: 700; color: var(--primary); font-size: 13px; font-family: monospace; }
        .opt-badge { background: #e2e8f0; color: #2d3748; padding: 2px 8px; border-radius: 12px; font-size: 12px; font-weight: 600; }
        .opt-spec { font-size: 12px; color: #4a5568; font-family: monospace; margin-bottom: 4px; word-break: break-all; }
        .opt-desc { font-size: 11px; color: #718096; line-height: 1.3; }
    </style>
    <script>
        function toggleDropdown(event) {
            event.stopPropagation();
            var dropdown = document.getElementById("navDropdown");
            dropdown.classList.toggle("show");
        }

        window.onclick = function(event) {
            var dropdown = document.getElementById("navDropdown");
            if (dropdown && dropdown.classList.contains("show")) {
                dropdown.classList.remove("show");
            }
        };

        let currentBrowsePath = "/srv/scans";

        function openFolderModal() {
            const currentVal = document.getElementById("default_save_dir").value || "/srv/scans";
            loadFolderList(currentVal);
            document.getElementById("folderModal").classList.add("show");
        }

        function closeFolderModal() {
            document.getElementById("folderModal").classList.remove("show");
        }

        function loadFolderList(path) {
            fetch("/api/browse?path=" + encodeURIComponent(path))
                .then(res => res.json())
                .then(data => {
                    if (data.error) {
                        alert(data.error);
                        return;
                    }
                    currentBrowsePath = data.current;
                    document.getElementById("modalCurrentPath").innerText = data.current;
                    
                    const list = document.getElementById("modalFolderList");
                    list.innerHTML = "";
                    
                    if (data.parent) {
                        const li = document.createElement("li");
                        li.className = "folder-item";
                        li.innerHTML = "📁 <strong>.. (Übergeordneter Ordner)</strong>";
                        li.onclick = () => loadFolderList(data.parent);
                        list.appendChild(li);
                    }
                    
                    if (data.dirs.length === 0) {
                        const li = document.createElement("li");
                        li.className = "folder-item";
                        li.style.cursor = "default";
                        li.style.color = "#a0aec0";
                        li.innerText = "Keine weiteren beschreibbaren Unterordner vorhanden";
                        list.appendChild(li);
                    } else {
                        data.dirs.forEach(d => {
                            const li = document.createElement("li");
                            li.className = "folder-item";
                            li.innerHTML = "📂 " + d.name;
                            li.onclick = () => loadFolderList(d.path);
                            list.appendChild(li);
                        });
                    }
                })
                .catch(err => alert("Fehler beim Laden der Ordnerstruktur: " + err));
        }

        function selectCurrentFolder() {
            document.getElementById("default_save_dir").value = currentBrowsePath;
            document.getElementById("display_default_save_dir").value = currentBrowsePath;
            
            const prefixes = document.querySelectorAll(".main-path-prefix");
            prefixes.forEach(el => el.innerText = currentBrowsePath + "/");
            
            closeFolderModal();
        }
    </script>
</head>
<body>
<header>
    <h1><a href="/" title="Zurück zur Hauptseite">Epson Push-Scan Control Center</a></h1>
    <div class="settings-menu">
        <button class="gear-btn" title="Scanner Daemon Configuration" onclick="toggleDropdown(event)">⚙️</button>
        <div id="navDropdown" class="dropdown-content">
            <a href="/">📄 Scan Profile Einstellungen</a>
            <a href="/daemon-config">⚙️ Daemon Einstellungen</a>
            <a href="/scanner-info">🔍 Scanner Status & Details</a>
            <a href="/logs/daemon">📋 Daemon Log anzeigen</a>
            <a href="/logs/web">📋 Web Service Log anzeigen</a>
        </div>
    </div>
</header>
<div class="container">
    {% if status == 'scan_saved' %}
    <div class="alert-banner">
        <span>✓ Scan-Einstellungen erfolgreich gespeichert! Der Daemon übernimmt diese beim nächsten Tastendruck.</span>
        <button class="close-btn" onclick="this.parentElement.remove()">×</button>
    </div>
    {% elif status == 'daemon_saved' %}
    <div class="alert-banner">
        <span>✓ Daemon-Einstellungen erfolgreich gespeichert! Die Sockets wurden aktualisiert.</span>
        <button class="close-btn" onclick="this.parentElement.remove()">×</button>
    </div>
    {% elif error_msg %}
    <div class="alert-banner error">
        <span>⚠️ <strong>Speichern abgebrochen:</strong> {{ error_msg }}</span>
        <button class="close-btn" onclick="this.parentElement.remove()">×</button>
    </div>
    {% endif %}
    __CONTENT__
</div>

<div id="folderModal" class="modal">
    <div class="modal-content">
        <div class="modal-header">
            <h3>Server Ordner-Browser</h3>
            <button class="close-btn" onclick="closeFolderModal()" style="background:none; border:none; font-size:20px; cursor:pointer;">×</button>
        </div>
        <div><strong>Aktueller Pfad:</strong></div>
        <div id="modalCurrentPath" class="path-display">/srv/scans</div>
        <ul id="modalFolderList" class="folder-list"></ul>
        <div class="hint">Es werden ausschließlich Ordner angezeigt, auf die der Service-User <code>epsonscan</code> vollen Schreibzugriff besitzt.</div>
        <div class="modal-footer">
            <button type="button" onclick="closeFolderModal()" style="background:#e2e8f0; color:#2d3748; border:none; padding:8px 16px; border-radius:4px; cursor:pointer;">Abbrechen</button>
            <button type="button" onclick="selectCurrentFolder()" style="background:#2b6cb0; color:white; border:none; padding:8px 16px; border-radius:4px; cursor:pointer; font-weight:600;">Diesen Ordner auswählen</button>
        </div>
    </div>
</div>
</body>
</html>
"""

BODY_SCAN_CONFIG = """
<h2>Scan-Profile (Hauptseite)</h2>
<form method="POST" action="/save_scan">
    <div class="card" style="background: #ebf8ff; border-color: #bee3f8;">
        <h3>Standard-Speicherordner</h3>
        <div class="form-group">
            <label>Haupt-Zielpfad für Scans:</label>
            <div style="display: flex; gap: 10px;">
                <input type="text" id="display_default_save_dir" value="{{ scan_cfg.default_save_dir }}" readonly style="background-color: #edf2f7; cursor: not-allowed; flex: 1;">
                <input type="hidden" id="default_save_dir" name="default_save_dir" value="{{ scan_cfg.default_save_dir }}">
                <button type="button" onclick="openFolderModal()" class="btn-submit" style="width: auto; padding: 10px 18px; background: #4a5568;">📁 Ordner wählen</button>
            </div>
            <div class="hint">Kann nur über das Ordner-Auswahlmenü geändert werden. Es stehen nur beschreibbare Server-Ordner zur Auswahl.</div>
        </div>
    </div>

    <h2>LCD Tasten-Konfiguration</h2>
    {% for id, action in scan_cfg.push_actions.items() %}
    <div class="card">
        <h3>PushScanID {{ id }} (Taste {{ loop.index }})</h3>
        <div class="form-group">
            <label>Bezeichnung:</label>
            <input type="text" name="label_{{ id }}" value="{{ action.label }}">
        </div>
        <div class="form-group">
            <label>Dateiformat:</label>
            <select name="format_{{ id }}">
                <option value="jpeg" {% if action.format == 'jpeg' %}selected{% endif %}>JPEG</option>
                <option value="pdf" {% if action.format == 'pdf' %}selected{% endif %}>PDF</option>
                <option value="png" {% if action.format == 'png' %}selected{% endif %}>PNG</option>
                <option value="tiff" {% if action.format == 'tiff' %}selected{% endif %}>TIFF</option>
            </select>
        </div>
        <div class="form-group">
            <label>Farbmodus:</label>
            <select name="mode_{{ id }}">
                <option value="Color" {% if action.mode == 'Color' %}selected{% endif %}>Color (Farbe)</option>
                <option value="Gray" {% if action.mode == 'Gray' %}selected{% endif %}>Gray (Graustufen)</option>
                <option value="Lineart" {% if action.mode == 'Lineart' %}selected{% endif %}>Lineart (S/W)</option>
            </select>
        </div>
        <div class="form-group">
            <label>Auflösung (DPI):</label>
            <select name="resolution_{{ id }}">
                <option value="75" {% if action.resolution|int == 75 %}selected{% endif %}>75 DPI (Entwurf)</option>
                <option value="150" {% if action.resolution|int == 150 %}selected{% endif %}>150 DPI (Schnell)</option>
                <option value="300" {% if action.resolution|int == 300 %}selected{% endif %}>300 DPI (Standard / OCR)</option>
                <option value="600" {% if action.resolution|int == 600 %}selected{% endif %}>600 DPI (Hohe Qualität)</option>
            </select>
        </div>
        <div class="form-group">
            <label>Unterordner für diese Taste (Optional):</label>
            <div style="display: flex; align-items: center; gap: 5px;">
                <span class="main-path-prefix" style="font-family: monospace; font-size: 13px; color: #718096; white-space: nowrap;">{{ scan_cfg.default_save_dir }}/</span>
                <input type="text" name="save_dir_{{ id }}" value="{{ action.save_dir }}" maxlength="64" placeholder="z. B. Rechnungen" pattern="[a-zA-Z0-9äöüÄÖÜß_\\-\\s/]*">
            </div>
            <div class="hint">Freilassen speichert direkt im Hauptordner. Max. 64 Zeichen (Buchstaben, Zahlen, Leerzeichen, -, _). Keine Pfadtraversierung ('..').</div>
        </div>
    </div>
    {% endfor %}

    <button type="submit" class="btn-submit">Scan-Einstellungen Validieren & Speichern</button>
</form>
"""

BODY_DAEMON_CONFIG = """
<h2>⚙️ Daemon Hardware & Netzwerk-Einstellungen</h2>
<form method="POST" action="/save_daemon">
    <div class="card">
        <h3>Netzwerk-Parameter</h3>
        <div class="form-group">
            <label>Scanner IP-Adresse:</label>
            <input type="text" name="scanner_ip" value="{{ daemon_cfg.scanner_ip }}" required>
        </div>
        <div class="form-group">
            <label>Anzeigename auf dem Scanner-Display (ClientName):</label>
            <input type="text" name="client_name" value="{{ daemon_cfg.client_name }}" required>
        </div>
        <div class="form-group">
            <label>Event TCP Port:</label>
            <input type="number" name="event_port" value="{{ daemon_cfg.event_port }}" required>
        </div>
        <div class="form-group">
            <label>SLP UDP Port:</label>
            <input type="number" name="slp_port" value="{{ daemon_cfg.slp_port }}" required>
        </div>
        <div class="form-group">
            <label>Multicast Gruppe:</label>
            <input type="text" name="mcast_grp" value="{{ daemon_cfg.mcast_grp }}" required>
        </div>
    </div>
    <button type="submit" class="btn-submit">Daemon-Einstellungen Speichern</button>
</form>
"""

BODY_SCANNER_INFO = """
<h2>🔍 Scanner Hardware-Details & Status</h2>

<div class="card">
    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 15px;">
        <div>
            <strong>Scanner-IP:</strong> <code>{{ scanner_ip }}</code>
        </div>
        {% if online %}
            <span class="status-badge online">● ONLINE</span>
        {% else %}
            <span class="status-badge offline">● OFFLINE / NICHT ERREICHBAR</span>
        {% endif %}
    </div>

    {% if online %}
        <p class="hint" style="margin-bottom: 20px;">Aktive Hardware-Parameter und unterstützte Sensor-Modi (gefiltert via SANE epson2 Backend):</p>
        
        {% for section in parsed_sections %}
        <div class="card" style="background: #ffffff; margin-bottom: 15px;">
            <h3 style="margin-bottom: 10px;">{{ section.title }}</h3>
            <div class="opt-grid">
                {% for opt in section.options %}
                <div class="opt-card">
                    <div class="opt-header">
                        <span class="opt-name">--{{ opt.name }}</span>
                        {% if opt.value %}
                        <span class="opt-badge">{{ opt.value }}</span>
                        {% endif %}
                    </div>
                    {% if opt.spec %}
                    <div class="opt-spec">Bereich/Werte: {{ opt.spec }}</div>
                    {% endif %}
                    {% if opt.description %}
                    <div class="opt-desc">{{ opt.description }}</div>
                    {% endif %}
                </div>
                {% endfor %}
            </div>
        </div>
        {% endfor %}

        <details style="margin-top: 20px; font-size: 13px; color: #4a5568;">
            <summary style="cursor: pointer; font-weight: 600; padding: 8px 0;">💻 Ungefilterte SANE-Rohausgabe anzeigen (Low-Level Debug)</summary>
            <textarea class="log-area" readonly style="height: 250px; background-color: #0d1117; color: #58a6ff; margin-top: 10px;">{{ raw_info }}</textarea>
        </details>
    {% else %}
        <div class="alert-banner warning" style="margin-bottom: 0;">
            <span>⚠️ <strong>Scanner nicht erreichbar:</strong> Das Gerät unter IP <code>{{ scanner_ip }}</code> antwortet nicht im Netzwerk. Bitte prüfen Sie, ob der Scanner eingeschaltet und im WLAN/LAN erreichbar ist.</span>
        </div>
    {% endif %}
</div>

<a href="/scanner-info" class="btn-submit" style="display: inline-block; text-align: center; text-decoration: none; width: auto; background: #4a5568;">🔄 Status erneut abfragen</a>
"""

BODY_LOG_VIEW = """
<h2>📋 Log-Anzeige: {{ service_name }}</h2>
<p class="hint">Es werden die letzten 50 Log-Einträge angezeigt.</p>

<textarea class="log-area" readonly>{{ log_content }}</textarea>

<a href="{{ download_url }}" class="btn-download">💾 Vollständiges Logfile herunterladen</a>
"""

PAGE_SCAN_CONFIG = BASE_LAYOUT.replace("__CONTENT__", BODY_SCAN_CONFIG)
PAGE_DAEMON_CONFIG = BASE_LAYOUT.replace("__CONTENT__", BODY_DAEMON_CONFIG)
PAGE_SCANNER_INFO = BASE_LAYOUT.replace("__CONTENT__", BODY_SCANNER_INFO)
PAGE_LOG_VIEW = BASE_LAYOUT.replace("__CONTENT__", BODY_LOG_VIEW)


def load_json(path):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

def get_last_log_lines(filepath, max_lines=50):
    if not os.path.exists(filepath):
        fallback_path = os.path.expanduser(f"~/.config/epson/logs/{os.path.basename(filepath)}")
        if os.path.exists(fallback_path):
            filepath = fallback_path
        else:
            return f"[INFO] Log-Datei '{filepath}' existiert noch nicht."

    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            lines = deque(f, maxlen=max_lines)
            return "".join(lines)
    except Exception as e:
        return f"[ERROR] Fehler beim Lesen der Log-Datei: {e}"


@app.route("/api/browse", methods=["GET"])
def browse_fs():
    req_path = request.args.get("path", "/srv/scans").strip()
    if not os.path.isabs(req_path):
        req_path = "/srv/scans"

    req_path = os.path.abspath(req_path)
    
    if not os.path.exists(req_path):
        req_path = "/srv" if os.path.exists("/srv") else "/"

    subdirs = []
    try:
        with os.scandir(req_path) as entry_iter:
            for entry in entry_iter:
                if entry.is_dir(follow_symlinks=False) and not entry.name.startswith("."):
                    writable, _ = check_path_writable(entry.path)
                    if writable:
                        subdirs.append({
                            "name": entry.name,
                            "path": entry.path
                        })
    except PermissionError:
        return jsonify({"error": f"Zugriff verweigert auf {req_path}", "current": req_path, "parent": None, "dirs": []})

    subdirs.sort(key=lambda x: x["name"].lower())
    
    parent_path = os.path.dirname(req_path) if req_path != "/" else None
    if parent_path:
        parent_writable, _ = check_path_writable(parent_path)
        if not parent_writable:
            parent_path = None

    return jsonify({
        "current": req_path,
        "parent": parent_path,
        "dirs": subdirs
    })


@app.route("/", methods=["GET"])
def index():
    s_cfg = load_json(SCAN_CONFIG_PATH)
    status = request.args.get("status")
    return render_template_string(PAGE_SCAN_CONFIG, scan_cfg=s_cfg, status=status)


@app.route("/daemon-config", methods=["GET"])
def daemon_config():
    d_cfg = load_json(DAEMON_CONFIG_PATH)
    status = request.args.get("status")
    return render_template_string(PAGE_DAEMON_CONFIG, daemon_cfg=d_cfg, status=status)


@app.route("/scanner-info", methods=["GET"])
def scanner_info():
    d_cfg = load_json(DAEMON_CONFIG_PATH)
    scanner_ip = d_cfg.get("scanner_ip", "")
    
    online = is_scanner_online(scanner_ip)
    raw_info = ""
    parsed_sections = []
    
    if online:
        success, raw_info = query_sane_info(scanner_ip)
        if success:
            parsed_sections = parse_sane_info(raw_info)
        else:
            online = False

    return render_template_string(
        PAGE_SCANNER_INFO,
        scanner_ip=scanner_ip,
        online=online,
        parsed_sections=parsed_sections,
        raw_info=raw_info
    )


@app.route("/logs/daemon", methods=["GET"])
def view_daemon_log():
    logs = get_last_log_lines(DAEMON_LOG_PATH, 50)
    return render_template_string(
        PAGE_LOG_VIEW,
        service_name="Epson Scanner Daemon",
        log_content=logs,
        download_url="/logs/daemon/download"
    )


@app.route("/logs/daemon/download", methods=["GET"])
def download_daemon_log():
    path = DAEMON_LOG_PATH
    if not os.path.exists(path):
        path = os.path.expanduser("~/.config/epson/logs/daemon.log")
    if os.path.exists(path):
        return send_file(path, as_attachment=True, download_name="epson_daemon.log")
    return "Logdatei nicht gefunden", 404


@app.route("/logs/web", methods=["GET"])
def view_web_log():
    logs = get_last_log_lines(ACTUAL_WEB_LOG, 50)
    return render_template_string(
        PAGE_LOG_VIEW,
        service_name="Web Config Service",
        log_content=logs,
        download_url="/logs/web/download"
    )


@app.route("/logs/web/download", methods=["GET"])
def download_web_log():
    if os.path.exists(ACTUAL_WEB_LOG):
        return send_file(ACTUAL_WEB_LOG, as_attachment=True, download_name="epson_web.log")
    return "Logdatei nicht gefunden", 404


@app.route("/save_scan", methods=["POST"])
def save_scan():
    s_cfg = load_json(SCAN_CONFIG_PATH)
    default_dir = request.form["default_save_dir"].strip()

    ok, msg_or_path = check_path_writable(default_dir)
    if not ok:
        s_cfg["default_save_dir"] = default_dir
        return render_template_string(
            PAGE_SCAN_CONFIG, 
            scan_cfg=s_cfg, 
            error_msg=f"{msg_or_path} Der epsonscan-Benutzer benötigt Schreibrechte auf diesen Server-Ordner."
        )

    s_cfg["default_save_dir"] = default_dir

    for push_id in list(s_cfg.get("push_actions", {}).keys()):
        if f"label_{push_id}" in request.form:
            raw_subfolder = request.form[f"save_dir_{push_id}"]
            
            valid_sub, clean_sub, error_sub = sanitize_subfolder(raw_subfolder)
            if not valid_sub:
                return render_template_string(
                    PAGE_SCAN_CONFIG, 
                    scan_cfg=s_cfg, 
                    error_msg=f"Fehler bei Taste {push_id}: {error_sub}"
                )

            if clean_sub:
                target_check = os.path.join(default_dir, clean_sub)
                ok_custom, msg_custom = check_path_writable(target_check)
                if not ok_custom:
                    return render_template_string(
                        PAGE_SCAN_CONFIG, 
                        scan_cfg=s_cfg, 
                        error_msg=f"Taste {push_id}: {msg_custom}"
                    )

            s_cfg["push_actions"][push_id] = {
                "label": request.form[f"label_{push_id}"],
                "format": request.form[f"format_{push_id}"],
                "mode": request.form[f"mode_{push_id}"],
                "resolution": int(request.form[f"resolution_{push_id}"]),
                "save_dir": clean_sub
            }

    save_json(SCAN_CONFIG_PATH, s_cfg)
    app.logger.info("Scan-Profile wurden erfolgreich sanitiert, validiert und gespeichert.")
    return redirect("/?status=scan_saved")


@app.route("/save_daemon", methods=["POST"])
def save_daemon():
    d_cfg = load_json(DAEMON_CONFIG_PATH)
    d_cfg["scanner_ip"] = request.form["scanner_ip"]
    d_cfg["client_name"] = request.form["client_name"]
    d_cfg["event_port"] = int(request.form["event_port"])
    d_cfg["slp_port"] = int(request.form["slp_port"])
    d_cfg["mcast_grp"] = request.form["mcast_grp"]

    save_json(DAEMON_CONFIG_PATH, d_cfg)
    app.logger.info("Daemon-Konfiguration wurde über die Web-Oberfläche aktualisiert.")
    return redirect("/daemon-config?status=daemon_saved")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
