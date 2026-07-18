CREATE
OR REPLACE VIEW owa_load_fluid_detail AS
SELECT
    l.uwi,
    l.project_number,
    l.client,
    l.afe_number,
    m.well_name,
    m.project_descriptor,
    m.surface_location,
    UPPER(m.area) AS area,
    l.date,
    l.category,
    l.tank,
    l.ticket_company,
    l.source,
    l.destination,
    l.fluid_type,
    l.m3
FROM
    owa_invoices.load_fluid l
    LEFT JOIN owa_invoices.main m ON l.uwi = m.uwi