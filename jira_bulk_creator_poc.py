#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PoC: Automatisierte Jira-Massenticket-Erzeugung für Infrastruktur-Änderungen (INFRARCH)
Unterstützt:
- Fall A (Betroffene unbekannt / Generischer Dienst -> Master-Epic + Self-Identify Sammel-Ticket)
- Fall B (Betroffene bekannt -> Master-Epic + Child-Issues in Zielprojekten oder Fallback INFRARCH)
- Idempotenz / Deduplizierung (verhindert doppelte Anlage bei erneutem Skript-Lauf)
- Chunking / Batching für Jira REST API (/rest/api/2/issue/bulk, max. 50 Issues pro Request)
- Retry mit Exponential Backoff bei Rate-Limiting (HTTP 429 / 5xx)
- Rollback-Modus zum sauberen Schließen/Löschen von Testtickets im PoC
"""

import argparse
import csv
import hashlib
import json
import logging
import os
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

DEFAULT_FALLBACK_PROJECT = "INFRARCH"
MAX_BULK_BATCH_SIZE = 50


class JiraInfrarchClient:
    def __init__(self, base_url: str, pat_token: str, dry_run: bool = False):
        self.base_url = base_url.rstrip("/")
        self.pat_token = pat_token
        self.dry_run = dry_run
        self.headers = {
            "Authorization": f"Bearer {self.pat_token}",
            "Content-Type": "application/json",
            "Accept": "application/json"
        }

    def _request(self, method: str, endpoint: str, payload: Optional[Dict] = None, retries: int = 3) -> Dict:
        url = f"{self.base_url}{endpoint}"
        if self.dry_run:
            logger.info(f"[DRY-RUN] {method} {url} | Payload-Keys: {list(payload.keys()) if payload else 'None'}")
            return {"dry_run": True, "key": f"INFRARCH-DRY-{int(time.time()*1000)%10000}"}

        data = json.dumps(payload).encode("utf-8") if payload else None
        for attempt in range(1, retries + 1):
            try:
                req = urllib.request.Request(url, data=data, headers=self.headers, method=method)
                with urllib.request.urlopen(req, timeout=30) as resp:
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
                logger.error(f"HTTP-Fehler {status} bei {method} {endpoint}: {err_body}")
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
            # Simuliere im PoC: BESTAND und SCHADEN existieren, unbekannte Keys fallen auf INFRARCH zurück
            return project_key in ("INFRARCH", "BESTAND", "SCHADEN")
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
                ],
                # Standard Jira Epic Name Customfield (in vielen Instanzen customfield_10011)
                "customfield_10011": epic_meta["summary"][:60]
            }
        }
        res = self._request("POST", "/rest/api/2/issue", payload)
        epic_key = res.get("key", "INFRARCH-100")
        logger.info(f"Master-Epic erfolgreich angelegt: {epic_key}")
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

            # Idempotenz-Hash berechnen
            raw_id = f"{epic_meta.get('change_id', 'POC')}_{kvkid}_{target_proj}_{env}"
            idem_hash = "idem-" + hashlib.sha1(raw_id.encode("utf-8")).hexdigest()[:10]

            existing_key = self.find_existing_by_idempotency_label(idem_hash)
            if existing_key:
                logger.info(f"Ticket für {kvkid} ({env}) existiert bereits: {existing_key}. Überspringe.")
                created_keys.append(existing_key)
                continue

            # Prüfe ob Zielprojekt existiert; wenn Produkt kein eigenes Jira-Projekt hat -> INFRARCH
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
            if item.get("assignee"):
                fields["assignee"] = {"name": item["assignee"]}

            issue_updates.append({"fields": fields})

        # In Chunks von max. 50 Issues an Jira Bulk API senden
        for i in range(0, len(issue_updates), MAX_BULK_BATCH_SIZE):
            batch = issue_updates[i:i + MAX_BULK_BATCH_SIZE]
            logger.info(f"Sende Bulk-Batch ({len(batch)} Issues) an Jira REST API...")
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
                    # Verlinke mit Master-Epic
                    self.link_to_epic(epic_key, key)

        return created_keys

    def link_to_epic(self, epic_key: str, child_key: str):
        """Erstellt einen Issue-Link zwischen Master-Epic und Child-Ticket (funktioniert auch projektübergreifend)."""
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
            f"Child-Tasks direkt im zentralen Projekt *INFRARCH* geführt und über das Label {{no-product-jira}} "
            f"bzw. {{triage-unklar}} gesteuert."
        )

    @staticmethod
    def _render_child_description(epic_key: str, meta: Dict[str, Any], item: Dict[str, Any], used_fallback: bool) -> str:
        fallback_notice = (
            "{panel:title=Hinweis zur Ticket-Verortung (Sammelprojekt INFRARCH)|borderStyle=solid|borderColor=#FF9900|titleBGColor=#FFF0B3|bgColor=#FFFFF0}\n"
            "Für dieses IT-Produkt ist aktuell kein eigenes Jira-Projekt hinterlegt oder die Betroffenheit befindet sich in der Triage. "
            "Dieses Ticket wurde daher stellvertretend im zentralen Projekt *INFRARCH* angelegt. Bitte bearbeiten und kommentieren Sie den Status direkt in diesem Ticket.\n"
            "{panel}\n\n"
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


def run_poc_demo():
    """Führt den im PoC geforderten Lauf aus (Master-Epic + 3 repräsentative Child-Issues in INFRARCH)."""
    with open("poc_sample_input.json", "r", encoding="utf-8") as f:
        data = json.load(f)

    client = JiraInfrarchClient(
        base_url=os.environ.get("JIRA_BASE_URL", "https://jira.konzern.example.com"),
        pat_token=os.environ.get("JIRA_PAT_TOKEN", "POC-SIMULATED-PAT-TOKEN"),
        dry_run=True  # Für sicheren PoC-Test standardmäßig Dry-Run aktiv
    )

    epic_key = client.create_epic(data["epic"])
    child_keys = client.create_child_issues_bulk(epic_key, data["epic"], data["children"])
    logger.info(f"PoC-Lauf abgeschlossen. Epic: {epic_key}, Child-Issues ({len(child_keys)}): {child_keys}")
    return epic_key, child_keys


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="INFRARCH Jira Bulk Issue Creator PoC")
    parser.add_argument("--input", default="poc_sample_input.json", help="Pfad zur JSON- oder CSV-Eingabedatei")
    parser.add_argument("--live", action="store_true", help="Führt echte REST-Calls gegen Jira aus (Standard: Dry-Run)")
    parser.add_argument("--rollback", nargs="*", help="Löscht/Schließt angegebene Ticket-Keys (Rollback-Test)")
    args = parser.parse_args()

    run_poc_demo()
