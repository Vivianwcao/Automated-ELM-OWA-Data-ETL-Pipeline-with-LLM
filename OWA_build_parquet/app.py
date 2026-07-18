import boto3
import pandas as pd
import io
import json
import logging
import traceback
from datetime import datetime, timezone

logger = logging.getLogger()
logger.setLevel(logging.INFO)


s3 = boto3.client('s3')
bedrock = boto3.client(service_name='bedrock-runtime', region_name='ca-central-1')
# # Correct Model ID for Claude Haiku 3.5
# model_id = "anthropic.claude-3-haiku-20240307-v1:0"
# # Correct Model ID for Claude Haiku 4.5
# model_id = "global.anthropic.claude-haiku-4-5-20251001-v1:0"
# Use the full ARN as the model_id
model_id = "arn:aws:bedrock:ca-central-1:759080473149:inference-profile/global.anthropic.claude-haiku-4-5-20251001-v1:0"

# ─── LOGGING ────────────────────────────────────────────────────────────────

def log_info(message, **kwargs):
    logger.info(json.dumps({"level": "INFO", "message": message, **kwargs}))

def log_error(message, **kwargs):
    logger.error(json.dumps({"level": "ERROR", "message": message, 'traceback': traceback.format_exc(), **kwargs}))

# always return a same string for concatenation
def safe_str(value):
    if value is None:
        return ""
    if isinstance(value, float) and pd.isna(value):
        return ""
    return str(value)


def call_claude(prompt):
    """Call Claude on Bedrock, return parsed JSON response."""
    body = json.dumps({
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 2000, # charged for actual usage (output tokens)
        "messages": [
            {
                "role": "user", 
                "content": [{"type": "text", "text": prompt}]
            }
        ],
        "temperature": 0  # Set to 0 for reliable JSON extraction
    })
    response = bedrock.invoke_model(
        modelId=model_id,
        body=body
    )
    response_body = json.loads(response.get('body').read())
        
    # Check if response was truncated
    stop_reason = response_body.get('stop_reason')
    if stop_reason == 'max_tokens':
        raise ValueError(f"LLM response truncated — increase max_tokens for output. Stop reason: {stop_reason}")

    # Claude returns content as list of blocks
    res = response_body['content'][0]['text']
    print(res)
    print("input tokens: ", response_body['usage']['input_tokens'])
    print("output tokens: ", response_body['usage']['output_tokens'])
    # Strip markdown fences if present
    text = res.strip().removeprefix('```json').removesuffix('```').strip()
    return json.loads(text)


