"""AssetGuardian AI Chat Agent — Admin-only conversational interface.

Queries DynamoDB (CMDB, inspection history, warranty cache) and uses
Bedrock Claude to answer natural language questions about assets,
inspections, warranties, ghost assets, compliance, fraud, lifecycle,
procurement, and sustainability — scoped strictly to asset management.
"""

import json
import logging
import os
from datetime import date, datetime, timedelta
from decimal import Decimal

import boto3

logger = logging.getLogger(__name__)

REGION = os.environ.get("AWS_REGION", "ap-southeast-1")
MODEL_ID = os.environ.get("MODEL_ID", "global.anthropic.claude-haiku-4-5-20251001-v1:0")
CMDB_TABLE = os.environ.get("CMDB_TABLE_NAME", "assetguardian-cmdb")
HISTORY_TABLE = os.environ.get("HISTORY_TABLE", "assetguardian-inspection-history")
WARRANTY_TABLE = os.environ.get("WARRANTY_CACHE_TABLE", "assetguardian-warranty-cache")

_dynamodb = boto3.resource("dynamodb", region_name=REGION)
_bedrock = boto3.client("bedrock-runtime", region_name=REGION)

SYSTEM_PROMPT = """You are the AssetGuardian AI Assistant — an admin-only chatbot for enterprise IT asset management.

You have access to LIVE DATA from the organization's asset management system. Use the DATA CONTEXT provided to give accurate, data-driven answers.

YOUR SCOPE (answer ONLY these topics):
1. Ghost Assets — devices not inspected recently, potentially lost/non-existent
2. Unauthorized Transfers — assignment mismatches, wrong employee holding device
3. Warranty Status — active/expired, upcoming expirations, vendor coverage
4. Insurance Evidence — inspection history, damage records, condition timelines
5. Lifecycle Decisions — replacement recommendations, condition-based refresh
6. Compliance & Audit — inspection coverage %, non-compliant devices
7. Employee Disputes — handover vs return condition comparison
8. Procurement Intelligence — which models last longest, failure rates by brand
9. ESG/Sustainability — device reuse rates, e-waste avoidance, lifecycle extension
10. Fraud/Theft — stock photo submissions, serial mismatches, suspicious patterns

RULES:
- Be concise and data-driven. Use numbers, percentages, and lists.
- If the data doesn't contain enough info to answer, say so honestly.
- NEVER answer general knowledge, coding, personal, or off-topic questions.
- NEVER suggest modifying or deleting data — you are read-only.
- If asked something outside scope, respond: "I can only help with asset management questions. Try asking about inspections, warranties, ghost assets, compliance, or fleet health."
- Always relate answers back to actionable insights.
- Format with bullet points for readability.
"""


class DecimalEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, Decimal):
            return float(o) if o % 1 else int(o)
        return super().default(o)


def run(question: str, identity: dict) -> dict:
    """
    Answer an admin's question about asset management.
    
    Args:
        question: Natural language question
        identity: Caller identity (must be admin)
        
    Returns:
        {"answer": str, "data_sources_queried": list}
    """
    if not question or not question.strip():
        return {"answer": "Please ask a question about your assets, inspections, or fleet health.", "data_sources_queried": []}

    # Gather relevant data context based on the question
    data_context, sources = _gather_context(question)

    # Build prompt
    user_prompt = f"""ADMIN QUESTION: {question}

DATA CONTEXT (live data from AssetGuardian system):
{data_context}

Answer the admin's question using the data above. Be specific with numbers and give actionable insights."""

    # Call Bedrock
    try:
        answer = _call_bedrock(user_prompt)
    except Exception as e:
        logger.warning("Chat Bedrock call failed: %s", e)
        answer = f"I couldn't process that question right now. Error: {str(e)[:100]}"

    return {"answer": answer, "data_sources_queried": sources}


