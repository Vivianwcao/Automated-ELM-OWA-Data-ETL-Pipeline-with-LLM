# Automated Well Abandonment Data Pipeline
## Serverless Data Lakehouse Architecture for Operational Analytics (OLAP)

### Tech Stack & Environment
* **Core Languages & Libraries:** Python, Pandas, OpenPyXL, PyArrow
* **Ingestion & Serverless Compute:** AWS AppFlow, Amazon S3, Amazon EventBridge, Amazon SQS, AWS Lambda
* **AI & Operational Enrichment:** Claude Haiku (AWS Bedrock)
* **Catalog, SQL & BI:** AWS Glue, Amazon Athena, Amazon QuickSight (SPICE)

---

## Project Overview and Scope

### The Business Problem
ELM (EnterMyInvoice) manages well abandonment projects in Alberta on behalf of oil and gas operators like Harvest Operations. For every job, ELM coordinates field contractors, tracks daily costs, and generates invoice packages. 

The core challenge was that ELM had zero cross job visibility. Every completed project existed as an isolated Excel workbook in Google Drive. While a project manager could open a single file to inspect one well, ELM could not answer portfolio level questions across a portfolio of 750 abandoned wells:
* Which wells exceeded their AFE (Authorization for Expenditure) budget, and by how much?
* What was the total spend per contractor across all jobs?
* How many wells encountered regulatory issues like failed pressure tests, leaky casing, or parted tubing?
* How many plugs were set across the portfolio, at what depths, and did they pass compliance checks?
* Which wells were completed versus those with outstanding work?

Answering these questions for Harvest Operations required manually opening 750 spreadsheets one by one, which was not feasible. Furthermore, demonstrating actual cost versus AFE budget with detailed supplier breakdowns is how ELM proves value to Harvest Operations and justifies its management fee on each job.

### The Solution
I built an event driven Medallion Data Lakehouse on AWS that automatically ingests messy field workbooks from Google Drive, parses visual layouts using `openpyxl`, extracts unstructured field context using Claude Haiku on Bedrock, and delivers clean datasets to Athena and QuickSight. This transformed a folder of disconnected Excel files into a professional analytics package for the client.

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

## Technical Challenges and Engineering Solutions

### 1. The Files Are Visual Forms, Not Spreadsheets
The field supervisors used Excel as a printed form layout tool rather than a database. Workbooks were built with merged cells, hand-aligned labels, and tables that started at different row offsets depending on how much notes the previous supervisor typed. With six tabs per file and subtle undocumented variations across 750 files from different contractors, standard parsing with `pd.read_excel()` failed on file one.

**The Solution:**
Instead of assuming fixed row coordinates, I built an anchor-based cell detection parser using `openpyxl`. The script scans for known label strings and reads neighboring cells at relative offsets to where the label was found. Section boundaries for charge tables and repeating line items are detected by keyword terminators rather than row counts. This handles layout shifts across files without breaking silently.

*(Insert Screenshot 1: Cost Summary Sheet showing visual layout and repeating line items)*
<img width="987" height="823" alt="image" src="https://github.com/user-attachments/assets/1811a3ca-6f38-4f14-9b1f-cf8eb42ae4f1" />


---

### 2. The Summary of Changes Tab Was One of the Trickiest Parts
This tab is a regulatory summary filled out by the engineer after job completion, not the field supervisor on site. It contains the most reliable structured data in the whole workbook, including Kelly Bushing (KB) elevation, Ground Level (GL), Total Depth (TD), Plug Back Total Depth (PBTD), and three sub-tables listing downhole installations, cement squeezes, and previous perforations.

None of it sits in a clean table. It is a visual form spread across a fixed region of the sheet with labels and values scattered across merged cells. Header fields like KB, GL, KB minus GL, and PBTD each required individual anchor searches within tightly bounded row and column slices because they sit in different column ranges across adjacent rows.

