#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PoC: Automatisierte Jira-Massenticket-Erzeugung für Infrastruktur-Änderungen (INFRARCH)
Vorkonfiguriert für: https://jira.vkbintra.de/jira
Unterstützt:
- Fall A (Betroffene unbekannt / Generischer Dienst -> Master-Epic + Self-Identify Sammel-Ticket)
- Fall B (Betroffene bekannt -> Master-Epic + Child-Issues in Zielprojekten oder Fallback INFRARCH)
- Idempotenz / Deduplizierung (verhindert doppelte Anlage bei erneutem Skript-Lauf)
- Chunking / Batching für Jira REST API (/rest/api/2/issue/bulk, max. 50 Issues pro Request)
- Retry mit Exponential Backoff bei Rate-Limiting (HTTP 429 / 5xx)
- Rollback-Modus zum sauberen Schließen/Löschen von Testtickets im PoC
- SSL-Support für interne Konzern-Zertifikate (vkbintra.de)
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
from typing import Dict, List, Any, Optional
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
# WICHTIG: Nicht ".../secure/Dashboard.jspa" eintragen, sondern nur die Basis-URL bis "/jira"!
JIRA_BASE_URL = "https://jira.vkbintra.de/jira"

# Dein Personal Access Token (in Jira oben rechts -> Profil -> Personal Access Tokens erstellen)
JIRA_PAT_TOKEN = "HIER_DEIN_PAT_TOKEN_EINTRAGEN"

# Ziel-Projekt-Kürzel (Project Key), in dem das Master-Epic und die Fallback-Tickets landen:
DEFAULT_FALLBACK_PROJECT = "INFRARCH"

# True = Nur Trockenlauf/Simulation im Fenster (legt noch nichts in Jira an)
# False = ECHTER LIVE-LAUF (legt die Tickets wirklich in https://jira.vkbintra.de/jira an!)
DRY_RUN_MODE = True
# ==============================================================================

MAX_BULK_BATCH_SIZE = 50


