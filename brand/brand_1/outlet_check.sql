-- =========================================================================
-- CHECK ENTITIES: source entities absent from the master reference.
-- =========================================================================

WITH trx AS (
  SELECT
    TRIM(t.entity_id) AS outlet_id_source,
    NULLIF(TRIM(t.entity_name), '') AS nama,
    t.phone_number AS nomor_wa,
    DATE(t.transaction_date) AS event_date
  FROM `your-project.your_dataset.transaction_header` AS t
  WHERE DATE(t.transaction_date) >= DATE_SUB(CURRENT_DATE(), INTERVAL @hari DAY)
    AND t.entity_id IS NOT NULL
),
master AS (
  SELECT DISTINCT TRIM(entity_id) AS outlet_id_source
  FROM `your-project.your_dataset.master_entity`
  WHERE entity_id IS NOT NULL
)

SELECT
  trx.outlet_id_source,
  ANY_VALUE(trx.nama) AS nama,
  ANY_VALUE(trx.nomor_wa) AS nomor_wa,
  COUNT(*) AS jumlah_transaksi,
  MIN(trx.event_date) AS pertama_muncul,
  MAX(trx.event_date) AS terakhir_muncul
FROM trx
LEFT JOIN master ON trx.outlet_id_source = master.outlet_id_source
WHERE master.outlet_id_source IS NULL
GROUP BY trx.outlet_id_source
ORDER BY jumlah_transaksi DESC
