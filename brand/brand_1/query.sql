-- =========================================================================
-- BIGQUERY SOURCE DATA
--
--  KOLOM (wajib, nama persis seperti ini):
--     outlet_id_source STRING   source entity identifier
--     nama          STRING   boleh NULL -> menentukan template v1 / v2
--     nomor_wa      STRING   mentah, biar core yang merapikan
--     segmen        STRING   segment_01 | segment_02 | segment_03
--     event_date    DATE     tanggal kejadian (transaksi)
--     total_qty     INT64    total unit terjual, customer x segmen ini saja.
--                            Dipakai core/transform.py untuk menentukan
--                            segmen pemenang kalau satu customer belanja
--                            lintas segmen (lihat segment_selection: quantity
--                            di brand/brand_1/config.yaml). WAJIB untuk brand_1 -
--                            brand lain boleh skip kalau segment_selection
--                            masih 'fixed' (default).
--

WITH
trx AS (
  SELECT
    t.transaction_id,
    t.entity_name,
    t.phone_number,
    t.entity_id,
    DATE(t.transaction_date) AS transaction_date,
    d.product_code,
    d.quantity
  FROM `your-project.your_dataset.transaction_header` AS t
  JOIN `your-project.your_dataset.transaction_detail` AS d
    ON d.transaction_id = t.transaction_id
  WHERE DATE(t.transaction_date) = DATE_SUB(@as_of, INTERVAL @lookback_days DAY)
),

-- -------------------------------------------------------------------------
-- MASTER DATA:
-- Source categories map to generic segment identifiers in the sync script.
-- -------------------------------------------------------------------------
master AS (
  SELECT
    UPPER(TRIM(product_code)) AS product_code,
    LOWER(TRIM(segment)) AS segment
  FROM `your-project.your_dataset.master_product`
  WHERE segment IS NOT NULL
),

bersegmen AS (
  SELECT
    trx.entity_name,
    trx.phone_number,
    trx.entity_id,
    trx.transaction_date,
    trx.quantity,
    master.segment
  FROM trx
  JOIN master
    ON UPPER(TRIM(trx.product_code)) = master.product_code
),

entity_valid AS (
  SELECT DISTINCT TRIM(entity_id) AS entity_id
  FROM `your-project.your_dataset.master_entity`
  WHERE entity_id IS NOT NULL
)

SELECT
  TRIM(bersegmen.entity_id) AS outlet_id_source,
  NULLIF(TRIM(bersegmen.entity_name), '') AS nama,
  bersegmen.phone_number AS nomor_wa,
  bersegmen.segment AS segmen,
  MAX(bersegmen.transaction_date) AS event_date,
  SUM(bersegmen.quantity) AS total_qty
FROM bersegmen
JOIN entity_valid
  ON TRIM(bersegmen.entity_id) = entity_valid.entity_id
GROUP BY outlet_id_source, nama, nomor_wa, segmen
ORDER BY event_date DESC, outlet_id_source