def build_llm_prompt(main, days, summary_of_changes, material_transfer):
    
    # Send raw summary_of_changes rows to LLM for interpretation
    downhole_raw = summary_of_changes.get('downhole_configuration', [])
    squeezes_raw = summary_of_changes.get('sement_squeezes', [])
    perforations_raw = summary_of_changes.get('previous_perforations', [])

    prompt = f"""You are analyzing an oil well abandonment report for regulatory and cost analysis.
Extract structured data from all sources below. Return ONLY valid JSON, no explanation.

EXECUTIVE SUMMARY:
{main.get('executive_summary_raw', 'Not available')}

DAILY OPERATION SUMMARY:
{chr(10).join([
    "Report_number: " + safe_str(summary.get("report_number")) 
    + " - SUMMARY: " + safe_str(summary.get("d_operation_summary"))
    + " | NOTES: " + truncate_daily_note(
        safe_str(summary.get("daily_notes_raw")), 
        max_chars=1200)
    for summary in days.get('daily_operations', [])
])}

SUMMARY OF CHANGES - DOWNHOLE CONFIGURATION:
{json.dumps(downhole_raw, indent=2)}

SUMMARY OF CHANGES - CEMENT SQUEEZES:
{json.dumps(squeezes_raw, indent=2)}

SUMMARY OF CHANGES - PREVIOUS PERFORATIONS:
{json.dumps(perforations_raw, indent=2)}

MATERIAL TRANSFERS:
{json.dumps(material_transfer, indent=2)}

EXTRACTION RULES:
- pressure_test_passed: "held", "HELD", "successful", "good test" = true. "failed", "feed rate", "bleed off", "did not get test" = false
- pressure_test_kpa: number followed by kPa or MPa (convert MPa to kPa: multiply by 1000) near a pressure test
- pressure_test_duration_min: number followed by "minutes" near a pressure test
- volume_m3_llm: convert litres to m3 (divide by 1000). "140 litres" = 0.14 m3
- cement_blend: extract blend type e.g. "Class G", "OWG", "Class G + 0.5% CFR"
- attempt_number: 1 for first attempt, 2 if a previous attempt failed and this is remediation
- report_number: which daily report number this event occurred on (from "Report No. X" prefix in daily notes)

IMPORTANT RULES:
- pressure_test fields only apply to bridge_plug and cement event types
- Set all pressure test fields to null for cut_and_cap, perforation, packer events
- Shear pressure used to SET a plug (typically 10,000-14,000 kPa) is NOT a pressure test
- A real pressure test occurs AFTER setting at lower pressure with a documented hold duration
- For bridge_plug, search ALL daily notes for the final successful pressure test at that depth
- If a plug was set on one day and tested on a later day, use the test result from the later day
- Extract perforations from PREVIOUS PERFORATIONS section as separate perforation events
- For cut_and_cap events, set depth_mkb and depth_to_mkb to null — 
  the cut depth below ground is not a wellbore depth measurement
- Use "cement" only for actual cement placement (bridge plug cement, squeeze cement)
- Fluid circulation, water displacement, or killing the well is NOT a cement event — skip it
Return this exact JSON structure:
{{
  "operational": {{
    "issues_noted": "string or null - specific problems: stuck packer, parted tubing, leaky casing, failed pressure tests with depths",
    "scvf_result": "no bubbles | bubbles detected | null",
    "job_complete": true or false or null,
    "next_operations": "string or null - last mentioned next operation",
    "dds_number_llm": "string or null - AER DDS notification number e.g. 1195853"
  }},
  "well_events": [
    {{
      "event_type": "bridge_plug | cement | cement_squeeze | perforation | packer | cut_and_cap | bond_log",
      "description": "clean human-readable description",
      "depth_mkb": number or null,
      "depth_to_mkb": number or null,
      "volume_m3_llm": number or null,
      "cement_blend": "string or null",
      "pressure_test_passed": true or false or null,
      "pressure_test_kpa": number or null,
      "pressure_test_duration_min": number or null,
      "attempt_number": 1 or 2 or null,
      "report_number": number or null
    }}
  ],
  "material_transfer": [
    {{
      "item": "string",
      "quantity": number or null,
      "condition": "A | B | C | D | JUNK | null",
      "transferred_to": "string or null"
    }}
  ]
}}"""
    # print(prompt)
    return prompt

def truncate_daily_note(note, max_chars=1200):
    if not note or len(note) <= max_chars:
        return note
    # Cut at the last complete sentence within the limit
    truncated = note[:max_chars]
    last_period = truncated.rfind('.')
    if last_period > max_chars * 0.6:  # only cut at sentence if not too short
        return truncated[:last_period + 1] + '...'
    return truncated + '...'

# For wells
def classify_downhole_event(description):
    if not description:
        return 'downhole_configuration'
    desc_lower = description.lower()
    if any(k in desc_lower for k in ['plug', 'pbp', 'bridge']):
        return 'bridge_plug'
    if any(k in desc_lower for k in ['cement', 'cmt', 'squeeze']):
        return 'cement'
    if any(k in desc_lower for k in ['packer']):
        return 'packer'
    if any(k in desc_lower for k in ['cut', 'cap']):
        return 'cut_and_cap'
    if any(k in desc_lower for k in ['perf', 'shot', 'zone']):
        return 'perforation'
    if any(k in desc_lower for k in ['bond', 'cbl', 'log']):
        return 'bond_log'
    return 'downhole_configuration'