def _gather_context(question: str) -> tuple[str, list]:
    """Query DynamoDB tables based on question keywords and build context."""
    q = question.lower()
    context_parts = []
    sources = []

    # Always include fleet summary
    cmdb_summary = _get_cmdb_summary()
    context_parts.append(cmdb_summary)
    sources.append("cmdb")

    # Ghost assets / uninspected
    if any(w in q for w in ["ghost", "uninspect", "never inspect", "missing", "lost", "not inspect", "6 month", "compliance", "audit", "coverage"]):
        context_parts.append(_get_ghost_asset_data())
        sources.append("inspection_history")

    # Warranty
    if any(w in q for w in ["warranty", "expir", "vendor", "hp", "dell", "lenovo", "coverage", "support end"]):
        context_parts.append(_get_warranty_data())
        sources.append("warranty_cache")

    # Damage / condition
    if any(w in q for w in ["damage", "condition", "score", "poor", "excellent", "repair", "replace", "lifecycle", "worst", "best"]):
        context_parts.append(_get_condition_data())
        sources.append("inspection_history")

    # Fraud
    if any(w in q for w in ["fraud", "stock photo", "suspicious", "theft", "stolen", "critical", "mismatch"]):
        context_parts.append(_get_fraud_data())
        sources.append("inspection_history")

    # Employee specific
    if any(w in q for w in ["employee", "emp", "who", "user", "assigned", "person"]):
        context_parts.append(_get_employee_data())
        sources.append("cmdb")

    # Department / procurement
    if any(w in q for w in ["department", "dept", "team", "brand", "model", "procure", "buy", "which laptop", "which model"]):
        context_parts.append(_get_procurement_data())
        sources.append("cmdb")

    # ESG / sustainability
    if any(w in q for w in ["esg", "sustain", "e-waste", "reuse", "circular", "environment", "green", "extend"]):
        context_parts.append(_get_esg_data())
        sources.append("inspection_history")

    # If nothing specific matched, give broad overview
    if len(context_parts) <= 1:
        context_parts.append(_get_ghost_asset_data())
        context_parts.append(_get_condition_data())
        context_parts.append(_get_warranty_data())
        sources = ["cmdb", "inspection_history", "warranty_cache"]

    return "\n\n".join(context_parts), list(set(sources))


def _get_cmdb_summary() -> str:
    """Fleet overview from CMDB."""
    try:
        table = _dynamodb.Table(CMDB_TABLE)
        resp = table.scan(Select="ALL_ATTRIBUTES")
        items = resp.get("Items", [])
        total = len(items)
        
        # Count by status
        statuses = {}
        brands = {}
        types = {}
        for item in items:
            s = item.get("status", "Unknown")
            statuses[s] = statuses.get(s, 0) + 1
            model = item.get("deviceModel", "")
            if "hp" in model.lower() or "elitebook" in model.lower():
                brands["HP"] = brands.get("HP", 0) + 1
            elif "dell" in model.lower() or "latitude" in model.lower():
                brands["Dell"] = brands.get("Dell", 0) + 1
            elif "lenovo" in model.lower() or "thinkpad" in model.lower():
                brands["Lenovo"] = brands.get("Lenovo", 0) + 1
            else:
                brands["Other"] = brands.get("Other", 0) + 1
            if "laptop" in model.lower():
                types["Laptop"] = types.get("Laptop", 0) + 1
            elif "monitor" in model.lower():
                types["Monitor"] = types.get("Monitor", 0) + 1
            elif "phone" in model.lower():
                types["Phone"] = types.get("Phone", 0) + 1
            else:
                types["Other"] = types.get("Other", 0) + 1

        # Warranty stats
        warranty_active = sum(1 for i in items if i.get("warrantyActive"))
        avg_age = sum(int(i.get("deviceAgeMonths", 0)) for i in items) / max(total, 1)

        return f"""FLEET SUMMARY:
- Total assets in CMDB: {total}
- By status: {json.dumps(statuses)}
- By brand: {json.dumps(brands)}
- By type: {json.dumps(types)}
- Warranty active: {warranty_active} ({round(warranty_active/max(total,1)*100)}%)
- Average device age: {round(avg_age)} months"""
    except Exception as e:
        return f"FLEET SUMMARY: Error querying CMDB: {str(e)[:80]}"


