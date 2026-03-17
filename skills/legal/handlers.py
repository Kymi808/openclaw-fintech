"""
OpenClaw skill handlers for the Legal & Compliance Agent.
CRITICAL: All contract analysis uses LOCAL LLM only.
"""
import json
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Optional
import httpx

from skills.shared import get_logger, audit_log

logger = get_logger("legal.handlers")

DATA_DIR = Path("./workspaces/legal-agent/data")
CONTRACTS_FILE = DATA_DIR / "contracts.json"
SEC_STATE_FILE = DATA_DIR / "sec_state.json"
SCAN_RESULTS_FILE = DATA_DIR / "scan_results.json"

DISCLAIMER = "⚖️ This is an AI-generated summary, not legal advice. Consult qualified counsel."

# SEC EDGAR base URL
SEC_EDGAR_BASE = "https://efts.sec.gov/LATEST/search-index?q="
SEC_EDGAR_FILINGS = "https://data.sec.gov/submissions/CIK{cik}.json"


def _ensure_data_dir():
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def _load_contracts() -> list[dict]:
    if CONTRACTS_FILE.exists():
        return json.loads(CONTRACTS_FILE.read_text())
    return []


def _save_contracts(contracts: list[dict]):
    _ensure_data_dir()
    CONTRACTS_FILE.write_text(json.dumps(contracts, indent=2))


def _load_sec_state() -> dict:
    if SEC_STATE_FILE.exists():
        return json.loads(SEC_STATE_FILE.read_text())
    return {
        "tracked_entities": [],  # [{cik, name, last_filing_date}]
        "seen_filings": [],
    }


def _save_sec_state(state: dict):
    _ensure_data_dir()
    SEC_STATE_FILE.write_text(json.dumps(state, indent=2))


async def analyze_contract(document_path: str) -> dict:
    """
    Analyze a contract document using LOCAL LLM (Ollama).
    NEVER sends document content to cloud APIs.
    """
    logger.info(f"Analyzing contract: {document_path}")

    # Read document content
    doc_path = Path(document_path)
    if not doc_path.exists():
        return {"error": f"Document not found: {document_path}"}

    # For PDF files, we'd use a PDF extraction library (PyMuPDF, pdfplumber)
    # For now, handle text-based files
    if doc_path.suffix == ".pdf":
        # In production: extract text with pdfplumber
        # import pdfplumber
        # with pdfplumber.open(doc_path) as pdf:
        #     text = "\n".join(page.extract_text() for page in pdf.pages)
        content_note = "PDF extraction required — install pdfplumber"
    else:
        content_note = doc_path.read_text()

    # Send to LOCAL Ollama instance — NEVER to cloud
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(
                "http://localhost:11434/api/generate",
                json={
                    "model": "llama3.1:70b",
                    "prompt": (
                        "You are a legal document analyst. Analyze this contract and provide:\n"
                        "1. PARTIES: Who are the parties involved?\n"
                        "2. TYPE: What type of agreement is this?\n"
                        "3. EFFECTIVE DATE and EXPIRATION DATE\n"
                        "4. KEY TERMS: List the most important terms in plain language\n"
                        "5. OBLIGATIONS: What must each party do?\n"
                        "6. RISK FLAGS: Flag any concerning clauses (indemnification, "
                        "liability caps, auto-renewal, non-compete, IP assignment, "
                        "governing law issues, missing standard clauses)\n"
                        "7. KEY DATES: List all important deadlines\n\n"
                        f"CONTRACT TEXT:\n{content_note}"
                    ),
                    "stream": False,
                },
            )
            resp.raise_for_status()
            analysis = resp.json().get("response", "Analysis failed")

    except httpx.ConnectError:
        return {
            "error": (
                "Local LLM (Ollama) is not running. "
                "Contract analysis REQUIRES a local model for confidentiality. "
                "Start Ollama with: ollama serve"
            )
        }
    except Exception as e:
        return {"error": f"Analysis failed: {e}"}

    # Track the contract
    contracts = _load_contracts()
    contract_entry = {
        "id": f"CTR-{len(contracts) + 1:06d}",
        "filename": doc_path.name,
        "analyzed_at": datetime.now(timezone.utc).isoformat(),
        "summary": analysis[:500],  # Store only summary, not full text
    }
    contracts.append(contract_entry)
    _save_contracts(contracts)

    audit_log("legal-agent", "contract_analyzed", {
        "contract_id": contract_entry["id"],
        "filename": doc_path.name,
        "method": "local_llm",
    })

    msg = f"📄 Contract Analysis: {doc_path.name}\n\n{analysis}\n\n{DISCLAIMER}"
    return {"contract_id": contract_entry["id"], "analysis": analysis, "message": msg}


