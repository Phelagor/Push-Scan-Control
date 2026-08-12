#!/usr/bin/env python3
import json
import logging
import os
import re
import socket
import struct
import subprocess
import sys
import threading
import time

DAEMON_CONFIG_PATH = os.environ.get("EPSON_DAEMON_CONFIG", "/etc/epson/daemon_config.json")
SCAN_CONFIG_PATH = os.environ.get("EPSON_SCAN_CONFIG", "/etc/epson/scan_config.json")
LOG_FILE_PATH = os.environ.get("EPSON_DAEMON_LOG", "/var/log/epson/daemon.log")


def setup_logging():
    log_dir = os.path.dirname(LOG_FILE_PATH)
    try:
        os.makedirs(log_dir, exist_ok=True)
        target_log = LOG_FILE_PATH
    except PermissionError:
        fallback_dir = os.path.expanduser("~/.config/epson/logs")
        os.makedirs(fallback_dir, exist_ok=True)
        target_log = os.path.join(fallback_dir, "daemon.log")

    file_handler = logging.FileHandler(target_log, encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))

    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)


setup_logging()


def custom_thread_excepthook(args):
    logging.error(
        f"Unbehandelte Thread-Exception in '{args.thread.name}': "
        f"{args.exc_type.__name__}: {args.exc_value}",
        exc_info=(args.exc_type, args.exc_value, args.exc_traceback)
    )

threading.excepthook = custom_thread_excepthook


