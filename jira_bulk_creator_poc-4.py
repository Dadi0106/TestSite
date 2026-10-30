#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PoC: Automatisierte Jira-Massenticket-Erzeugung für Infrastruktur-Änderungen (INFRARCH)
Vorkonfiguriert für: https://jira.vkbintra.de/jira
Unterstützt als Input:
1. JSON-Datei (.json) -> enthält Epic-Metadaten + Child-Issues
2. CSV-Datei (.csv)   -> enthält die Child-Issues (Tabellenform aus Excel exportiert)
3. Excel-Datei (.xlsx)-> liest direkt Reiter '1_Epic_Intake' und '2_Bulk_Issues_Daten' (falls openpyxl installiert)
"""

import argparse
import csv
import hashlib
import json
import logging
import os
import ssl
import sys
import time
from typing import Dict, List, Any, Optional, Tuple
import urllib.request
import urllib.error

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("INFRARCH-Bulk-Creator")

# ==============================================================================
# HIER KONFIGURIEREN FÜR DEN TEST IM UNTERNEHMENS-JIRA:
# ==============================================================================
JIRA_BASE_URL = "https://jira.vkbintra.de/jira"

# Dein Personal Access Token (aus Jira -> Profil -> Personal Access Tokens)
JIRA_PAT_TOKEN = "HIER_DEIN_PAT_TOKEN_EINTRAGEN"

# Ziel-Projekt-Kürzel (Project Key), z. B. "INFRARCH"
DEFAULT_FALLBACK_PROJECT = "INFRARCH"

# Eingabedatei: Kann auf ".json", ".csv" oder ".xlsx" gesetzt werden!
DEFAULT_INPUT_FILE = "poc_sample_input.json"

# True = Nur Trockenlauf/Simulation im Fenster
# False = ECHTER LIVE-LAUF gegen https://jira.vkbintra.de/jira
DRY_RUN_MODE = True
# ==============================================================================

MAX_BULK_BATCH_SIZE = 50


def load_input_data(file_path: str, script_dir: str) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """
    Lädt die Eingabedaten flexibel aus einer JSON-, CSV- oder Excel-Datei (.xlsx).
    Gibt ein Tupel zurück: (epic_meta_dict, list_of_children_dicts)
    """
    if not os.path.isabs(file_path):
        file_path = os.path.join(script_dir, file_path)

    ext = os.path.splitext(file_path)[1].lower()
    logger.info(f"Lade Eingabedaten aus Datei: {os.path.basename(file_path)} (Format: {ext})")

    # Standard-Epic-Metadaten als Fallback (falls nur eine reine CSV-Tabelle mit Child-Issues geladen wird)
    default_epic = {
        "process_type": "Fall B: Betroffene bekannt (inkl. Triage & Sammelprojekt-Fallback)",
        "change_id": "CHG-2026-8841",
        "summary": "[INFRA-CHANGE] Ablösung Core-Firewall & Subnetz-Routing RZ München",
        "domain": "Netzwerk / Firewall",
        "environment": "PROD, STAGE",
        "change_window": "2026-11-28 22:00 bis 2026-11-29 06:00",
        "duedate": "2026-11-15",
        "risk": "Hoch",
        "reporter": "david.geisberger",
        "test_req": "Verpflichtender Verbindungs- und Smoke-Test im Wartungsfenster",
        "description": "Im Rahmen der Netzwerk-Modernisierung werden die zentralen Firewall-Cluster migriert."
    }

    # 1. JSON-Format (Enthält 'epic' und 'children')
    if ext == ".json":
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("epic", default_epic), data.get("children", [])

    # 2. CSV-Format (Semikolon- oder Komma-getrennte Tabelle der Child-Issues)
    elif ext == ".csv":
        # Falls neben der CSV auch die JSON liegt, holen wir uns daraus die Epic-Kopfdaten
        json_fallback = os.path.join(script_dir, "poc_sample_input.json")
        epic_meta = default_epic
        if os.path.exists(json_fallback):
            try:
                with open(json_fallback, "r", encoding="utf-8") as jf:
                    epic_meta = json.load(jf).get("epic", default_epic)
            except Exception:
                pass

        children = []
        with open(file_path, "r", encoding="utf-8-sig") as f:
            sample = f.read(2048)
            f.seek(0)
            delimiter = ";" if ";" in sample else ","
            reader = csv.DictReader(f, delimiter=delimiter)
            for row in reader:
                # Unterstützt sowohl die englischen Keys aus poc_sample_input.csv als auch deutsche Excel-Spaltennamen
                kvkid = row.get("kvkid") or row.get("KVKID_Service_ID") or ""
                if not kvkid.strip():
                    continue
                children.append({
                    "kvkid": kvkid.strip(),
                    "app_name": (row.get("app_name") or row.get("Anwendungsname") or "").strip(),
                    "target_project": (row.get("target_project") or row.get("Ziel_Jira_Projekt") or DEFAULT_FALLBACK_PROJECT).strip(),
                    "issue_type": (row.get("issue_type") or row.get("Issue_Type") or "Task").strip(),
                    "clarity": (row.get("clarity") or row.get("Betroffenheit_Klarheit") or "Gesichert betroffen").strip(),
                    "environment": (row.get("environment") or row.get("Umgebung") or "PROD").strip(),
                    "objects": (row.get("objects") or row.get("Betroffene_Objekte_Hosts_IPs") or "").strip(),
                    "assignee": (row.get("assignee") or row.get("PO_SO_Assignee") or "").strip(),
                    "contact_email": (row.get("contact_email") or row.get("Kontakt_Email") or "").strip(),
                    "risk": (row.get("risk") or row.get("Risiko_Einschaetzung") or "Mittel").strip(),
                    "duedate": (row.get("duedate") or row.get("Rueckmelde_Frist") or epic_meta.get("duedate")).strip(),
                    "change_window": (row.get("change_window") or row.get("Change_Fenster") or epic_meta.get("change_window")).strip(),
                    "instruction": (row.get("instruction") or row.get("Spezifische_Handlungsanweisung") or "").strip()
                })
        logger.info(f"{len(children)} Zeilen erfolgreich aus CSV-Datei eingelesen.")
        return epic_meta, children

    # 3. Direktes Excel-Format (.xlsx)
    elif ext == ".xlsx":
        try:
            import openpyxl
        except ImportError:
            raise RuntimeError("Zum direkten Lesen von .xlsx wird 'openpyxl' benötigt. Bitte speichere das Excel als .csv ab oder nutze die .json Datei.")
        wb = openpyxl.load_workbook(file_path, data_only=True)
        epic_meta = default_epic.copy()
        if "1_Epic_Intake" in wb.sheetnames:
            ws1 = wb["1_Epic_Intake"]
            epic_meta["process_type"] = str(ws1["C4"].value or epic_meta["process_type"])
            epic_meta["summary"] = str(ws1["C5"].value or epic_meta["summary"])
            epic_meta["change_id"] = str(ws1["C7"].value or epic_meta["change_id"])
            epic_meta["domain"] = str(ws1["C8"].value or epic_meta["domain"])
            epic_meta["environment"] = str(ws1["C9"].value or epic_meta["environment"])
            epic_meta["change_window"] = str(ws1["C10"].value or epic_meta["change_window"])
            epic_meta["duedate"] = str(ws1["C11"].value or epic_meta["duedate"])[:10]
            epic_meta["risk"] = str(ws1["C12"].value or epic_meta["risk"])
            epic_meta["test_req"] = str(ws1["C13"].value or epic_meta["test_req"])
            epic_meta["reporter"] = str(ws1["C14"].value or epic_meta["reporter"])
            epic_meta["description"] = str(ws1["C15"].value or epic_meta["description"])

        children = []
        if "2_Bulk_Issues_Daten" in wb.sheetnames:
            ws2 = wb["2_Bulk_Issues_Daten"]
            for row in ws2.iter_rows(min_row=4, values_only=True):
                if not row or not row[2]:
                    continue
                children.append({
                    "kvkid": str(row[2]).strip(),
                    "app_name": str(row[3] or "").strip(),
                    "target_project": str(row[4] or DEFAULT_FALLBACK_PROJECT).strip(),
                    "issue_type": str(row[5] or "Task").strip(),
                    "clarity": str(row[6] or "Gesichert betroffen").strip(),
                    "environment": str(row[7] or "PROD").strip(),
                    "objects": str(row[8] or "").strip(),
                    "assignee": str(row[9] or "").strip(),
                    "contact_email": str(row[10] or "").strip(),
                    "risk": str(row[11] or "Mittel").strip(),
                    "duedate": str(row[12] or epic_meta["duedate"])[:10],
                    "change_window": str(row[13] or epic_meta["change_window"]).strip(),
                    "instruction": str(row[14] or "").strip()
                })
        logger.info(f"Excel-Datei erfolgreich gelesen: Epic '{epic_meta['change_id']}' + {len(children)} Child-Issues.")
        return epic_meta, children

    else:
        raise ValueError(f"Nicht unterstütztes Dateiformat: {ext}. Bitte .json, .csv oder .xlsx verwenden.")


class JiraInfrarchClient:
    def __init__(self, base_url: str, pat_token: str, dry_run: bool = False):
        if "/secure/" in base_url:
            base_url = base_url.split("/secure/")[0]
        self.base_url = base_url.rstrip("/")
        self.pat_token = pat_token
        self.dry_run = dry_run
        self.headers = {
            "Authorization": f"Bearer {self.pat_token}",
            "Content-Type": "application/json",
            "Accept": "application/json"
        }
        self.ssl_context = ssl._create_unverified_context()
        self._epic_name_field_id = None
        self._epic_link_field_id = None
        self._link_type_name = None
        self._project_issue_types = {}

    def _request(self, method: str, endpoint: str, payload: Optional[Dict] = None, retries: int = 3) -> Dict:
        url = f"{self.base_url}{endpoint}"
        if self.dry_run:
            logger.info(f"[DRY-RUN] {method} {url} | Payload-Keys: {list(payload.keys()) if payload else 'None'}")
            return {"dry_run": True, "key": f"{DEFAULT_FALLBACK_PROJECT}-DRY-{int(time.time()*1000)%10000}"}

        data = json.dumps(payload).encode("utf-8") if payload else None
        for attempt in range(1, retries + 1):
            try:
                req = urllib.request.Request(url, data=data, headers=self.headers, method=method)
                with urllib.request.urlopen(req, timeout=30, context=self.ssl_context) as resp:
                    body = resp.read().decode("utf-8")
                    return json.loads(body) if body else {}
            except urllib.error.HTTPError as e:
                status = e.code
                err_body = e.read().decode("utf-8", errors="replace")
                if status in (429, 502, 503, 504) and attempt < retries:
                    wait = 2 ** attempt
                    logger.warning(f"HTTP {status} erhalten. Retry {attempt}/{retries} in {wait}s...")
                    time.sleep(wait)
                    continue
                logger.error(f"\n--- JIRA API FEHLER (HTTP {status}) bei {method} {url} ---")
                logger.error(f"Antwort vom Jira-Server: {err_body}")
                logger.error("----------------------------------------------------------\n")
                raise
            except Exception as ex:
                if attempt < retries:
                    time.sleep(2 ** attempt)
                    continue
                raise ex
        return {}

    def verify_project_exists(self, project_key: str) -> bool:
        if self.dry_run:
            return project_key in (DEFAULT_FALLBACK_PROJECT, "BESTAND", "SCHADEN")
        try:
            proj_data = self._request("GET", f"/rest/api/2/project/{project_key}")
            issue_types = [it.get("name") for it in proj_data.get("issueTypes", []) if not it.get("subtask", False)]
            self._project_issue_types[project_key] = issue_types
            logger.info(f"Projekt '{project_key}' gefunden. Verfügbare Issue-Typen: {issue_types}")
            return True
        except Exception:
            return False

    def resolve_issue_type(self, project_key: str, preferred_type: str) -> str:
        if self.dry_run:
            return preferred_type
        if project_key not in self._project_issue_types:
            self.verify_project_exists(project_key)
        available = self._project_issue_types.get(project_key, [])
        if not available:
            return preferred_type

        if preferred_type in available:
            return preferred_type

        synonyms = {
            "Epic": ["Epic", "Epos", "Initiative", "Feature", "Aufgabe", "Task", "Story"],
            "Task": ["Task", "Aufgabe", "Story", "Anforderung", "Change", "Vorgang"]
        }
        for candidate in synonyms.get(preferred_type, [preferred_type]):
            for avail in available:
                if candidate.lower() == avail.lower():
                    logger.info(f"Nutze Vorgangstyp '{avail}' (statt '{preferred_type}') im Projekt {project_key}.")
                    return avail

        fallback_it = available[0]
        logger.warning(f"Vorgangstyp '{preferred_type}' existiert in {project_key} nicht. Nutze stattdessen '{fallback_it}'.")
        return fallback_it

    def _discover_custom_fields(self):
        if self.dry_run:
            self._epic_name_field_id = "customfield_11475"
            self._epic_link_field_id = "customfield_11474"
            return
        if self._epic_name_field_id is not None:
            return

        self._epic_name_field_id = ""
        self._epic_link_field_id = ""
        try:
            fields = self._request("GET", "/rest/api/2/field")
            for f in fields:
                fname = (f.get("name") or "").lower()
                schema_custom = (f.get("schema") or {}).get("custom", "")
                if "gh-epic-label" in schema_custom or fname in ("epic name", "epos-name", "epic-name", "epos name"):
                    self._epic_name_field_id = f.get("id")
                    logger.info(f"Custom-Field für Epic-Name erkannt: {self._epic_name_field_id} ({f.get('name')})")
                elif "gh-epic-link" in schema_custom or fname in ("epic link", "epos-verknüpfung", "epos-link", "epic-link"):
                    self._epic_link_field_id = f.get("id")
                    logger.info(f"Custom-Field für Epos-Verknüpfung (Epic Link) erkannt: {self._epic_link_field_id} ({f.get('name')})")
        except Exception as e:
            logger.warning(f"Konnte Custom-Fields nicht abfragen: {e}")

    def get_valid_issue_link_type(self) -> str:
        if self.dry_run:
            return "Relates"
        if self._link_type_name:
            return self._link_type_name
        try:
            res = self._request("GET", "/rest/api/2/issueLinkType")
            link_types = res.get("issueLinkTypes", [])
            available_names = [lt.get("name") for lt in link_types if lt.get("name")]
            logger.info(f"Verfügbare Verknüpfungstypen in Jira: {available_names}")

            preferred = ["Relates", "Bezieht sich auf", "Verknüpfung", "Reference", "Abhängigkeit", "Blocks", "Blockiert"]
            for pref in preferred:
                for avail in available_names:
                    if pref.lower() in avail.lower():
                        self._link_type_name = avail
                        logger.info(f"Nutze Vorgangsverknüpfungstyp: '{self._link_type_name}'")
                        return self._link_type_name

            if available_names:
                self._link_type_name = available_names[0]
                return self._link_type_name
        except Exception as e:
            logger.warning(f"Konnte Verknüpfungstypen nicht abfragen: {e}")
        return "Relates"

    def find_existing_by_idempotency_label(self, idempotency_label: str) -> Optional[str]:
        if self.dry_run:
            return None
        jql = f'labels = "{idempotency_label}"'
        try:
            res = self._request("POST", "/rest/api/2/search", {"jql": jql, "maxResults": 1, "fields": ["key"]})
            issues = res.get("issues", [])
            return issues[0]["key"] if issues else None
        except Exception:
            return None

    def create_epic(self, epic_meta: Dict[str, Any]) -> str:
        if not self.dry_run and not self.verify_project_exists(DEFAULT_FALLBACK_PROJECT):
            raise RuntimeError(f"Projekt '{DEFAULT_FALLBACK_PROJECT}' existiert nicht oder keine Berechtigung!")

        self._discover_custom_fields()

        change_id = epic_meta.get("change_id", "CHG-POC")
        idem_label = f"idem-epic-{change_id.lower()}"
        existing = self.find_existing_by_idempotency_label(idem_label)
        if existing:
            logger.info(f"Master-Epic für {change_id} existiert bereits ({existing}). Überspringe Neuanlage.")
            return existing

        issue_type_name = self.resolve_issue_type(DEFAULT_FALLBACK_PROJECT, "Epic")

        fields = {
            "project": {"key": DEFAULT_FALLBACK_PROJECT},
            "summary": epic_meta["summary"],
            "description": self._render_epic_description(epic_meta),
            "issuetype": {"name": issue_type_name},
            "labels": [
                "infra-change",
                f"change-{change_id.lower()}",
                idem_label,
                "fall-a" if "Fall A" in epic_meta.get("process_type", "") else "fall-b"
            ]
        }

        if issue_type_name.lower() in ("epic", "epos") and self._epic_name_field_id:
            fields[self._epic_name_field_id] = epic_meta["summary"][:60]

        res = self._request("POST", "/rest/api/2/issue", {"fields": fields})
        epic_key = res.get("key", f"{DEFAULT_FALLBACK_PROJECT}-100")
        logger.info(f"Master-Epic erfolgreich angelegt: {epic_key} ({self.base_url}/browse/{epic_key})")
        return epic_key

    def create_child_issues_bulk(self, epic_key: str, epic_meta: Dict[str, Any], items: List[Dict[str, Any]]) -> List[str]:
        self._discover_custom_fields()
        created_keys = []
        issue_updates = []

        for item in items:
            kvkid = item.get("kvkid", "KVK-UNKNOWN").strip()
            target_proj = item.get("target_project", "").strip() or DEFAULT_FALLBACK_PROJECT
            env = item.get("environment", "PROD").strip()
            clarity = item.get("clarity", "Gesichert betroffen").strip()

            raw_id = f"{epic_meta.get('change_id', 'POC')}_{kvkid}_{target_proj}_{env}"
            idem_hash = "idem-" + hashlib.sha1(raw_id.encode("utf-8")).hexdigest()[:10]

            existing_key = self.find_existing_by_idempotency_label(idem_hash)
            if existing_key:
                logger.info(f"Ticket für {kvkid} ({env}) existiert bereits: {existing_key}. Stelle Verknüpfung zu Epic {epic_key} sicher...")
                if not self.dry_run:
                    self.link_to_epic(epic_key, existing_key)
                created_keys.append(existing_key)
                continue

            used_fallback = False
            if target_proj != DEFAULT_FALLBACK_PROJECT and not self.verify_project_exists(target_proj):
                logger.warning(f"Produkt {kvkid} hat kein gültiges Jira-Projekt ('{target_proj}'). Nutze Fallback '{DEFAULT_FALLBACK_PROJECT}'.")
                target_proj = DEFAULT_FALLBACK_PROJECT
                used_fallback = True
            elif target_proj == DEFAULT_FALLBACK_PROJECT:
                used_fallback = True

            clarity_tag = "triage-unklar" if "Unklar" in clarity else ("self-identify" if "Selbst" in clarity else "betroffen-klar")
            fallback_tag = "no-product-jira" if used_fallback else "native-product-jira"

            summary_prefix = "[INFRA-TRIAGE]" if "Unklar" in clarity else "[INFRA-ACTION]"
            summary = f"{summary_prefix} [{kvkid}] {item.get('app_name', 'Anwendung')} - {epic_meta.get('change_id', 'Change')}"

            description = self._render_child_description(epic_key, epic_meta, item, used_fallback)
            child_issue_type = self.resolve_issue_type(target_proj, item.get("issue_type", "Task"))

            fields = {
                "project": {"key": target_proj},
                "summary": summary[:250],
                "description": description,
                "issuetype": {"name": child_issue_type},
                "labels": [
                    "infra-change",
                    f"kvkid-{kvkid.lower()}",
                    clarity_tag,
                    fallback_tag,
                    idem_hash
                ]
            }

            if self._epic_link_field_id and not self.dry_run:
                fields[self._epic_link_field_id] = epic_key

            issue_updates.append({"fields": fields})

        for i in range(0, len(issue_updates), MAX_BULK_BATCH_SIZE):
            batch = issue_updates[i:i + MAX_BULK_BATCH_SIZE]
            logger.info(f"Sende Bulk-Batch ({len(batch)} Issues) an Jira REST API ({self.base_url}/rest/api/2/issue/bulk)...")
            if self.dry_run:
                for idx, b in enumerate(batch, 1):
                    sim_key = f"{b['fields']['project']['key']}-{200 + i + idx}"
                    created_keys.append(sim_key)
                    logger.info(f"[DRY-RUN] Erzeugt: {sim_key} -> {b['fields']['summary']}")
            else:
                res = self._request("POST", "/rest/api/2/issue/bulk", {"issueUpdates": batch})
                for iss in res.get("issues", []):
                    key = iss.get("key")
                    created_keys.append(key)
                    logger.info(f"[LIVE] Erzeugt: {self.base_url}/browse/{key}")
                    self.link_to_epic(epic_key, key)

        return created_keys

    def link_to_epic(self, epic_key: str, child_key: str):
        self._discover_custom_fields()

        if self._epic_link_field_id and not self.dry_run:
            try:
                self._request("PUT", f"/rest/api/2/issue/{child_key}", {"fields": {self._epic_link_field_id: epic_key}})
                logger.info(f"Epos-Verknüpfung (Epic Link) für {child_key} -> {epic_key} erfolgreich gesetzt!")
            except Exception as e:
                logger.warning(f"Hinweis bei Epos-Verknüpfung für {child_key}: {e}")

        link_type = self.get_valid_issue_link_type()
        payload = {
            "type": {"name": link_type},
            "inwardIssue": {"key": epic_key},
            "outwardIssue": {"key": child_key}
        }
        try:
            self._request("POST", "/rest/api/2/issueLink", payload)
            logger.info(f"Vorgangsverknüpfung ('{link_type}') zwischen {epic_key} und {child_key} erfolgreich erstellt!")
        except Exception as link_err:
            logger.warning(f"Konnte Issue-Link ('{link_type}') für {child_key} nicht setzen: {link_err}")

    def rollback_poc_issues(self, keys: List[str]):
        logger.info(f"Starte Rollback für {len(keys)} PoC-Tickets: {keys}")
        for k in keys:
            if self.dry_run:
                logger.info(f"[DRY-RUN] Rollback (DELETE) für Testticket {k} erfolgreich.")
            else:
                self._request("DELETE", f"/rest/api/2/issue/{k}")
                logger.info(f"Testticket {k} gelöscht.")

    @staticmethod
    def _render_epic_description(meta: Dict[str, Any]) -> str:
        return (
            f"h2. Übersicht Infrastruktur-Änderung ({meta.get('change_id')})\n"
            f"* *Prozessvariante:* {meta.get('process_type')}\n"
            f"* *Infrastruktur-Domäne:* {meta.get('domain')}\n"
            f"* *Betroffene Umgebungen:* {meta.get('environment')}\n"
            f"* *Geplantes Change-Fenster:* {meta.get('change_window')}\n"
            f"* *Rückmelde-Frist (SLA):* {meta.get('duedate')}\n"
            f"* *Risikoklasse:* {meta.get('risk')}\n"
            f"* *Tech Lead / Ansprechpartner:* {meta.get('reporter')}\n\n"
            f"h3. Fachlicher & Technischer Hintergrund\n"
            f"{meta.get('description')}\n\n"
            f"h3. Vorgehen bei fehlendem Produkt-Jira oder unklarer Betroffenheit\n"
            f"Produkte ohne eigenes Jira-Projekt sowie Systeme mit noch unklarer Betroffenheit werden als "
            f"Child-Tasks direkt im zentralen Projekt *{DEFAULT_FALLBACK_PROJECT}* geführt und über das Label {{no-product-jira}} "
            f"bzw. {{triage-unklar}} gesteuert."
        )

    @staticmethod
    def _render_child_description(epic_key: str, meta: Dict[str, Any], item: Dict[str, Any], used_fallback: bool) -> str:
        fallback_notice = (
            f"{{panel:title=Hinweis zur Ticket-Verortung (Sammelprojekt {DEFAULT_FALLBACK_PROJECT})|borderStyle=solid|borderColor=#FF9900|titleBGColor=#FFF0B3|bgColor=#FFFFF0}}\n"
            f"Für dieses IT-Produkt ist aktuell kein eigenes Jira-Projekt hinterlegt oder die Betroffenheit befindet sich in der Triage. "
            f"Dieses Ticket wurde daher stellvertretend im zentralen Projekt *{DEFAULT_FALLBACK_PROJECT}* angelegt. Bitte bearbeiten und kommentieren Sie den Status direkt in diesem Ticket.\n"
            f"{{panel}}\n\n"
        ) if used_fallback else ""

        return (
            f"{fallback_notice}"
            f"h2. Handlungsbedarf wegen Infrastruktur-Anpassung ({meta.get('change_id')})\n"
            f"* *Übergeordnetes Master-Epic:* {epic_key}\n"
            f"* *Service / KVKID:* *{item.get('kvkid')}* ({item.get('app_name')})\n"
            f"* *Status der Betroffenheit:* *{item.get('clarity')}*\n"
            f"* *Betroffene Umgebung:* {item.get('environment')}\n"
            f"* *Betroffene Objekte (Hosts / IPs / Regeln):* {{code}}{item.get('objects')}{{code}}\n"
            f"* *Durchführungsfenster:* {item.get('change_window') or meta.get('change_window')}\n"
            f"* *Rückmelde-Frist (SLA):* *{item.get('duedate') or meta.get('duedate')}*\n"
            f"* *Risikoeinschätzung:* {item.get('risk')}\n\n"
            f"h3. Konkrete Aufgabe (Call-to-Action)\n"
            f"{item.get('instruction')}\n\n"
            f"h3. Geforderte Rückmeldung bis zur Frist\n"
            f"# *Impact & Zeitraum bestätigen:* Ist die Anpassung/Prüfung zum genannten Change-Fenster möglich?\n"
            f"# *Bei unklarer Betroffenheit:* Bitte prüfen Sie Ihre Konfiguration und setzen Sie das Label {{betroffen-bestaetigt}} oder schließen Sie das Ticket mit Kommentar {{Nicht betroffen}}.\n"
            f"# *Abnahmetest:* {meta.get('test_req', 'Smoke-Test nach Durchführung bestätigen.')}"
        )


def run_poc_demo(input_file: str = DEFAULT_INPUT_FILE, live_override: bool = False, rollback_keys: Optional[List[str]] = None):
    script_dir = os.path.dirname(os.path.abspath(__file__))
    epic_meta, children = load_input_data(input_file, script_dir)

    is_dry_run = False if live_override else DRY_RUN_MODE

    client = JiraInfrarchClient(
        base_url=os.environ.get("JIRA_BASE_URL", JIRA_BASE_URL),
        pat_token=os.environ.get("JIRA_PAT_TOKEN", JIRA_PAT_TOKEN),
        dry_run=is_dry_run
    )

    if rollback_keys:
        client.rollback_poc_issues(rollback_keys)
        return [], []

    epic_key = client.create_epic(epic_meta)
    child_keys = client.create_child_issues_bulk(epic_key, epic_meta, children)
    logger.info(f"PoC-Lauf abgeschlossen. Epic: {epic_key}, Child-Issues ({len(child_keys)}): {child_keys}")
    return epic_key, child_keys


if __name__ == "__main__":
    try:
        parser = argparse.ArgumentParser(description="INFRARCH Jira Bulk Issue Creator PoC (VKB Intra)")
        parser.add_argument("--input", default=DEFAULT_INPUT_FILE, help="Pfad zur .json, .csv oder .xlsx Eingabedatei")
        parser.add_argument("--live", action="store_true", help="Führt echte REST-Calls gegen Jira aus")
        parser.add_argument("--rollback", nargs="*", help="Löscht/Schließt angegebene Ticket-Keys (Rollback-Test)")
        args = parser.parse_args()

        run_poc_demo(input_file=args.input, live_override=args.live, rollback_keys=args.rollback)
    except Exception as e:
        import traceback
        print("\n[FEHLER BEIM AUSFÜHREN]:", e)
        traceback.print_exc()
    finally:
        print("\n" + "="*70)
        input("Lauf abgeschlossen. Druecke die ENTER-Taste, um das Fenster zu schliessen...")