async def check_sec_filings() -> list[dict]:
    """
    Check SEC EDGAR for new filings from tracked entities.
    Uses the free SEC EDGAR API (no auth required, just User-Agent).
    """
    import os
    user_agent = os.getenv("SEC_EDGAR_USER_AGENT", "FinTechBot admin@example.com")

    sec_state = _load_sec_state()
    tracked = sec_state.get("tracked_entities", [])

    if not tracked:
        return [{
            "message": "No SEC entities configured. Add CIK numbers to track.",
        }]

    new_filings = []

    async with httpx.AsyncClient(
        timeout=15.0,
        headers={"User-Agent": user_agent, "Accept": "application/json"},
    ) as client:
        for entity in tracked:
            cik = str(entity["cik"]).zfill(10)
            try:
                resp = await client.get(
                    f"https://data.sec.gov/submissions/CIK{cik}.json"
                )
                resp.raise_for_status()
                data = resp.json()

                company_name = data.get("name", entity.get("name", "Unknown"))
                recent = data.get("filings", {}).get("recent", {})

                forms = recent.get("form", [])
                dates = recent.get("filingDate", [])
                accessions = recent.get("accessionNumber", [])
                descriptions = recent.get("primaryDocDescription", [])

                # Check last 10 filings
                monitored_forms = {"10-K", "10-Q", "8-K", "S-1", "DEF 14A", "13F-HR"}
                seen = set(sec_state.get("seen_filings", []))

                for i in range(min(10, len(forms))):
                    accession = accessions[i] if i < len(accessions) else ""
                    if accession in seen:
                        continue
                    if forms[i] not in monitored_forms:
                        continue

                    filing = {
                        "company": company_name,
                        "cik": entity["cik"],
                        "form": forms[i],
                        "date": dates[i] if i < len(dates) else "",
                        "accession": accession,
                        "description": descriptions[i] if i < len(descriptions) else "",
                        "url": (
                            f"https://www.sec.gov/Archives/edgar/data/"
                            f"{entity['cik']}/{accession.replace('-', '')}"
                        ),
                        "is_material_event": forms[i] == "8-K",
                    }
                    new_filings.append(filing)
                    seen.add(accession)

                sec_state["seen_filings"] = list(seen)[-500:]  # Keep last 500

            except Exception as e:
                logger.error(f"SEC filing check failed for CIK {entity['cik']}: {e}")

    _save_sec_state(sec_state)

    # Format messages
    for filing in new_filings:
        priority = "🚨" if filing["is_material_event"] else "📋"
        filing["message"] = (
            f"{priority} SEC Filing Alert\n"
            f"Entity: {filing['company']}\n"
            f"Filing: {filing['form']}\n"
            f"Date: {filing['date']}\n"
            f"Description: {filing['description']}\n"
            f"Link: {filing['url']}\n\n"
            f"{DISCLAIMER}"
        )

        audit_log("legal-agent", "sec_filing_detected", {
            "company": filing["company"],
            "form": filing["form"],
            "date": filing["date"],
            "is_material": filing["is_material_event"],
        })

    return new_filings