class EpsonDaemon:
    def __init__(self):
        self.daemon_cfg = {}
        self.last_daemon_mtime = 0
        self.slp_sock = None
        self.tcp_sock = None
        self.running = True
        self.load_daemon_config()

    def load_daemon_config(self):
        try:
            if os.path.exists(DAEMON_CONFIG_PATH):
                self.last_daemon_mtime = os.path.getmtime(DAEMON_CONFIG_PATH)
                with open(DAEMON_CONFIG_PATH, "r", encoding="utf-8") as f:
                    self.daemon_cfg = json.load(f)
                logging.info(f"Daemon-Konfiguration geladen aus {DAEMON_CONFIG_PATH}")
        except Exception as e:
            logging.error(f"Fehler beim Laden der Daemon-Konfiguration: {e}", exc_info=True)

    def check_daemon_config_change(self):
        try:
            if not os.path.exists(DAEMON_CONFIG_PATH):
                return
            current_mtime = os.path.getmtime(DAEMON_CONFIG_PATH)
            if current_mtime > self.last_daemon_mtime:
                logging.info("Änderung in daemon_config.json erkannt. Reinitialisiere Sockets...")
                self.load_daemon_config()
                self.restart_listeners()
        except Exception as e:
            logging.error(f"Fehler bei der Konfigurationsprüfung: {e}", exc_info=True)

    def get_local_ip(self, target_ip):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect((target_ip, 80))
            return s.getsockname()[0]
        finally:
            s.close()

    def build_slp_attr_reply(self, xid, hostname, local_ip, event_port):
        attr_str = f"(ClientName={hostname}),(IPAddress={local_ip}),(EventPort={event_port})"
        attr_bytes = attr_str.encode("ascii")
        payload = b"\x00\x00" + struct.pack(">H", len(attr_bytes)) + attr_bytes + b"\x00"
        total_len = 16 + len(payload)
        len_bytes = struct.pack(">I", total_len)[1:]
        header = b"\x02\x07" + len_bytes + b"\x00\x00\x00\x00\x00" + xid + b"\x00\x02en"
        return header + payload

    def slp_listener(self):
        scanner_ip = self.daemon_cfg.get("scanner_ip")
        if not scanner_ip:
            return
        local_ip = self.get_local_ip(scanner_ip)

        try:
            self.slp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            self.slp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if hasattr(socket, "SO_REUSEPORT"):
                try:
                    self.slp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
                except Exception:
                    pass

            self.slp_sock.bind(("", self.daemon_cfg["slp_port"]))
            mreq = struct.pack("4sl", socket.inet_aton(self.daemon_cfg["mcast_grp"]), socket.INADDR_ANY)
            self.slp_sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)

            logging.info(f"SLP-Listener aktiv auf Multicast {self.daemon_cfg['mcast_grp']}:{self.daemon_cfg['slp_port']}")

            while self.running:
                try:
                    data, addr = self.slp_sock.recvfrom(2048)
                except OSError as e:
                    if e.errno in (22, 9, 10038) or not self.running:
                        logging.info("SLP-Listener geordnet beendet.")
                        break
                    raise e

                if addr[0] == scanner_ip and len(data) >= 12:
                    if data[0] == 0x02 and data[1] == 0x06:
                        xid = data[10:12]
                        reply = self.build_slp_attr_reply(
                            xid, self.daemon_cfg["client_name"], local_ip, self.daemon_cfg["event_port"]
                        )
                        self.slp_sock.sendto(reply, (scanner_ip, self.daemon_cfg["slp_port"]))
        except Exception as e:
            if self.running:
                logging.error(f"SLP Listener Fehler: {e}", exc_info=True)

    def build_soap_response(self):
        soap_body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">'
            '<s:Body>'
            '<p:PushScanResponse xmlns:p="http://schema.epson.net/EpsonNet/Scan/2004/pushscan">'
            '<p:Response>0</p:Response>'
            '</p:PushScanResponse>'
            '</s:Body>'
            '</s:Envelope>'
        )
        body_bytes = soap_body.encode("utf-8")
        http_header = (
            "HTTP/1.0 200 OK\r\n"
            "Content-Type: application/soap+xml; charset=utf-8\r\n"
            f"Content-Length: {len(body_bytes)}\r\n"
            "Connection: close\r\n\r\n"
        )
        return http_header.encode("ascii") + body_bytes

    def execute_sane_scan(self, push_id):
        scan_cfg = {}
        try:
            if os.path.exists(SCAN_CONFIG_PATH):
                with open(SCAN_CONFIG_PATH, "r", encoding="utf-8") as f:
                    scan_cfg = json.load(f)
        except Exception as e:
            logging.error(f"Konnte {SCAN_CONFIG_PATH} nicht lesen: {e}")

        default_save_dir = scan_cfg.get("default_save_dir", "/srv/scans")
        push_actions = scan_cfg.get("push_actions", {})
        action = push_actions.get(push_id, {
            "format": "jpeg",
            "mode": "Color",
            "resolution": 300,
            "save_dir": ""
        })

        custom_dir = action.get("save_dir", "").strip()
        target_dir = custom_dir if custom_dir else default_save_dir
        save_dir = os.path.expanduser(target_dir)

        try:
            os.makedirs(save_dir, exist_ok=True)
        except PermissionError:
            logging.error(f"Keine Schreibrechte für Verzeichnis: '{save_dir}'. Bitte Ordnerrechte prüfen!")
            return
        except Exception as e:
            logging.error(f"Fehler beim Erstellen von Verzeichnis '{save_dir}': {e}", exc_info=True)
            return

        scanner_ip = self.daemon_cfg["scanner_ip"]
        ext = "jpg" if action["format"] == "jpeg" else action["format"]
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        output_file = os.path.join(save_dir, f"scan_{timestamp}_{push_id}.{ext}")

        device_name = f"epson2:net:{scanner_ip}"
        logging.info(f"Starte Scan - PushScanID: {push_id} ({action.get('label', 'Default')}) -> Ordner: {save_dir}")

        cmd = [
            "scanimage",
            f"--device-name={device_name}",
            f"--format={action['format']}",
            f"--mode={action['mode']}",
            f"--resolution={action['resolution']}",
            f"--output-file={output_file}"
        ]

        try:
            res = subprocess.run(cmd, capture_output=True, text=True)
            if res.returncode == 0:
                logging.info(f"Scan erfolgreich gespeichert: {output_file}")
            else:
                logging.error(f"SANE Fehler:\n{res.stderr}")
        except Exception as e:
            logging.error(f"Fehler bei Ausführung von scanimage: {e}", exc_info=True)

    def handle_client(self, conn, addr):
        try:
            conn.settimeout(5.0)
            request_data = b""

            while b"\r\n\r\n" not in request_data:
                chunk = conn.recv(2048)
                if not chunk:
                    break
                request_data += chunk

            header_part, _, body_part = request_data.partition(b"\r\n\r\n")

            content_length = 0
            for line in header_part.decode("latin-1", errors="ignore").split("\r\n"):
                if line.lower().startswith("content-length:"):
                    content_length = int(line.split(":")[1].strip())
                    break

            while len(body_part) < content_length:
                chunk = conn.recv(2048)
                if not chunk:
                    break
                body_part += chunk

            full_payload = (header_part + b"\r\n\r\n" + body_part).decode("utf-8", errors="ignore")

            conn.sendall(self.build_soap_response())
            time.sleep(0.3)

            match = re.search(r"<PushScanIDIn>([^<]+)</PushScanIDIn>", full_payload)
            push_id = match.group(1).strip() if match else "01"
            logging.info(f"Event empfangen. PushScanID: '{push_id}'")

            threading.Thread(target=self.execute_sane_scan, args=(push_id,), daemon=True).start()
        except Exception as e:
            logging.error(f"Fehler beim Verarbeiten des Event-Requests: {e}", exc_info=True)
        finally:
            conn.close()

    def tcp_listener(self):
        scanner_ip = self.daemon_cfg.get("scanner_ip")
        if not scanner_ip:
            return

        try:
            self.tcp_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.tcp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if hasattr(socket, "SO_REUSEPORT"):
                try:
                    self.tcp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
                except Exception:
                    pass

            self.tcp_sock.bind(("0.0.0.0", self.daemon_cfg["event_port"]))
            self.tcp_sock.listen(5)

            logging.info(f"TCP-Event-Listener aktiv auf Port {self.daemon_cfg['event_port']}")

            while self.running:
                try:
                    conn, addr = self.tcp_sock.accept()
                except OSError as e:
                    if e.errno in (22, 9, 10038) or not self.running:
                        logging.info("TCP-Listener geordnet beendet.")
                        break
                    raise e

                if addr[0] == scanner_ip:
                    threading.Thread(target=self.handle_client, args=(conn, addr), daemon=True).start()
                else:
                    conn.close()
        except Exception as e:
            if self.running:
                logging.error(f"TCP Listener Fehler: {e}", exc_info=True)

    def stop_listeners(self):
        if self.tcp_sock:
            try:
                self.tcp_sock.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            try:
                self.tcp_sock.close()
            except Exception:
                pass
            self.tcp_sock = None

        if self.slp_sock:
            try:
                self.slp_sock.close()
            except Exception:
                pass
            self.slp_sock = None

    def restart_listeners(self):
        self.stop_listeners()
        time.sleep(0.5)
        threading.Thread(target=self.slp_listener, daemon=True).start()
        threading.Thread(target=self.tcp_listener, daemon=True).start()

    def start(self):
        self.restart_listeners()
        try:
            while self.running:
                self.check_daemon_config_change()
                time.sleep(1)
        except KeyboardInterrupt:
            self.running = False
            self.stop_listeners()


if __name__ == "__main__":
    daemon = EpsonDaemon()
    daemon.start()
