-- Generic example source query. Replace all columns and eligibility categories
-- with the authorized source schema before running.
WITH eligible_events AS (
  SELECT
    source_name AS nama,
    source_phone AS nomor_wa,
    DATE(SAFE_CAST(source_timestamp AS DATETIME)) AS event_date,
    source_product_name AS nama_produk,
    source_product_code AS kode_produk
  FROM `your-project.your_dataset.source_events`
  WHERE is_eligible IS TRUE
    AND LOWER(TRIM(source_category)) IN ('category_01', 'category_02')
)

SELECT
  nama, nomor_wa, event_date, nama_produk, kode_produk,
  event_date AS tanggal_pembelian
FROM eligible_events
WHERE event_date = DATE_SUB(@as_of, INTERVAL @lookback_days DAY)