async def gdpr_scan(url: str) -> dict:
    """
    Scan a website for GDPR/privacy compliance issues.
    Checks for cookie consent, privacy policy, encryption, trackers.
    """
    logger.info(f"GDPR scanning: {url}")

    issues = []

    async with httpx.AsyncClient(
        timeout=15.0,
        follow_redirects=True,
        headers={"User-Agent": "FinTechBot-ComplianceScanner/1.0"},
    ) as client:
        try:
            resp = await client.get(url)
            html = resp.text.lower()
            headers = resp.headers

            # Check 1: HTTPS
            if not url.startswith("https"):
                issues.append({
                    "severity": "HIGH",
                    "issue": "Site not using HTTPS",
                    "location": url,
                    "remediation": "Enable HTTPS with a valid SSL certificate",
                })

            # Check 2: Cookie consent banner
            cookie_indicators = [
                "cookie-consent", "cookie-banner", "cookieconsent",
                "gdpr-consent", "cookie-notice", "cookie-policy",
                "consent-banner", "onetrust", "cookiebot",
            ]
            has_cookie_consent = any(ind in html for ind in cookie_indicators)
            if not has_cookie_consent:
                issues.append({
                    "severity": "HIGH",
                    "issue": "No cookie consent banner detected",
                    "location": "All pages",
                    "remediation": "Implement a GDPR-compliant cookie consent mechanism",
                })

            # Check 3: Privacy policy link
            privacy_indicators = ["privacy-policy", "privacy policy", "/privacy", "datenschutz"]
            has_privacy = any(ind in html for ind in privacy_indicators)
            if not has_privacy:
                issues.append({
                    "severity": "HIGH",
                    "issue": "No privacy policy link detected",
                    "location": "Footer / site-wide",
                    "remediation": "Add a clearly visible link to your privacy policy",
                })

            # Check 4: Third-party trackers
            tracker_domains = [
                "google-analytics", "googletagmanager", "facebook.net",
                "doubleclick", "hotjar", "mixpanel", "segment.com",
                "amplitude", "fullstory",
            ]
            found_trackers = [t for t in tracker_domains if t in html]
            if found_trackers and not has_cookie_consent:
                issues.append({
                    "severity": "HIGH",
                    "issue": f"Third-party trackers without consent: {', '.join(found_trackers)}",
                    "location": "Page scripts",
                    "remediation": "Block trackers until user grants consent",
                })

            # Check 5: Unencrypted forms
            if "<form" in html and "action=\"http:" in html:
                issues.append({
                    "severity": "MEDIUM",
                    "issue": "Form submitting data over unencrypted HTTP",
                    "location": "Contact/input forms",
                    "remediation": "Ensure all form actions use HTTPS",
                })

            # Check 6: Security headers
            security_headers = {
                "strict-transport-security": "HSTS header",
                "x-content-type-options": "X-Content-Type-Options",
                "x-frame-options": "X-Frame-Options",
                "content-security-policy": "Content-Security-Policy",
            }
            for header, name in security_headers.items():
                if header not in headers:
                    issues.append({
                        "severity": "LOW",
                        "issue": f"Missing security header: {name}",
                        "location": "HTTP headers",
                        "remediation": f"Add {name} header to server configuration",
                    })

        except Exception as e:
            return {"error": f"Scan failed: {e}"}

    # Sort by severity
    severity_order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    issues.sort(key=lambda x: severity_order.get(x["severity"], 3))

    # Format report
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    lines = [
        f"🔍 GDPR Compliance Scan — {url}",
        f"Scan date: {now}",
        f"",
        f"Issues Found: {len(issues)}",
        f"| Severity | Issue | Location |",
        f"|----------|-------|----------|",
    ]
    for issue in issues:
        lines.append(
            f"| {issue['severity']:<8} | {issue['issue'][:40]} | {issue['location'][:15]} |"
        )

    if issues:
        lines.append(f"\nRemediation steps:")
        for i, issue in enumerate(issues, 1):
            lines.append(f"{i}. [{issue['severity']}] {issue['remediation']}")

    lines.append(f"\n{DISCLAIMER}")

    # Save results
    _ensure_data_dir()
    scan_result = {
        "url": url,
        "date": now,
        "issues": issues,
        "issue_count": len(issues),
    }
    results = []
    if SCAN_RESULTS_FILE.exists():
        results = json.loads(SCAN_RESULTS_FILE.read_text())
    results.append(scan_result)
    results = results[-100:]  # Keep last 100 scans
    SCAN_RESULTS_FILE.write_text(json.dumps(results, indent=2))

    audit_log("legal-agent", "gdpr_scan_completed", {
        "url": url,
        "issues_found": len(issues),
        "high_severity": sum(1 for i in issues if i["severity"] == "HIGH"),
    })

    return {
        "url": url,
        "issues": issues,
        "issue_count": len(issues),
        "message": "\n".join(lines),
    }


async def check_contract_renewals() -> list[dict]:
    """Check for contracts expiring within 30 days."""
    contracts = _load_contracts()
    now = datetime.now(timezone.utc)
    alerts = []

    for contract in contracts:
        expiry_str = contract.get("expiration_date")
        if not expiry_str:
            continue

        try:
            expiry = datetime.fromisoformat(expiry_str)
            days_until = (expiry - now).days

            if days_until <= 0:
                alerts.append({
                    "contract_id": contract["id"],
                    "filename": contract.get("filename", ""),
                    "status": "EXPIRED",
                    "days": days_until,
                    "message": f"🔴 EXPIRED: {contract.get('filename', contract['id'])} expired {abs(days_until)} days ago",
                })
            elif days_until <= 7:
                alerts.append({
                    "contract_id": contract["id"],
                    "filename": contract.get("filename", ""),
                    "status": "URGENT",
                    "days": days_until,
                    "message": f"🟠 URGENT: {contract.get('filename', contract['id'])} expires in {days_until} days",
                })
            elif days_until <= 30:
                alerts.append({
                    "contract_id": contract["id"],
                    "filename": contract.get("filename", ""),
                    "status": "WARNING",
                    "days": days_until,
                    "message": f"🟡 WARNING: {contract.get('filename', contract['id'])} expires in {days_until} days",
                })
        except (ValueError, TypeError):
            continue

    if alerts:
        for alert in alerts:
            audit_log("legal-agent", "contract_renewal_alert", {
                "contract_id": alert["contract_id"],
                "status": alert["status"],
                "days_until_expiry": alert["days"],
            })

    return alerts