def lambda_handler(event, context):
    # Initialize variables so the 'except' block always has something to print
    bucket = "emi-v3"
    key = "unknown_key"
    s3_event = event # Default for local tests

    try:
        # ── LAYER 1: Event Extraction ──────────────────────────────────────
        try:
            # Detect if this is an SQS-wrapped event
            if "Records" in event:
                # Overwrite s3_event with the 'Inner Letter'
                s3_event = json.loads(event["Records"][0]["body"])

            bucket = s3_event['detail']['bucket']['name']
            key    = s3_event['detail']['object']['key']
        except (ValueError, TypeError, json.JSONDecodeError) as e:
            # Use a safe default for key if extraction failed
            error_key = s3_event.get('detail', {}).get('object', {}).get('key', 'unknown_key')
            log_error("Event extraction failed", key=error_key, error=str(e))
            return {'status': 'error', 'message': 'malformed event'}
            
        # ── LAYER 2: Main Business Logic ───────────────────────────────────
        # This only runs if Layer 1 succeeded

        # Read the JSON from extracted/
        response = s3.get_object(Bucket=bucket, Key=key)
        # print("response", response)
        body = response['Body'].read()
        invoice = json.loads(body.decode())

        main = invoice['main']
        days = invoice['days']
        summary_of_changes = invoice.get('summary_of_changes', {})
        load_fluid = invoice.get('load_fluid', [])
        material_transfer = invoice.get('material_transfer', [])

        # ── Enrich Main ────────────────────────────────────────────────────

        prompt = build_llm_prompt(main, days, summary_of_changes, material_transfer)
        llm_fields = call_claude(prompt)
        # update main with llm results
        main.update(llm_fields.get("operational", {}))
        # update main with summary_of_changes headers
        soc_meta = {
            'soc_prepared_by': summary_of_changes.get('prepared_by'),
            'soc_kb': summary_of_changes.get('kb'),
            'soc_gl': summary_of_changes.get('gl'),
            'soc_kb_gl': summary_of_changes.get('kb-gl'),
            'soc_kb_tf': summary_of_changes.get('kb-tf'),
            'soc_td': summary_of_changes.get('td'),
            'soc_pbtd': summary_of_changes.get('pbtd'),
        }
        main.update(soc_meta)

        # add to every sheet's columns
        join_keys = {
            'uwi': main.get('uwi'),
            'project_number': main.get('project_number'),
            'client': main.get('client'),
            'afe_number': main.get('afe_number'),
        }

        # ── Enrich well_events  ─────────────────────────

        WELL_EVENT_BASE = {
            'source': None,
            'table': None,
            'event_type': None,
            'description': None,
            'depth_mkb': None,
            'depth_to_mkb': None,
            'volume_m3_llm': None,
            'volume_m3': None,
            'volume_tonne': None,
            'cement_blend': None,
            'pressure_test_passed': None,
            'pressure_test_kpa': None,
            'pressure_test_duration_min': None,
            'attempt_number': None,
            'report_number': None,
            'daily_notes_raw': None,
            "d_operation_summary": None
        }
        well_events = []
        # From summary of changes — add source tag
        for item in summary_of_changes.get('downhole_configuration', []):
            if not item.get('description'):
                continue
            well_events.append({
                **WELL_EVENT_BASE,
                **join_keys,
                'source': 'summary_of_changes',
                'table': 'downhole_configuration', 
                'event_type': classify_downhole_event(item.get('description')),
                'description': item.get('description'),
                'depth_mkb': item.get('interval1'),
                'depth_to_mkb': item.get('interval2'),
            })
        for item in summary_of_changes.get('sement_squeezes', []):
            well_events.append({
                **WELL_EVENT_BASE,
                **join_keys,
                'source': 'summary_of_changes',
                'table': 'cement_squeeze',
                'event_type': 'cement_squeeze',
                'depth_mkb': item.get('interval1'),
                'depth_to_mkb': item.get('interval2'),
                'volume_m3': item.get('volume_m3'),
                'volume_tonne': item.get('volume_tonne'),
                'cement_blend': item.get('cement_blend'),
            })
        for item in summary_of_changes.get('previous_perforations', []):
            if not item.get('zone') and not item.get('zone2'):
                continue
            well_events.append({
                **WELL_EVENT_BASE,
                **join_keys,
                'source': 'summary_of_changes',
                'table': 'perforation',
                'event_type': 'perforation',
                'description': ' / '.join(filter(None, [item.get('zone'), item.get('zone2')])),
                'depth_mkb': item.get('interval1'),
                'depth_to_mkb': item.get('interval2'),
            })

        # From LLM — add source tag
        for item in llm_fields.get("well_events", []):
            well_events.append({
                **WELL_EVENT_BASE,
                **join_keys,
                "source": "llm",
                **item
            })

        # raw notes&summaries from xlsm
        well_events.extend([{
                **WELL_EVENT_BASE,
                **join_keys,
                "source": "days",
                "event_type": "daily_report",
                **item, #report_number and raw notes
            } for item in days.get("daily_operations", [])])

        # ── Write Parquet files ───────────────────────────────────────────────
        # Spaces in S3 keys are valid but cause problems with Glue crawler and some Athena query tools, replacing spaces with _
        def recreate_s3_key(key, prefix):
            filename = key.split('/')[-1].replace('.json', '.parquet').replace(' ', '_')
            return f"{prefix}{filename}"

        # 1. main — single row
        write_parquet(bucket, recreate_s3_key(key, 'OWA/clean/main/main-'), [main])

        # 2. charges — embed join keys on every row
        for day in days.get('charges', []):
            day.update(join_keys)
        write_parquet(bucket, recreate_s3_key(key, 'OWA/clean/charges/charges-'), days.get('charges', []))

        # 3. well_events
        if well_events:
            write_parquet(bucket, recreate_s3_key(key, 'OWA/clean/wells/wells-'), well_events)

        # 4. material_transfer
        llm_mt = llm_fields.get("material_transfer")
        material_transfer_final = llm_mt if llm_mt else material_transfer
        if material_transfer_final:
            for x in material_transfer_final:
                x.update(join_keys)
            write_parquet(bucket, recreate_s3_key(key, 'OWA/clean/material_transfer/material_transfer-'), material_transfer_final)
    
        # 5. load_fluid
        if load_fluid:
            for x in load_fluid:
                x.update(join_keys)
            write_parquet(bucket, recreate_s3_key(key, 'OWA/clean/load_fluid/load_fluid-'), load_fluid)

        return {'status': 'success', 'key': key}
        
    except Exception as e:
        log_error("Lambda failed", key=key, error=str(e))

        # Write failure marker so you can query S3 for all failed files
        failed_key = (
            key.replace('extracted/', 'clean_failed/')
               .replace('.xlsm', '_failed.json')
        )
        try:
            s3.put_object(
                Bucket=bucket,
                Key=failed_key,
                Body=json.dumps({
                    'source_key': key,
                    'error':      str(e),
                    'timestamp':  datetime.now(timezone.utc).isoformat(),
                }),
                ContentType='application/json',
            )
        except Exception as marker_err:
            log_error("Could not write failure marker", key=key, error=str(marker_err))
        
        raise   # Re-raise so SQS/DLQ captures the failure

