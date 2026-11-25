-- =========================================================================
-- CHECK PRODUCT CODES: find unregistered codes with matching product names.
-- =========================================================================

WITH trx AS (
  SELECT
    d.product_code,
    DATE(t.transaction_date) AS transaction_date,
    t.transaction_payload
  FROM `your-project.your_dataset.transaction_header`        AS t
  JOIN `your-project.your_dataset.transaction_detail` AS d
    ON d.transaction_id = t.transaction_id
  WHERE DATE(t.transaction_date) >= DATE_SUB(CURRENT_DATE(), INTERVAL @hari DAY)
),
master AS (
  SELECT DISTINCT
    UPPER(TRIM(product_code)) AS product_code,
    UPPER(TRIM(product_name)) AS product_name_normalized
  FROM `your-project.your_dataset.master_product`
),
tidak_dikenal AS (
  SELECT trx.*
  FROM trx
  LEFT JOIN master ON UPPER(TRIM(trx.product_code)) = master.product_code
  WHERE master.product_code IS NULL
),
dengan_nama AS (
  SELECT
    tidak_dikenal.product_code,
    tidak_dikenal.transaction_date,
    JSON_VALUE(detail_item, '$.productName') AS product_name
  FROM tidak_dikenal,
  UNNEST(JSON_EXTRACT_ARRAY(tidak_dikenal.transaction_payload, '$.items')) AS detail_item
  WHERE UPPER(TRIM(JSON_VALUE(detail_item, '$.productCode'))) = UPPER(TRIM(tidak_dikenal.product_code))
),
ringkas AS (
  SELECT
    UPPER(TRIM(product_code)) AS kode_produk_tidak_dikenal,
    ANY_VALUE(product_name) AS nama_produk,
    COUNT(*) AS jumlah_transaksi,
    MIN(transaction_date) AS pertama_muncul,
    MAX(transaction_date) AS terakhir_muncul
  FROM dengan_nama
  GROUP BY kode_produk_tidak_dikenal
)

SELECT
  ringkas.kode_produk_tidak_dikenal,
  ringkas.nama_produk,
  master.product_code AS kode_produk_mirip_di_master,
  ringkas.jumlah_transaksi,
  ringkas.pertama_muncul,
  ringkas.terakhir_muncul
FROM ringkas
JOIN master
  ON UPPER(TRIM(ringkas.nama_produk)) = master.product_name_normalized
ORDER BY ringkas.jumlah_transaksi DESC
