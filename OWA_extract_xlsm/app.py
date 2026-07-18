import boto3
from botocore.exceptions import ClientError
import pandas as pd
import io
import re
import json
import traceback
from datetime import datetime, timezone
import logging

logger = logging.getLogger()
logger.setLevel(logging.INFO)

s3 = boto3.client('s3')


# ─── LOGGING ────────────────────────────────────────────────────────────────

def log_info(message, **kwargs):
    logger.info(json.dumps({"level": "INFO", "message": message, **kwargs}))

def log_error(message, **kwargs):
    logger.error(json.dumps({"level": "ERROR", "message": message, 'traceback': traceback.format_exc(), **kwargs}))


# ─── LOW-LEVEL CELL HELPERS ─────────────────────────────────────────────────

def clean_str(value):
    """Return stripped string or None if empty/nan."""
    if value is None:
        return None
    # A “missing value” is often stored as a float NaN, not as an empty value.
    if isinstance(value, float) and pd.isna(value):
        return None
    s = str(value).strip()
    return None if s.lower() in ('nan', 'none', '') else s

def clean_val(value):
    """Return float, stripping currency symbols and #ERROR!."""
    if value is None:
        return None
    # A “missing value” is often stored as a float NaN, not as an empty value.
    if isinstance(value, float) and pd.isna(value):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().lower()
    if s in ('', 'nan', 'none', '#error!'):
        return None
    try:
        is_percentage = "%" in s # if a percentage
        # Remove all characters except digits, dot, minus
        cleaned = float(re.sub(r'[^\d.-]', '', s))
        if is_percentage:
            cleaned /= 100
        return cleaned
    except ValueError:
        return None

# Need to enforce field type consistensy for creating parquet files. Mixed types for the same field will crash
# Material_transfer QTY sometimes are entered as strings, but will be cleaned to float by LLM
FLOAT_COLUMNS = ['interval1', 'interval2', 'm3', 'quantity']

def clean(label, value):
    """Clean both floats and strings"""
    try:
        if label in FLOAT_COLUMNS:
            return clean_val(value)
        return clean_str(value)
    except ValueError:
        return None

# return a tuple (vol_m3, vol_tonne)
def parse_volume(vol_str):
    vol_m3 = None
    vol_tonne = None
    if not vol_str:
        return vol_m3, vol_tonne
    try:
        # default unit for a cement squeeze is cubic meters.
        vol_m3 = float(vol_str)
        return vol_m3, vol_tonne
    except ValueError:
        pass

    vol_str_lower = vol_str.lower()
    pattern = r'(\d+\.?\d*)\s*(tonne|tonnes|ton|t\b|m\b|m3|m³|cubic)?'
    matches = re.findall(pattern, vol_str_lower)

    for num_str, unit in matches:
        try:
            num = float(num_str)
        except ValueError:
            continue

        if unit in ('m', 'm3', 'm³', 'cubic'):
            vol_m3 = num
        elif unit in ('tonne', 'tonnes', 'ton', 't'):
            vol_tonne = num
        else:
            vol_m3 = num
    return vol_m3, vol_tonne

# ── Save JSON to extracted/ prefix ───────────────────────────────────
def generate_output_key(key):
    return (
        key.replace('raw/', 'extracted/')
        .replace('.xlsm', '.json')
        .replace('.XLSM', '.json')
        .replace('.xlsx', '.json')
        .replace('.XLSX', '.json')
        .replace(' ', '_') # Spaces in S3 keys are valid but cause problems with Glue crawler and some Athena query tools, replacing spaces with _
    )

def output_exists(bucket, key):
    """
    Checks if the processed json version of the file already exists in raw/.
    To prevent re-extracting
    """
    try:
        s3.head_object(Bucket=bucket, Key=key)
        # file exists, exit early
        return True
    except ClientError as e:
        # 404 means the file does not exist, which is what we want
        if e.response['Error']['Code'] == "404":
            return False
        # If it's a different error (like 403 Access Denied), raise it
        raise e   