# Enforce consistent types to prevent PyArrow mixed-type errors
MASTER_SCHEMA = {
    # main
        "client": "string",
        "date": "string",
        "uwi": "string",
        "report_number": "float64",
        "project_manager": "string",
        "afe_number": "string",
        "afe_amount": "float64",
        "total_costs_to_date": "float64",
        "project_number": "string",
        "project_descriptor": "string",
        "casing_size": "float64",
        "cost_centre": "string",
        "well_name": "string",
        "surface_location": "string",
        "area": "string",
        "license": "string",
        "executive_summary_raw": "string",
        "dds_number": "string",
        "dds_number_llm": "string",
        "issues_noted": "string",
        "scvf_result": "string",
        "job_complete": "boolean",
        "next_operations": "string",
        "soc_prepared_by": "string",
        "soc_kb": "float64",
        "soc_gl": "float64",
        "soc_kb_gl": "float64",
        "soc_kb_tf": "float64",
        "soc_td": "float64",
        "soc_pbtd": "float64",
        # charges
        "d_report_number": "float64",
        "charge_type": "string",
        "service_provided": "string",
        "number_of_units": "float64",
        "rate": "float64",
        "resource_name": "string",
        "thrdpty_man_hours": "float64",
        "kilometers": "float64",
        "po_number": "string",
        "ticket_number": "string",
        "contractor": "string",
        "thrdpty_subtotal": "float64",
        "amount": "float64",
        "rates_elm_fraction": "float64",
        "rates_thrdpty_fraction": "float64",
        "subtotal_with_mgt_fee": "float64",
        # load_fluid
        "ticket_company": "string",
        "source": "string",
        "fluid_type": "string",
        "m3": "float64",
        "category": "string",
        "tank": "string",
        "destination": "string",
        # material_transfer
        "item": "string",
        "quantity": "float64",
        "condition": "string",
        "transferred_to": "string",
        # wells
        "source": "string",
        "table": "string",
        "event_type": "string",
        "description": "string",
        "depth_mkb": "float64",
        "depth_to_mkb": "float64",
        "volume_m3_llm": "float64",
        "volume_m3": "float64",
        "volume_tonne": "float64",
        "cement_blend": "string",
        "pressure_test_passed": "boolean",
        "pressure_test_kpa": "float64",
        "pressure_test_duration_min": "float64",
        "attempt_number": "float64",
        "report_number": "float64",
        "daily_notes_raw": "string",
        "d_operation_summary": "string"
}

