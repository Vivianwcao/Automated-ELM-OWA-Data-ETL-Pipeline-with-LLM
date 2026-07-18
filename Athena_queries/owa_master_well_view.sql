CREATE
OR REPLACE VIEW owa_master_well_summary AS
SELECT
    m.uwi,
    m.client,
    m.well_name,
    m.surface_location,
    m.license,
    UPPER(m.area) AS area,
    m.project_manager,
    m.date,
    m.afe_number,
    m.afe_amount,
    UPPER(m.project_descriptor) AS project_descriptor,
    m.dds_number,
    m.dds_number_llm,
    m.scvf_result,
    CASE
        WHEN m.job_complete = TRUE THEN 'Yes'
        WHEN m.job_complete = false THEN 'No'
        ELSE NULL
    END AS job_complete,
    m.issues_noted,
    m.report_number,
    m.total_costs_to_date,
    m.project_number,
    m.casing_size,
    m.cost_centre,
    m.soc_prepared_by,
    m.soc_kb,
    m.soc_gl,
    m.soc_kb_gl,
    m.soc_kb_tf,
    m.next_operations,
    -- Cost totals calculated from charge lines
    SUM(c.amount) AS total_cost,
    m.afe_amount - SUM(c.amount) AS afe_variance,
    SUM(
        CASE
            WHEN c.charge_type = 'THIRD PARTY CHARGES' THEN COALESCE(c.thrdpty_man_hours, 0)
            ELSE 0
        END
    ) AS total_man_hours,
    -- Plug summary from well events
    COUNT(
        DISTINCT CASE
            WHEN w.event_type = 'bridge_plug'
            AND w.source IN ('llm', 'summary_of_changes') THEN w.depth_mkb
        END
    ) AS plug_count,
    MIN(
        CASE
            WHEN w.event_type = 'bridge_plug' THEN w.depth_mkb
        END
    ) AS shallowest_plug_mkb,
    MAX(
        CASE
            WHEN w.event_type = 'bridge_plug' THEN w.depth_mkb
        END
    ) AS deepest_plug_mkb,
    COUNT(
        DISTINCT CASE
            WHEN w.event_type = 'perforation' THEN w.depth_mkb
        END
    ) AS perforation_count,
    COUNT(
        DISTINCT CASE
            WHEN w.event_type = 'cement_squeeze' THEN w.depth_mkb
        END
    ) AS cement_squeeze_count,
    m.executive_summary_raw
FROM
    owa_invoices.main m
    LEFT JOIN owa_invoices.charges c ON m.uwi = c.uwi
    AND COALESCE(m.project_number, '') = COALESCE(c.project_number, '')
    LEFT JOIN owa_invoices.wells w ON m.uwi = w.uwi
    AND COALESCE(m.project_number, '') = COALESCE(w.project_number, '')
GROUP BY
    m.uwi,
    m.client,
    m.well_name,
    m.surface_location,
    m.license,
    m.area,
    m.project_manager,
    m.date,
    m.afe_number,
    m.afe_amount,
    m.project_descriptor,
    m.dds_number,
    m.dds_number_llm,
    m.scvf_result,
    m.job_complete,
    m.issues_noted,
    m.next_operations,
    m.executive_summary_raw,
    m.report_number,
    m.total_costs_to_date,
    m.project_number,
    m.casing_size,
    m.cost_centre,
    m.soc_prepared_by,
    m.soc_kb,
    m.soc_gl,
    m.soc_kb_gl,
    m.soc_kb_tf;