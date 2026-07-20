# Automated Well Abandonment Data Pipeline
## Serverless Data Lakehouse Architecture for Operational Analytics (OLAP)

### Tech Stack & Environment
* **Core Languages & Libraries:** Python, Pandas, OpenPyXL, PyArrow
* **Ingestion & Serverless Compute:** AWS AppFlow, Amazon S3, Amazon EventBridge, Amazon SQS, AWS Lambda
* **AI & Operational Enrichment:** Claude Haiku (AWS Bedrock)
* **Catalog, SQL & BI:** AWS Glue, Amazon Athena, Amazon QuickSight (SPICE)

---

## Project Overview & Scope
* **The Business Problem:** Over five years of Alberta field operations, supervisors generated 750+ multi tab `.xlsm` workbooks that acted as job site invoices and daily reports. Financial and Alberta Energy Regulator compliance data was trapped in visual spreadsheet forms, leaving management unable to audit cross well costs or verify regulatory compliance.
* **The Domain Challenge:** Key metrics like plug depths, cement volumes, and pressure test outcomes were buried in unstructured field notes. Regex failed because it lacked engineering context, like distinguishing tool shear pressure from a post set regulatory pressure test.
* **The Technical Solution:** I built an event driven Medallion Data Lakehouse on AWS. The pipeline ingests messy workbooks via AppFlow, parses visual layouts using `openpyxl` cell anchors, extracts domain context with Claude Haiku on Bedrock, and feeds clean datasets into Athena and QuickSight for automated cost and compliance tracking.
---

## Pipeline Architecture & Data Flow
<img width="2720" height="2640" alt="well_abandonment_pipeline_v4" src="https://github.com/user-attachments/assets/c27e89b5-cacf-48ec-9639-9d1cba4dc5d4" />

### Medallion Data Processing Stages
* **Bronze Layer (`S3 raw/`):** Raw, multi-tab `.xlsm` workbooks bulk ingested from Google Drive via AWS AppFlow.
* **Silver Layer (`S3 extracted/` & `S3 clean/`):** 
  * **Extraction Sub-Stage (`S3 extracted/`):** Lambda 1 parses each `.xlsm` file and generates **one single JSON file per workbook**, structured with 5 distinct JSON objects corresponding to the 5 workbook tabs.
  * **Clean Parquet Sub-Stage (`S3 clean/`):** Lambda 2 runs LLM enrichment on free-text notes, enforces schema typing, and writes the output into **5 separate Parquet subfolders** (one per tab domain). AWS Glue crawls these Parquet subfolders to build 5 base Athena tables.
* **Gold Layer (Curated Athena Views):** The Gold layer consists of **5 curated SQL views** in Athena that join, aggregate, and normalize data across the 5 base Parquet tables. These business-ready views serve as the source layer for downstream BI reporting in Amazon QuickSight.

---

## Technical Challenges & Engineering Solutions

### 1. Ingestion Traffic Spikes vs. Lambda Rate Limits
* **Challenge:** Transferring 750 historical files at once via AppFlow lands all objects in S3 almost simultaneously. Direct S3-to-Lambda event triggers would instantly spike concurrent invocations and hit AWS account concurrency limits.
* **Solution:** Decoupled processing using **Amazon EventBridge and SQS Queues**. EventBridge captures file upload events and pushes them into an SQS buffer. Downstream Lambda functions poll SQS at a controlled execution rate, preventing throttling.

### 2. Layout Variation & Visual Spreadsheet Parsing
* **Challenge:** Field supervisors filled out `.xlsm` workbooks over 5 years, treating Excel as a visual layout tool or digital form rather than a relational database (merged cells, variable row placements, hand-aligned labels, and repeating line items). Traditional cell coordinate parsing via `pd.read_excel()` broke constantly due to layout drift across years.
* **Solution:** Developed an **anchor-based detection parser with continuous repeating extraction** using `openpyxl`. The script scans sheet tabs for known keyword labels, reads neighboring values relative to offset anchor positions, and iteratively parses repeating line items (functioning like layout-aware visual extraction for spreadsheets).

### 3. Context Extraction from Unstructured Daily Summaries
* **Challenge:** The daily summary field in each workbook contained critical operational data (e.g., plug depths, cement volumes, pressure test results) embedded within long free-text notes. Standard regex could isolate numerical values but lacked the domain awareness to determine context (e.g., distinguishing tool activation pressure from a post-set regulatory pressure test).
* **Solution:** Integrated **Claude Haiku via AWS Bedrock** inside Lambda 2. The daily free-text string is sent through a structured prompt to evaluate the engineering context and extract standardized, strictly-typed JSON containing pass/fail flags, regulatory numbers, and classified well event types.

**Sample XLSM Workbook tabs**
<img width="1346" height="821" alt="image" src="https://github.com/user-attachments/assets/6da712ab-33e7-4c66-813c-e8481df8393a" />
<img width="932" height="815" alt="image" src="https://github.com/user-attachments/assets/88c04dc6-b68e-417c-ba14-369c8b656fdb" />


### 4. Schema Coercion & Catalog Stability
* **Challenge:** Processing hundreds of independently created files leads to implicit type mismatches in Pandas/PyArrow (e.g., empty columns inferred as `float` in one file and `string` in another), causing Glue Crawlers or Athena queries to fail.
* **Solution:** Enforced a centralized `MASTER_SCHEMA` dictionary during Lambda 2 execution. Every DataFrame is forcibly coerced prior to Parquet conversion: missing columns are injected as nulls, numbers pass through `pd.to_numeric(errors='coerce')`, and booleans map to pandas' nullable Boolean type.

---

## Analytics & Downstream BI Layer

### Athena Base Tables & Gold SQL Views
AWS Glue catalogs the Silver Parquet datasets into 5 base tables in Athena. To normalize inconsistent formatting and join related operational data across tables, 5 Gold SQL views were created:
1. **Cost by Supplier View:** Normalizes unit prices, item descriptions, and vendor names for cost tracking.
2. **Load Fluid Detail View:** Tracks fluid types, volumes, and disposal metrics across jobs.
3. **Master Well Summary View:** Joins high-level well identifiers, AER regulatory numbers, and execution dates.
4. **Material Transfer Detail View:** Tracks equipment and material movements between well locations.
5. **Plug & Cement Detail View:** Isolates regulatory compliance metrics, plug depths, and pressure test outcomes extracted by the LLM.

### QuickSight Reporting
Amazon QuickSight connects to the Gold Athena SQL views via the **SPICE in-memory calculation engine** on a periodic refresh schedule. This isolates dashboard user traffic (filtering bar charts, cost trends, and compliance metrics) from executing live S3 queries in Athena, minimizing query costs and latency.


https://github.com/user-attachments/assets/ad0d67b7-3b11-43b2-a1fc-7dd6e336887f



---

## Monitoring & Error Handling
* **Dead-Letter Queues (DLQs):** SQS Queue 1 and Queue 2 are configured with DLQs to catch payloads that fail processing after maximum retry attempts.
* **Alerting:** A CloudWatch metric alarm monitors DLQ depth and triggers an **SNS email alert** if a message lands in the DLQ.
* **Redrive Strategy:** Failed files can be analyzed, corrected, and redriven directly from the DLQ without re-uploading source files from Google Drive.