def write_parquet(bucket, key, rows):
    if not rows:
        return
    df = pd.DataFrame(rows)
    
    for col, target_type in MASTER_SCHEMA.items():
        if col not in df.columns:
            continue
    
        # 2. Category-based enforcement
        if target_type == 'float64':
            # errors='coerce' turns strings/None into NaN.
            # NaN is the correct 'Null' for Float64.
            df[col] = pd.to_numeric(df[col], errors='coerce').astype('float64')

        elif target_type == "string":
            # CRITICAL: .astype(str) turns None into the word "None". 
            # We want to keep actual Nulls as Nulls.
            df[col] = df[col].replace({pd.NA: None})
            df[col] = df[col].fillna('').astype(str).replace('', None)

        elif target_type == "boolean":
            # Pandas 'boolean' (capital B) supports <NA> (null)
            # Standard 'bool' does NOT.(old Numpy)
            df[col] = df[col].astype("boolean")
            
    # Now PyArrow will see a perfectly consistent schema every time
    buf = io.BytesIO()
    df.to_parquet(buf, engine='pyarrow', index=False)
    s3.put_object(Bucket=bucket, Key=key, Body=buf.getvalue())

# ─── LOCAL TEST ──────────────────────────────────────────────────────────────

if __name__ == '__main__':
    test_event = {
        "detail": {
            'bucket': {'name': 'emi-v3'},
            'object': {'key': 'OWA/extracted/Harvest_das_Provost_100-11-36-041-11W4_r3_final_provost4.json'},
            # 'object': {'key': 'OWA/extracted/Harvest_ds_108__06-15-041-12W4_r6_final_Bellshill_round2.json'},
            # 'object': {'key': 'OWA/extracted/Harvest_sc_1B0_01-32-040-01W4_r8_final_Hayter.json'},
            # 'object': {'key': 'OWA/extracted/OWA_100_03-01-047-17W4_Daily_Report.json'},
            # 'object': {'key': 'OWA/extracted/Hayter_105_04-35-040-01W4_R7.json'},
            # 'object': {'key': 'OWA/extracted/Hayter_105_03-25-040-01W4_R10.json'},
            # 'object': {'key': 'OWA/extracted/Harvest_wb_1D0_13-20-40-1W4_r13_final_Hayter_vent_flow.json'},
            # 'object': {'key': 'OWA/extracted/Bellshill_103_01-22-041-12W4_r9_.json'},
        }
    }
    result = lambda_handler(test_event, None)
    print(json.dumps(result, indent=2))