# ─── ANCHOR / VALUE SEARCH ───────────────────────────────────────────────────

def find_value(df, label, col_offset=1, row_offset=0):
    """
    Scan every cell for an exact label match (case-insensitive, ignoring
    trailing colons).  Return the neighbour cell at (row+row_offset,
    col+col_offset).  If that cell is NaN, try one column further right
    (merged-cell fallback).
    """
    needle = label.lower().strip().rstrip(':')
    for r in range(len(df)):
        for c in range(len(df.columns)):
            cell = str(df.iloc[r, c]).strip().lower().rstrip(':')
            if cell == needle:
                try:
                    val = df.iloc[r + row_offset, c + col_offset]
                    if pd.isna(val):
                        # merged cell: value may be one column to the right
                        return df.iloc[r + row_offset, c + col_offset + 1]
                    return val
                except (IndexError, KeyError):
                    return None
    return None

def find_row_index(df, anchor_text):
    """Return the first row index whose concatenated text contains anchor_text."""
    needle = anchor_text.lower()
    for r in range(len(df)):
        row_text = ' '.join(str(x) for x in df.iloc[r].values).lower()
        if needle in row_text:
            return r
    return None


# ─── TEXT BLOCK EXTRACTION ───────────────────────────────────────────────────

def extract_text_block(df, start_row, max_consecutive_empty=2, block_terminators=None):
    """
    Collect text from start_row downward.

    Stops when:
      - max_consecutive_empty blank rows in a row, OR
      - a known terminator keyword is found.

    Returns a single string with lines joined by ' | '.
    """
    lines = []
    empty_streak = 0

    for r in range(start_row, len(df)):
        # Gather all non-empty cell values in this row
        cells = [
            str(df.iloc[r, c]).strip()
            for c in range(len(df.columns))
        ]
        cells = [v for v in cells if v and v.lower() not in ('nan', 'none')]
        row_text = ' '.join(cells).strip()

        if not row_text:
            empty_streak += 1
            if empty_streak >= max_consecutive_empty:
                break
            continue

        empty_streak = 0

        # Stop at terminator keywords
        if block_terminators and any(t in row_text.lower() for t in block_terminators):
            break

        lines.append(row_text)

    return ' | '.join(lines) if lines else None

# ─── TABLE ROWS EXTRACTION ───────────────────────────────────────────────────

# For Summary of Changes && Load Fluid tables - fixed row ranges + fixed column labels
def extract_table_lines(df, column_labels, rows):
    for _, row in df.iterrows(): # returns a row/series
        # line = {label : row[col] for label, col in zip(column_labels, df.columns) if label is not None}
        line = {} 
        for label, col in zip(column_labels, df.columns):
            cell = row[col]
            if label is None:
                continue
            line[label] = clean(label, cell)
        # reject line if every cell is blank 
        if all(v is None or v == "" or (isinstance(v, float) and pd.isna(v)) for v in line.values()):
            continue
        rows.append(line)


# ─── COLUMN MAP ──────────────────────────────────────────────────────────────

# For daily tab tables (1, 2, 3, ...) - variant column labels + find row ranges using find_row_index()
def build_col_map(header_row):
    """Build {col_name_lower: positional_col_index} from a row Series."""
    col_map = {}
    for pos, val in enumerate(header_row.values):
        val = clean_str(val)
        if val:
            col_map[val.lower()] = pos
    return col_map

def get_col(row, col_map, *candidate_names):
    """
    Try each candidate name (lower-case) in col_map and return the first
    non-NaN value found.  Returns None if nothing matches.
    """
    for name in candidate_names:
        idx = col_map.get(name.lower())
        if idx is not None:
            try:
                val = row.iloc[idx]
                if not (isinstance(val, float) and pd.isna(val)):
                    return val
            except (IndexError, KeyError):
                pass
    return None


# ─── DDS EXTRACTION ──────────────────────────────────────────────────────────

def extract_dds_number(text):
    """Pull AER DDS notification number from free text, e.g. '# 1195853'."""
    if not text:
        return None
    match = re.search(
        r'(?:dds|notification|aer|AER)[^\d#]*#?\s*(\d{5,})',
        text, re.IGNORECASE
    )
    return match.group(1) if match else None