The cement squeeze table presented another hurdle. Engineers typed volumes as `"3.0"`, `"3.0m3"`, `"22.7 Tonne / 17m3"`, or just `"3"` with no unit. 

**The Solution:**
I wrote a dedicated `parse_volume()` function to handle all these string variants and split them into separate `volume_m3` and `volume_tonne` float fields so the data could actually be queried downstream.

*(Insert Screenshot 2: Summary of Changes Tab)*
<img width="1346" height="821" alt="image" src="https://github.com/user-attachments/assets/6da712ab-33e7-4c66-813c-e8481df8393a" />

---

### 3. Daily and Job Notes Could Not Be Parsed with Regex Alone
The Main Data sheet (Job) and every daily sheet contains a free-text section written by the field supervisor. These paragraphs hold the most operationally valuable details in the file, such as plug depths, pressure test outcomes, cement volumes, and equipment failures. Regex can pull numbers, but it cannot determine engineering context.

For example, a supervisor note might say the well was pressured to 13 MPa to shear the setting tool, and then describe a separate pressure test on the next line at 7 MPa held for 15 minutes. Both entries contain a pressure value, but only one is a regulatory test result. That distinction is critical for compliance reporting and cannot be made reliably with pattern matching across hundreds of files written by different supervisors.

**The Solution:**
I integrated Claude Haiku on AWS Bedrock inside Lambda 2. A structured prompt sends the daily free-text summaries alongside the parsed Summary of Changes data. The model returns clean JSON per file containing classified well events, pressure test results with pass/fail flags, converted cement volumes, and AER regulatory numbers. Prompt engineering was the main effort here, specifically teaching the model the field-specific distinction between tool shear pressure and a post-set pressure test.

*(Insert Screenshot 3: Main Data Sheet showing notes for the entire job)*
<img width="932" height="815" alt="image" src="https://github.com/user-attachments/assets/88c04dc6-b68e-417c-ba14-369c8b656fdb" />

---

*(Insert Screenshot 4: Daily Summary Sheet showing daily notes)*
<img width="972" height="867" alt="image" src="https://github.com/user-attachments/assets/4a465fe8-31d7-458f-87dc-dd13dc431a85" />


---


### 4. Handling 750 Files Arriving Simultaneously
AWS AppFlow transfers all files from Google Drive in bulk. Without rate control, dropping 750 files into S3 simultaneously would spawn hundreds of concurrent Lambda invocations and hit AWS account concurrency limits, causing silent failures with no visibility into lost data.

**The Solution:**
I decoupled the pipeline using two SQS queues. SQS Queue 1 buffers raw file transfers between S3 and Lambda 1, while SQS Queue 2 buffers intermediate JSON files between S3 and Lambda 2. Both queues use dedicated Dead-Letter Queues (DLQs). If a file fails processing after maximum retries, it lands in the DLQ, triggers a CloudWatch depth alarm, and sends an SNS email alert. Failed files can be analyzed, fixed, and redriven directly out of the DLQ without re-uploading source files from Google Drive.

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

[![Watch Demo](https://img.shields.io/badge/▶️_Watch_the_QuickSight_Dashboard_Video_Demo-94f2a8?style=for-the-badge)](https://youtu.be/aFxhm_Saku0)
<p align="center">
  <a href="https://youtu.be/aFxhm_Saku0" target="_blank">
    <img src="https://img.youtube.com/vi/aFxhm_Saku0/maxresdefault.jpg" alt="QuickSight Dashboard Demo" width="100%" />
  </a>
</p>


---

## Monitoring & Error Handling
* **Dead-Letter Queues (DLQs):** SQS Queue 1 and Queue 2 are configured with DLQs to catch payloads that fail processing after maximum retry attempts.
* **Alerting:** A CloudWatch metric alarm monitors DLQ depth and triggers an **SNS email alert** if a message lands in the DLQ.
* **Redrive Strategy:** Failed files can be analyzed, corrected, and redriven directly from the DLQ without re-uploading source files from Google Drive.