async def legal_research(topic: str, jurisdiction: str = "US Federal") -> dict:
    """
    Research legal precedents on a topic.
    Step 1: Search CourtListener for real court opinions
    Step 2: Use local LLM to synthesize findings with verified citations
    """
    logger.info(f"Legal research: {topic} ({jurisdiction})")

    # Step 1: Search CourtListener for real case law
    from .courtlistener import search_opinions, format_search_results

    court_results = None
    try:
        court_results = await search_opinions(
            query=topic,
            limit=10,
        )
    except Exception as e:
        logger.warning(f"CourtListener search failed: {e}")

    # Build context from real cases for the LLM
    case_context = ""
    if court_results and court_results.opinions:
        case_lines = []
        for op in court_results.opinions[:5]:
            case_lines.append(
                f"- {op.case_name} ({op.court}, {op.date_filed}): {op.summary[:200]}"
            )
        case_context = (
            "\n\nThe following real court cases were found via CourtListener. "
            "Reference these in your analysis where relevant:\n"
            + "\n".join(case_lines)
        )

    # Step 2: Use local LLM to synthesize with real citations
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(
                "http://localhost:11434/api/generate",
                json={
                    "model": "llama3.1:70b",
                    "prompt": (
                        f"You are a legal research assistant. Analyze the following topic:\n"
                        f"Topic: {topic}\n"
                        f"Jurisdiction: {jurisdiction}\n"
                        f"{case_context}\n\n"
                        f"Provide:\n"
                        f"1. Key legal principles applicable\n"
                        f"2. Relevant statutes and regulations\n"
                        f"3. Notable case precedents — use the real cases provided above "
                        f"when relevant, and clearly mark any other citations as "
                        f"'[UNVERIFIED - needs manual confirmation]'\n"
                        f"4. Regulatory guidance or agency opinions\n"
                        f"5. Practical implications\n\n"
                        f"Flag any jurisdiction limitations."
                    ),
                    "stream": False,
                    "options": {"temperature": 0.2},
                },
            )
            resp.raise_for_status()
            analysis = resp.json().get("response", "Analysis failed")

    except httpx.ConnectError:
        # If Ollama is down, still return the CourtListener results
        if court_results and court_results.opinions:
            msg = format_search_results(court_results)
            msg += (
                "\n\n⚠️ Local LLM unavailable — showing raw search results only.\n"
                "Start Ollama for synthesized analysis: ollama serve\n\n"
                f"{DISCLAIMER}"
            )
            return {
                "topic": topic,
                "jurisdiction": jurisdiction,
                "court_results": len(court_results.opinions),
                "message": msg,
            }
        return {"error": "Local LLM (Ollama) not running and CourtListener returned no results."}
    except Exception as e:
        return {"error": f"Research failed: {e}"}

    # Combine results
    audit_log("legal-agent", "legal_research", {
        "topic": topic,
        "jurisdiction": jurisdiction,
        "court_results": len(court_results.opinions) if court_results else 0,
    })

    msg_parts = [
        f"📚 Legal Research: {topic}",
        f"Jurisdiction: {jurisdiction}",
        f"",
        analysis,
    ]

    # Append verified court citations
    if court_results and court_results.opinions:
        msg_parts.append("\n--- Verified Court Citations (CourtListener) ---")
        for op in court_results.opinions[:5]:
            msg_parts.append(f"• {op.case_name} — {op.court}, {op.date_filed}")
            msg_parts.append(f"  {op.url}")

    msg_parts.append(f"\n{DISCLAIMER}")

    return {
        "topic": topic,
        "jurisdiction": jurisdiction,
        "analysis": analysis,
        "verified_citations": len(court_results.opinions) if court_results else 0,
        "message": "\n".join(msg_parts),
    }