# ─── SECTION ANCHORS ─────────────────────────────────────────────────────────

DAILY_CHARGE_TERMINATORS = (
    'subtotal', 'elm total', 'third party total',
    'management fee', 'To add a line in text box use "alt enter"',
    'man hours / km', 'man hours total', 'end man hours',
)
       
# Keywords that signal the end of a free-text block
DAILY_SUMMARY_TERMINATORS = (
    'scvf test', 'bag size', 'calculated flow',
)

# ─── MAIN HANDLER ────────────────────────────────────────────────────────────

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
        except (ValueError, TypeError) as e:
            # Use a safe default for key if extraction failed
            error_key = s3_event.get('detail', {}).get('object', {}).get('key', 'unknown_key')
            log_error("Event extraction failed", key=error_key, error=str(e))
            return {"statusCode": 200}

        # ── LAYER 2: Check if file already extracted ───────────────────────
        output_key = generate_output_key(key) # will also be used when saving file
        if output_exists(bucket, output_key):
            log_info("Skipping, file already exists in extracted folder", key=output_key)
            return {
                'statusCode': 200,
                'body': f"File {key} already process. Skipping."
            }

        # ── LAYER 3: Main Business Logic ───────────────────────────────────
        # This only runs if Layer 1 succeeded

        log_info("Starting extraction", bucket=bucket, key=key)
        summary_of_changes = {}
        load_fluid = []
        material_transfer = []
        days = {}

        response = s3.get_object(Bucket=bucket, Key=key)
        # xl is an Excel file handler, not the data itself.
        xl = pd.ExcelFile(
            io.BytesIO(response['Body'].read()), engine='openpyxl'
        )
        sheet_names = xl.sheet_names
        log_info("Sheets found", sheets=sheet_names, key=key)

        # ── RATES SHEET ──────────────────────────────────────────────────────
        rates_dict = {}
        rates_elm_fraction = 0
        rates_thrdpty_fraction = 0

        if 'Rates' in sheet_names:
            # .parse() - Read a sheet into a DataFrame
            rdf = xl.parse('Rates', header=None)
            rates_dict = {
                str(k).lower().strip(): clean_val(v)
                # : → all rows, - Select all rows starting from the 4th row (3:) from the second column (0)
                #  DataFrames are like key(header name): columns data 
                for k, v in zip(rdf.iloc[3:, 0], rdf.iloc[3:, 2])
                if clean_str(k)
            }
            log_info("Rates", rates=rates_dict)
            # look for the item one row below its label
            rates_elm_fraction = clean_val(find_value(rdf.iloc[3:, [1]], 'ELM', col_offset=0, row_offset=1)) or 0
            rates_thrdpty_fraction = clean_val(find_value(rdf.iloc[3:, [2]], 'THRDPTY', col_offset=0, row_offset=1)) or 0

        # ── SUMMARY OF CHANGES SHEET ──────────────────────────────────────────────────────
        if 'Summary of Changes'in sheet_names:
            sdf = xl.parse('Summary of Changes', header=None).iloc[1:47, 11:20]
            prepared_by = clean_str(sdf.iat[1, 3])
            soc_kb = clean_val(find_value(sdf.iloc[3:4,:5], label="KB:", col_offset=1, row_offset=0))
            soc_gl = clean_val(find_value(sdf.iloc[4:5,:5], label="GL:", col_offset=1, row_offset=0))
            soc_kb_gl = clean_val(find_value(sdf.iloc[3:4,5:7], label="KB-GL:", col_offset=1, row_offset=0))
            soc_kb_tf = clean_val(find_value(sdf.iloc[4:5,5:7], label="KB-TF:", col_offset=1, row_offset=0))
            soc_td = clean_val(find_value(sdf.iloc[3:4,7:9], label="TD:", col_offset=1, row_offset=0))
            soc_pbtd = clean_val(find_value(sdf.iloc[4:5,6:9], label="PBTD:", col_offset=1, row_offset=0))

            # Hard-coded postions, chance of changing is low
            summary_of_changes["prepared_by"] = prepared_by
            summary_of_changes["kb"] = soc_kb
            summary_of_changes["gl"] = soc_gl
            summary_of_changes["kb-gl"] = soc_kb_gl
            summary_of_changes["kb-tf"] = soc_kb_tf
            summary_of_changes["td"] = soc_td
            summary_of_changes["pbtd"] = soc_pbtd
            
            downhole_configuration = []
            sdf_dc = sdf.iloc[8:25, 2:]
            # The tricky thing is if no data in the last column, it will be cut off. labels like "Interval:" are in 2nd to last column. 
            dc_labels = ['description', None, None, None, None, 'interval1', 'interval2']
            # Downhole table:
            extract_table_lines(sdf_dc, dc_labels, downhole_configuration)

            summary_of_changes['downhole_configuration'] = downhole_configuration

            sement_squeezes = []
            sdf_ss = sdf.iloc[27:34, :]
            ss_labels = ['volume_raw', None, None, 'cement_blend', None, None, None, 'interval1', 'interval2']
            extract_table_lines(sdf_ss, ss_labels, sement_squeezes)

            for line in sement_squeezes:
                volume_raw = line.get("volume_raw")
                volume_m3, volume_tonne = parse_volume(volume_raw)
                line["volume_m3"] = volume_m3
                line["volume_tonne"] = volume_tonne    
            summary_of_changes['sement_squeezes'] = sement_squeezes

            previous_perforations = []
            sdf_pp = sdf.iloc[40:45, 1:6]
            pp_labels = ['zone', 'zone2', None, 'interval1', 'interval2']
            extract_table_lines(sdf_pp, pp_labels, previous_perforations)
            summary_of_changes['previous_perforations'] = previous_perforations

        # ── LOAD FLUID SHEET ──────────────────────────────────────────────────────

        def add_columns(list, **kwargs):
                for x in list:
                    if x:
                        for key, value in kwargs.items():
                            x[key] = value

        if 'Load Fluid'in sheet_names:
            ldf = xl.parse('Load Fluid', header=None).iloc[:, :9]
            # opening load oil table
            # use load_fluid list
            row_idx_olo = find_row_index(ldf, 'OPENING LOAD OIL')
            olo_labels = ["date", None, "tank", None, "source", None, "fluid_type", None, "m3"]
            if row_idx_olo is not None:
                extract_table_lines(ldf.iloc[row_idx_olo + 2:row_idx_olo + 4, :], olo_labels, load_fluid)
                add_columns(load_fluid, category='opening_load_oil', ticket_company=None, destination=None)

            # fluid hauled to lease table
            fluid_hauled_to_lease = []
            row_idx_fhtl = find_row_index(ldf, 'FLUID HAULED TO LEASE')
            fhtl_labels = ["date", None, "ticket_company", None, "source", None, "fluid_type", None, "m3"]
            if row_idx_fhtl is not None:
                extract_table_lines(ldf.iloc[row_idx_fhtl + 2:row_idx_fhtl + 11, :], fhtl_labels, fluid_hauled_to_lease)
                add_columns(fluid_hauled_to_lease, category='fluid_hauled_to_lease', tank=None, destination=None)

            # fluid hauled from lease table
            fluid_hauled_from_lease = []
            row_idx_fhfl = find_row_index(ldf, 'FLUID HAULED FROM LEASE')
            fhfl_labels = ["date", None, "ticket_company", None, None, "destination", "fluid_type", None, "m3"]
            if row_idx_fhfl is not None:
                extract_table_lines(ldf.iloc[row_idx_fhfl + 2:row_idx_fhfl + 11, :], fhfl_labels, fluid_hauled_from_lease)
                add_columns(fluid_hauled_from_lease, category='fluid_hauled_from_lease', tank=None, source=None)
            load_fluid.extend(fluid_hauled_to_lease)
            load_fluid.extend(fluid_hauled_from_lease)

        # ── MATERIAL TRANSFER SHEET ──────────────────────────────────────────────────────
        if 'Material Transfer'in sheet_names:
            mtdf = xl.parse('Material Transfer', header=None).iloc[:, :7]
            mt_header_row_idx = find_row_index(mtdf, 'TRANSFERRED TO')
            mt_column_labels = ['quantity', 'item', None, None, 'condition', 'transferred_to']
            if mt_header_row_idx is None:
                mt_header_row_idx = 4
            extract_table_lines(mtdf.iloc[mt_header_row_idx + 1:25, 1:], mt_column_labels, material_transfer)

        # ── MAIN DATA SHEET ──────────────────────────────────────────────────
        if 'Main Data' not in sheet_names:
            raise ValueError("Sheet 'Main Data' not found")

        # Columns A-F only (indices 0-5)
        mdf = xl.parse('Main Data', header=None).iloc[:, 0:6]

        def fv(label, col_offset=1, row_offset=0):
            return find_value(mdf.iloc[2:12, :], label, col_offset, row_offset)

        # Executive summary: full text block below the label
        exec_summary_row = find_row_index(mdf, 'EXECUTIVE SUMMARY') # infact we know it is fixed row 13 (14th)
        exec_summary_raw = (
            # by default row 14 - 35 (14:36)
            extract_text_block(mdf.iloc[exec_summary_row + 1:, :], 0)
            if exec_summary_row is not None else None
        )

        main_headers = {
            'client':                   clean_str(fv('CLIENT')),
            'date':                     clean_str(fv('DATE')),
            'uwi':                      clean_str(fv('UWI')),
            'report_number':            clean_val(fv('Report #')),
            'project_manager':          clean_str(fv('PROJECT MANAGER')),
            'afe_number':               clean_str(fv('AFE #')),
            'afe_amount':               clean_val(fv('AFE Amount')),
            'total_costs_to_date':      clean_val(mdf.iat[6, 5]),
            'project_number':           clean_str(fv('Proj # / AFE / Job Number')),
            'project_descriptor':       clean_str(fv('Project Descriptor')),
            'casing_size':              clean_val(fv('Casing Size')),   # in very vew invoices
            # Some use Cost Centre, some use Well name
            'cost_centre':              clean_str(fv('Cost Centre')),
            'well_name':                clean_str(fv('Well name')),
            'surface_location':         clean_str(fv('Surface Location')),
            'area':                     clean_str(fv('AREA')),
            'license':                  clean_str(fv('License')),
            'executive_summary_raw':    exec_summary_raw,
            'dds_number':               extract_dds_number(exec_summary_raw),
        }

        log_info(
            "Main headers extracted",
            main_headers=main_headers
        )

        # ── DAILY SHEETS (numbered: "1", "2", …) ────────────────────────────

        all_rows = []

        for sheet_name in sheet_names:
            sheet_stripped = sheet_name.strip()
            # skip sheet 0. Sometimes the Number of units are mistakenly assigned 1.0 but has 0 Current days Costs(error)
            if not (sheet_stripped.isdigit() and sheet_stripped != "0"):
                continue

            log_info("Processing daily sheet", sheet=sheet_name, key=key)

            try:
                # Columns A-I only (indices 0-8)
                ddf = xl.parse(sheet_name, header=None).iloc[:, 0:9]

                d_report_number = clean_val(find_value(ddf.iloc[2:3, :], 'Report #'))
                d_operation_summary = clean_str(find_value(ddf.iloc[6:7, :], 'OPERATIONS SUMMARY'))

                # Daily notes (variable-length free text block)
                # Find the "THIRD PARTY TOTAL..." header row
                wa_header_row = find_row_index(ddf, 'THIRD PARTY TOTAL') 
                daily_notes_raw = None

                if wa_header_row is not None:
                    # Notes cell is exactly one row below the header, column B (index 1)
                    notes_cell = clean_str(ddf.iloc[wa_header_row + 3, 1])
                    if notes_cell:
                        daily_notes_raw = notes_cell.replace('\n', ' | ').strip(' | ')

                log_info("daily_notes_raw: ", d_notes=daily_notes_raw)

                daily_operations = {"report_number": d_report_number, "d_operation_summary": d_operation_summary, "daily_notes_raw": daily_notes_raw}
                days.setdefault('daily_operations', []).append(daily_operations)

                # Promote DDS number from daily notes if not found on main sheet
                if not main_headers.get('dds_number'):
                    main_headers['dds_number'] = extract_dds_number(daily_notes_raw)

                # ── Charge-line extraction ───────────────────────────────────
                current_section = None
                col_map         = {}
                
                for _, row in ddf.iterrows():
                    row_upper = ' '.join(str(x) for x in row.values).upper()

                    # ── Detect section start ─────────────────────────────────
                    # Check most specific first so "ELM CHARGES" doesn't
                    # accidentally match inside "THIRD PARTY CHARGES" row.
                    if 'THIRD PARTY CHARGES' in row_upper:
                        current_section = 'THIRD PARTY CHARGES'
                        col_map = {}
                        continue
                    if 'ELM CHARGES' in row_upper:
                        current_section = 'ELM CHARGES'
                        col_map = {}
                        continue
                    if 'SUPERVISION CHARGES' in row_upper:
                        current_section = 'SUPERVISION CHARGES'
                        col_map = {}
                        continue

                    # ── Detect column-header row ─────────────────────────────
                    # The row immediately following the section title that
                    # contains "SERVICE PROVIDED" is the column header row.
                    # Build a positional mapper like {Service provided: 0, Number of units: 1, ...}
                    if current_section and not col_map:
                        if 'service provided' in row_upper.lower():
                            col_map = build_col_map(row)
                            continue

                    # ── Terminator: end of section ───────────────────────────
                    row_lower = row_upper.lower()
                    if any(t in row_lower for t in DAILY_CHARGE_TERMINATORS):
                        current_section = None
                        col_map         = {}
                        continue

                    # ── Skip if not inside a section with a known header ─────
                    if not current_section or not col_map:
                        continue

                    # extract service proviced name, for mapping in col_mapper below
                    service = clean_str(row.iloc[0])
                    if not service or len(service) < 2:
                        continue
                    if service.lower() in ('service provided', 'day', 'time', 'none'):
                        continue

                    # ── Build universal line ─────────────────────────────────
                    # Start with a copy of the file-level headers, then add
                    # day-level and charge-level fields.
                    # All charge columns are present in every row; most will
                    # be None/0 depending on section type.
                    line = {
                        'd_report_number':      d_report_number,
                        'charge_type':          current_section,
                        'service_provided':     service,
                        # ELM / Supervision columns
                        'number_of_units':      None,
                        'rate':                 None,
                        'resource_name':        None,
                        # Third Party columns
                        'thrdpty_man_hours':    None,
                        'kilometers':           None,
                        'po_number':            None,
                        'ticket_number':        None,
                        'contractor':           None,
                        'thrdpty_subtotal':     None,
                        # Shared
                        'amount':               None,
                        'rates_elm_fraction':   0.0,
                        'rates_thrdpty_fraction': 0.0,
                        'subtotal_with_mgt_fee': 0.0
                    }

                    if current_section in ('ELM CHARGES', 'SUPERVISION CHARGES'):
                        qnty   = get_col(row, col_map, 'number of units')
                        clean_qnty = clean_val(qnty)
                        resource= get_col(row, col_map, 'resource name')
                        rate_cell = get_col(row, col_map, 'rate')

                        line['number_of_units'] = clean_qnty
                        line['resource_name']   = clean_str(resource)
                        # Prefer rate from the Rates sheet; fall back to cell
                        rate   = rates_dict.get(service.lower(), clean_val(rate_cell))
                        if not rate:
                            log_error(f"No matching rate found in Rate Table: {service}")
                        line['rate'] = rate
                        line['amount'] = (clean_qnty * rate if clean_qnty is not None and rate is not None else None)
                        line['rates_elm_fraction'] = rates_elm_fraction
                        line['subtotal_with_mgt_fee'] = (clean_qnty * rate * (1 + rates_elm_fraction) if clean_qnty is not None and rate is not None else None)

                    else:  # THIRD PARTY CHARGES
                        man_h      = get_col(row, col_map, 'man hours')
                        km         = get_col(row, col_map, 'kilometers')
                        po         = get_col(row, col_map, 'po#', 'po #', 'po number')
                        ticket     = get_col(row, col_map, 'ticket #', 'ticket#', 'ticket number')
                        contractor = get_col(row, col_map, 'contractor')
                        thrdpty_subtotal   = get_col(row, col_map, 'subtotal')
                        amount     = get_col(row, col_map, 'amount')
                        amount_clean = clean_val(amount)

                        line['thrdpty_man_hours']     = clean_val(man_h)
                        line['kilometers']    = clean_val(km)
                        line['po_number']     = clean_str(po)
                        line['ticket_number'] = clean_str(ticket)
                        line['contractor']    = clean_str(contractor)
                        line['thrdpty_subtotal'] = clean_val(thrdpty_subtotal)
                        line['amount']        = amount_clean
                        line['rates_thrdpty_fraction'] = rates_thrdpty_fraction
                        line['subtotal_with_mgt_fee'] = (amount_clean * (1 + rates_thrdpty_fraction) if amount_clean is not None else None)
                    # only add non 0 subtotal valid lines
                    if line['amount']:
                        all_rows.append(line)

            except Exception as sheet_err:
                log_error(
                    "Sheet processing failed",
                    sheet=sheet_name, key=key, error=str(sheet_err)
                )
                # Continue — don't let one bad sheet kill the whole file

        if not all_rows:
            raise ValueError("No charge rows extracted from any daily sheet")
        days['charges'] = all_rows

        log_info("Extraction complete", row_count=len(all_rows), key=key)

        invoice = {
            "main": main_headers, 
            "days": days, 
            "summary_of_changes": summary_of_changes, 
            "load_fluid": load_fluid,
            "material_transfer": material_transfer,
            }

        s3.put_object(
            Bucket=bucket,
            Key=output_key,
            Body=json.dumps(invoice, indent=2, default=str),
            ContentType='application/json',
        )
        log_info("Saved JSON", output_key=output_key)
        return {'status': 'success', 'file': output_key, 'rows': len(all_rows)}

    except Exception as e:
        log_error("Lambda failed", key=key, error=str(e))

        # Write failure marker so you can query S3 for all failed files
        failed_key = (
            key.replace('raw/', 'extracted_failed/')
               .replace('.xlsm', '_failed.json')
               .replace('.XLSM', '_failed.json')
               .replace('.xlsx', '_failed.json')
               .replace('.XLSX', '_failed.json')
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


# ─── LOCAL TEST ──────────────────────────────────────────────────────────────

if __name__ == '__main__':
    test_event = {
        "detail": {
            'bucket': {'name': 'emi-v3'},
            # 'object': {'key': 'OWA/raw/Harvest sc 1B0 01-32-040-01W4 r8 final Hayter.xlsm'},
            # 'object': {'key': 'OWA/raw/OWA 100_03-01-047-17W4 Daily Report.xlsm'},
            # 'object': {'key': 'OWA/raw/Harvest das Provost 100-11-36-041-11W4 r3 final provost4.xlsm'},
            # 'object': {'key': 'OWA/raw/Harvest ds 108  06-15-041-12W4 r6 final Bellshill round2.xlsm'},
            # 'object': {'key': 'OWA/raw/Hayter 105_03-25-040-01W4 R10.xlsm'},
            # 'object': {'key': 'OWA/raw/Hayter 105_04-35-040-01W4 R7.xlsm'},
            'object': {'key': 'OWA/raw/Harvest wb 1D0_13-20-40-1W4 r13 final Hayter vent flow.xlsm'},
        }
    }
    result = lambda_handler(test_event, None)
    print(json.dumps(result, indent=2))