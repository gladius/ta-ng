
WITH calls AS (
  SELECT COALESCE(`vegas_usecase_name`, '') AS usecase, 'x' AS truth,
         FARM_FINGERPRINT(CONCAT(
           ARRAY_TO_STRING(ARRAY(SELECT JSON_VALUE(t, '$.function.name') FROM UNNEST(JSON_QUERY_ARRAY(`request_payload`, '$.tools')) t
                                 WHERE JSON_VALUE(t, '$.function.name') IS NOT NULL ORDER BY 1), ','),
           '|',
           SUBSTR(masked, 1, 300))) AS k,
         FARM_FINGERPRINT(masked) AS whole
  FROM (SELECT *, REGEXP_REPLACE(REGEXP_REPLACE(LOWER(IF(JSON_VALUE(`request_payload`, '$.messages[0].role') IN ('system', 'developer'),
  (SELECT COALESCE(JSON_VALUE(m, '$.content'),
                ARRAY_TO_STRING(ARRAY(SELECT JSON_VALUE(p, '$.text') FROM UNNEST(JSON_QUERY_ARRAY(m, '$.content')) p), '\n'), '') FROM UNNEST([JSON_QUERY(`request_payload`, '$.messages[0]')]) m),
  COALESCE(NULLIF(ARRAY_TO_STRING(ARRAY(SELECT COALESCE(JSON_VALUE(m, '$.content'),
                ARRAY_TO_STRING(ARRAY(SELECT JSON_VALUE(p, '$.text') FROM UNNEST(JSON_QUERY_ARRAY(m, '$.content')) p), '\n'), '') FROM UNNEST(JSON_QUERY_ARRAY(`request_payload`, '$.messages')) m WITH OFFSET o
                                        WHERE JSON_VALUE(m, '$.role') IN ('system', 'developer') ORDER BY o), '\n'), ''),
           (SELECT SUBSTR(x, 1, 8000) FROM (SELECT COALESCE(JSON_VALUE(m, '$.content'),
                ARRAY_TO_STRING(ARRAY(SELECT JSON_VALUE(p, '$.text') FROM UNNEST(JSON_QUERY_ARRAY(m, '$.content')) p), '\n'), '') AS x, o FROM UNNEST(JSON_QUERY_ARRAY(`request_payload`, '$.messages')) m WITH OFFSET o)
            WHERE LENGTH(TRIM(x)) > 0 ORDER BY o LIMIT 1),
           ''))), r'[0-9]+', '#'), r'\s+', ' ') AS masked FROM `PROD_TABLE`)
  WHERE DATE(`created_timestamp_utc`) BETWEEN DATE 'YYYY-MM-DD' AND DATE 'YYYY-MM-DD' AND JSON_QUERY(`request_payload`, '$.messages') IS NOT NULL AND NOT EXISTS (SELECT 1 FROM UNNEST(['user-%', 'unknown']) p WHERE COALESCE(`vegas_usecase_name`, '') LIKE p)
),
keys AS (
  SELECT usecase, k, COUNT(*) AS n, MAX(tn) AS top_truth, ANY_VALUE(truth HAVING MAX tn) AS truth, COUNT(DISTINCT whole) = 1 AS static
  FROM (SELECT usecase, k, truth, whole, COUNT(*) OVER (PARTITION BY usecase, k, truth) AS tn FROM calls)
  GROUP BY 1, 2
),
truths AS (
  SELECT usecase, truth, COUNT(DISTINCT k) AS split FROM keys WHERE n >= 5 GROUP BY 1, 2
)
SELECT k.usecase, SUM(n) AS calls, COUNT(*) AS keys,
       COUNTIF(n >= 5) AS big_keys, SUM(IF(n >= 5, n, 0)) AS big_calls,
       COUNTIF(n < 5) AS small_keys, SUM(IF(n < 5, n, 0)) AS small_calls,
       SUM(IF(n >= 5 AND static, n, 0)) AS static_calls,
       SAFE_DIVIDE(SUM(IF(n >= 5, top_truth, 0)), SUM(IF(n >= 5, n, 0))) AS purity,
       (SELECT COUNT(*) FROM truths t WHERE t.usecase = k.usecase) AS true_sites,
       (SELECT SUM(split) FROM truths t WHERE t.usecase = k.usecase) AS big_keys_of_true_sites
FROM keys k GROUP BY 1 ORDER BY calls DESC