def _get_ghost_asset_data() -> str:
    """Find assets not recently inspected."""
    try:
        cmdb_table = _dynamodb.Table(CMDB_TABLE)
        history_table = _dynamodb.Table(HISTORY_TABLE)
        
        cmdb_items = cmdb_table.scan().get("Items", [])
        history_items = history_table.scan().get("Items", [])
        
        # Find which assets have been inspected
        inspected_assets = set()
        latest_inspection = {}
        for h in history_items:
            aid = h.get("assetId", "")
            if aid:
                inspected_assets.add(aid)
                ts = h.get("inspectedAt", "")
                if aid not in latest_inspection or ts > latest_inspection[aid]:
                    latest_inspection[aid] = ts

        # Ghost assets = in CMDB but never inspected or not recently
        never_inspected = []
        stale = []
        six_months_ago = (datetime.utcnow() - timedelta(days=180)).isoformat()
        
        for item in cmdb_items:
            aid = item.get("assetId", "")
            if aid not in inspected_assets:
                never_inspected.append(f"{aid} ({item.get('deviceModel','?')}) assigned to {item.get('assignedUser','?')}")
            elif latest_inspection.get(aid, "") < six_months_ago:
                stale.append(f"{aid} — last inspected: {latest_inspection[aid][:10]}")

        return f"""GHOST ASSET ANALYSIS:
- Total assets in CMDB: {len(cmdb_items)}
- Total inspections recorded: {len(history_items)}
- Assets with at least one inspection: {len(inspected_assets)}
- NEVER inspected: {len(never_inspected)} assets
  {chr(10).join('  • ' + a for a in never_inspected[:15])}
- Not inspected in 6+ months: {len(stale)} assets
  {chr(10).join('  • ' + a for a in stale[:10])}
- Ghost asset rate: {round((len(never_inspected)+len(stale))/max(len(cmdb_items),1)*100)}%"""
    except Exception as e:
        return f"GHOST ASSET ANALYSIS: Error: {str(e)[:80]}"


def _get_warranty_data() -> str:
    """Warranty coverage stats."""
    try:
        cmdb_table = _dynamodb.Table(CMDB_TABLE)
        items = cmdb_table.scan().get("Items", [])
        
        active = [i for i in items if i.get("warrantyActive")]
        expired = [i for i in items if not i.get("warrantyActive")]
        
        return f"""WARRANTY STATUS:
- Warranty active: {len(active)} devices ({round(len(active)/max(len(items),1)*100)}%)
- Warranty expired: {len(expired)} devices
- Top expired (by age): {', '.join(i.get('assetId','?') + ' (' + str(i.get('deviceAgeMonths',0)) + 'mo)' for i in sorted(expired, key=lambda x: int(x.get('deviceAgeMonths',0)), reverse=True)[:5])}"""
    except Exception as e:
        return f"WARRANTY STATUS: Error: {str(e)[:80]}"


def _get_condition_data() -> str:
    """Inspection condition/damage stats."""
    try:
        table = _dynamodb.Table(HISTORY_TABLE)
        items = table.scan().get("Items", [])
        
        if not items:
            return "CONDITION DATA: No inspections recorded yet."
        
        scores = []
        decisions = {}
        for item in items:
            score = item.get("damageScore")
            if score:
                scores.append(int(score))
            decision = item.get("decision", "Unknown")
            decisions[decision] = decisions.get(decision, 0) + 1

        avg_score = round(sum(scores) / max(len(scores), 1), 1) if scores else 0
        worst = sorted(items, key=lambda x: int(x.get("damageScore", 0)), reverse=True)[:5]
        
        return f"""CONDITION & LIFECYCLE DATA:
- Total inspections: {len(items)}
- Average damage score: {avg_score}/100
- Lifecycle decisions: {json.dumps(decisions)}
- Worst condition devices:
  {chr(10).join('  • ' + i.get('assetId','?') + ' — damage score: ' + str(i.get('damageScore','?')) + ', decision: ' + i.get('decision','?') for i in worst)}"""
    except Exception as e:
        return f"CONDITION DATA: Error: {str(e)[:80]}"


