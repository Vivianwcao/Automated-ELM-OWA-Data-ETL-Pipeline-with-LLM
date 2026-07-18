CREATE
OR REPLACE VIEW owa_material_transfer_detail AS
SELECT
    mt.uwi,
    mt.project_number,
    mt.client,
    mt.afe_number,
    m.well_name,
    m.project_descriptor,
    m.surface_location,
    UPPER(m.area) AS area,
    mt.quantity,
    mt.item,
    mt.condition,
    mt.transferred_to
FROM
    owa_invoices.material_transfer mt
    LEFT JOIN owa_invoices.main m ON mt.uwi = m.uwi