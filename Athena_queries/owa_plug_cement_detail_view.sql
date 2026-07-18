CREATE
OR REPLACE VIEW owa_plug_cement_detail AS
SELECT
    w.uwi,
    m.well_name,
    UPPER(m.area) AS area,
    m.client,
    m.license,
    m.date,
    m.dds_number,
    m.dds_number_llm,
    w.event_type,
    w.description,
    w.depth_mkb,
    w.depth_to_mkb,
    CASE
        WHEN w.pressure_test_passed = TRUE THEN 'Pass'
        WHEN w.pressure_test_passed = false THEN 'Fail'
        ELSE NULL
    END AS pressure_test_passed,
    w.pressure_test_kpa,
    w.pressure_test_duration_min,
    w.cement_blend,
    w.volume_m3,
    w.volume_tonne,
    w.volume_m3_llm,
    w.attempt_number,
    w.report_number AS day_number,
    w.source AS data_source,
    wd.daily_notes_raw,
    wd.d_operation_summary
FROM
    owa_invoices.wells w
    LEFT JOIN owa_invoices.main m ON w.uwi = m.uwi
    AND COALESCE(w.project_number, '') = COALESCE(m.project_number, '')
    LEFT JOIN owa_invoices.wells wd ON w.uwi = wd.uwi
    AND COALESCE(w.project_number, '') = COALESCE(wd.project_number, '')
    AND w.report_number = wd.report_number
    AND wd.source = 'days'
    AND wd.event_type = 'daily_report'
WHERE
    w.event_type IN (
        'bridge_plug',
        'cement',
        'cement_squeeze',
        'perforation',
        'bond_log'
    )
    AND w.source IN ('llm', 'summary_of_changes')
ORDER BY
    w.uwi,
    w.depth_mkb;