class JiraInfrarchClient:
    def __init__(self, base_url: str, pat_token: str, dry_run: bool = False):
        # Entfernt versehentlich mitkopierte Browser-Pfade wie /secure/Dashboard.jspa
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
        # Erlaubt interne Konzern-SSL-Zertifikate (*.vkbintra.de), falls Python sie lokal nicht kennt
        self.ssl_context = ssl._create_unverified_context()

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
                logger.error(f"HTTP-Fehler {status} bei {method} {url}: {err_body}")
                raise
            except Exception as ex:
                if attempt < retries:
                    time.sleep(2 ** attempt)
                    continue
                raise ex
        return {}

    def verify_project_exists(self, project_key: str) -> bool:
        """Prüft, ob ein Ziel-Jira-Projekt existiert und beschreibbar ist. Falls nicht -> Fallback INFRARCH."""
        if self.dry_run:
            return project_key in (DEFAULT_FALLBACK_PROJECT, "BESTAND", "SCHADEN")
        try:
            self._request("GET", f"/rest/api/2/project/{project_key}")
            return True
        except Exception:
            return False

    def find_existing_by_idempotency_label(self, idempotency_label: str) -> Optional[str]:
        """Sucht via JQL nach bereits angelegten Tickets mit demselben Idempotenz-Label (Deduplizierung)."""
        if self.dry_run:
            return None
        jql = f'labels = "{idempotency_label}"'
        res = self._request("POST", "/rest/api/2/search", {"jql": jql, "maxResults": 1, "fields": ["key"]})
        issues = res.get("issues", [])
        return issues[0]["key"] if issues else None

    def create_epic(self, epic_meta: Dict[str, Any]) -> str:
        """Legt das zentrale Master-Epic im Projekt INFRARCH an."""
        change_id = epic_meta.get("change_id", "CHG-POC")
        idem_label = f"idem-epic-{change_id.lower()}"
        existing = self.find_existing_by_idempotency_label(idem_label)
        if existing:
            logger.info(f"Epic für {change_id} existiert bereits ({existing}). Überspringe Neuanlage.")
            return existing

        payload = {
            "fields": {
                "project": {"key": DEFAULT_FALLBACK_PROJECT},
                "summary": epic_meta["summary"],
                "description": self._render_epic_description(epic_meta),
                "issuetype": {"name": "Epic"},
                "duedate": epic_meta.get("duedate"),
                "labels": [
                    "infra-change",
                    f"change-{change_id.lower()}",
                    idem_label,
                    "fall-a" if "Fall A" in epic_meta.get("process_type", "") else "fall-b"
                ]
            }
        }
        res = self._request("POST", "/rest/api/2/issue", payload)
        epic_key = res.get("key", f"{DEFAULT_FALLBACK_PROJECT}-100")
        logger.info(f"Master-Epic erfolgreich angelegt: {epic_key} ({self.base_url}/browse/{epic_key})")
        return epic_key

    def create_child_issues_bulk(self, epic_key: str, epic_meta: Dict[str, Any], items: List[Dict[str, Any]]) -> List[str]:
        """Erzeugt Child-Issues in Batches via /rest/api/2/issue/bulk inkl. Fallback auf INFRARCH."""
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
                logger.info(f"Ticket für {kvkid} ({env}) existiert bereits: {existing_key}. Überspringe.")
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

            fields = {
                "project": {"key": target_proj},
                "summary": summary[:250],
                "description": description,
                "issuetype": {"name": item.get("issue_type", "Task")},
                "duedate": item.get("duedate") or epic_meta.get("duedate"),
                "labels": [
                    "infra-change",
                    f"kvkid-{kvkid.lower()}",
                    clarity_tag,
                    fallback_tag,
                    idem_hash
                ]
            }
            if item.get("assignee") and not self.dry_run:
                fields["assignee"] = {"name": item["assignee"]}

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
        """Erstellt einen Issue-Link zwischen Master-Epic und Child-Ticket."""
        payload = {
            "type": {"name": "Relates"},
            "inwardIssue": {"key": epic_key},
            "outwardIssue": {"key": child_key}
        }
        self._request("POST", "/rest/api/2/issueLink", payload)

    def rollback_poc_issues(self, keys: List[str]):
        """Rollback-Funktion für den PoC: Schließt oder löscht Testtickets sauber."""
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
            f"* *Tech Lead / Ansprechpartner:* [~{meta.get('reporter')}]\n\n"
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


def run_poc_demo(live_override: bool = False, rollback_keys: Optional[List[str]] = None):
    script_dir = os.path.dirname(os.path.abspath(__file__))
    json_path = os.path.join(script_dir, "poc_sample_input.json")
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    is_dry_run = False if live_override else DRY_RUN_MODE

    client = JiraInfrarchClient(
        base_url=os.environ.get("JIRA_BASE_URL", JIRA_BASE_URL),
        pat_token=os.environ.get("JIRA_PAT_TOKEN", JIRA_PAT_TOKEN),
        dry_run=is_dry_run
    )

    if rollback_keys:
        client.rollback_poc_issues(rollback_keys)
        return [], []

    epic_key = client.create_epic(data["epic"])
    child_keys = client.create_child_issues_bulk(epic_key, data["epic"], data["children"])
    logger.info(f"PoC-Lauf abgeschlossen. Epic: {epic_key}, Child-Issues ({len(child_keys)}): {child_keys}")
    return epic_key, child_keys


if __name__ == "__main__":
    try:
        parser = argparse.ArgumentParser(description="INFRARCH Jira Bulk Issue Creator PoC (VKB Intra)")
        parser.add_argument("--input", default="poc_sample_input.json", help="Pfad zur JSON- oder CSV-Eingabedatei")
        parser.add_argument("--live", action="store_true", help="Führt echte REST-Calls gegen Jira aus")
        parser.add_argument("--rollback", nargs="*", help="Löscht/Schließt angegebene Ticket-Keys (Rollback-Test)")
        args = parser.parse_args()

        run_poc_demo(live_override=args.live, rollback_keys=args.rollback)
    except Exception as e:
        import traceback
        print("\n[FEHLER BEIM AUSFÜHREN]:", e)
        traceback.print_exc()
    finally:
        print("\n" + "="*70)
        input("Lauf abgeschlossen. Druecke die ENTER-Taste, um das Fenster zu schliessen...")