def _get_fraud_data() -> str:
    """Fraud detection stats from inspections."""
    try:
        table = _dynamodb.Table(HISTORY_TABLE)
        items = table.scan().get("Items", [])
        
        high_risk = [i for i in items if i.get("fraudRisk") in ("High", "Critical")]
        
        return f"""FRAUD DETECTION DATA:
- Total inspections: {len(items)}
- High/Critical fraud risk: {len(high_risk)} inspections
- Flagged submissions:
  {chr(10).join('  • ' + i.get('assetId','?') + ' by ' + i.get('employeeId','?') + ' — risk: ' + i.get('fraudRisk','?') + ' on ' + (i.get('inspectedAt','')[:10]) for i in high_risk[:10])}"""
    except Exception as e:
        return f"FRAUD DATA: Error: {str(e)[:80]}"


def _get_employee_data() -> str:
    """Employee-asset assignment data."""
    try:
        table = _dynamodb.Table(CMDB_TABLE)
        items = table.scan().get("Items", [])
        
        by_employee = {}
        for item in items:
            emp = item.get("assignedUser", "Unassigned")
            if emp not in by_employee:
                by_employee[emp] = []
            by_employee[emp].append(item.get("assetId", "?") + " (" + item.get("deviceModel", "?") + ")")
        
        multi = {k: v for k, v in by_employee.items() if len(v) > 1}
        
        return f"""EMPLOYEE ASSIGNMENT DATA:
- Total employees with assets: {len(by_employee)}
- Employees with multiple devices: {len(multi)}
  {chr(10).join('  • ' + k + ': ' + ', '.join(v) for k, v in list(multi.items())[:10])}
- Unassigned assets: {len(by_employee.get('Unassigned', by_employee.get('', [])))}"""
    except Exception as e:
        return f"EMPLOYEE DATA: Error: {str(e)[:80]}"


def _get_procurement_data() -> str:
    """Brand/model analysis for procurement."""
    try:
        table = _dynamodb.Table(CMDB_TABLE)
        items = table.scan().get("Items", [])
        
        models = {}
        for item in items:
            model = item.get("deviceModel", "Unknown")
            if model not in models:
                models[model] = {"count": 0, "total_age": 0, "warranty_active": 0}
            models[model]["count"] += 1
            models[model]["total_age"] += int(item.get("deviceAgeMonths", 0))
            if item.get("warrantyActive"):
                models[model]["warranty_active"] += 1
        
        summary = []
        for model, data in sorted(models.items(), key=lambda x: x[1]["count"], reverse=True)[:10]:
            avg_age = round(data["total_age"] / max(data["count"], 1))
            summary.append(f"  • {model}: {data['count']} units, avg age {avg_age}mo, {data['warranty_active']} in warranty")
        
        return f"""PROCUREMENT / MODEL ANALYSIS:
- Unique device models: {len(models)}
- Top models by count:
{chr(10).join(summary)}"""
    except Exception as e:
        return f"PROCUREMENT DATA: Error: {str(e)[:80]}"


def _get_esg_data() -> str:
    """Sustainability / ESG metrics."""
    try:
        history_table = _dynamodb.Table(HISTORY_TABLE)
        items = history_table.scan().get("Items", [])
        
        continued = sum(1 for i in items if i.get("decision") == "Continue Use")
        repaired = sum(1 for i in items if "Repair" in (i.get("decision") or ""))
        disposed = sum(1 for i in items if "Dispose" in (i.get("decision") or ""))
        
        # Estimate: each continued device = ~2.5kg e-waste avoided
        ewaste_avoided_kg = continued * 2.5
        
        return f"""ESG / SUSTAINABILITY METRICS:
- Devices kept in service (life extended): {continued}
- Devices sent for repair/refurbish: {repaired}
- Devices marked for disposal: {disposed}
- Estimated e-waste avoided: {ewaste_avoided_kg}kg
- Reuse rate: {round(continued/max(len(items),1)*100)}%
- Note: Each lifecycle extension saves approx $1,200-$1,800 in replacement cost"""
    except Exception as e:
        return f"ESG DATA: Error: {str(e)[:80]}"


def _call_bedrock(user_prompt: str) -> str:
    """Call Bedrock Claude to answer the question."""
    body = json.dumps({
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 1000,
        "temperature": 0.2,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": user_prompt}]
    })

    response = _bedrock.invoke_model(
        modelId=MODEL_ID,
        contentType="application/json",
        accept="application/json",
        body=body,
    )

    response_body = json.loads(response["body"].read())
    return response_body.get("content", [{}])[0].get("text", "I couldn't generate a response.")
