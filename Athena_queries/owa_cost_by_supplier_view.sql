CREATE
OR REPLACE VIEW owa_cost_by_supplier AS
SELECT
    c.uwi,
    m.well_name,
    UPPER(m.area) AS area,
    m.client,
    m.date,
    c.charge_type,
    c.service_provided,
    c.contractor,
    c.ticket_number,
    c.po_number,
    c.number_of_units,
    c.rate,
    c.amount,
    c.subtotal_with_mgt_fee,
    c.thrdpty_man_hours,
    c.resource_name,
    c.kilometers,
    c.thrdpty_subtotal,
    c.rates_elm_fraction,
    c.rates_thrdpty_fraction,
    c.d_report_number AS day_number
FROM
    owa_invoices.charges c
    LEFT JOIN owa_invoices.main m ON c.uwi = m.uwi
    AND COALESCE(c.project_number, '') = COALESCE(m.project_number, '')
WHERE
    c.amount IS NOT NULL;