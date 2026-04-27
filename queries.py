##### queries.py #####
# quarda queries SQL
# cada query é uma string multi-linha, e deve ser escrita de forma a ser legível e fácil de manter
##################

QUERY_TEMPO_PRODUCAO_MD = """
WITH
now_ctx AS (
  SELECT
    now() AS now_ts,
    (now() AT TIME ZONE 'Europe/Lisbon')::timestamp AS now_local
),
base AS (
  SELECT
    date_trunc('day', now_local)::timestamp AS today_local,
    CASE
      WHEN EXTRACT(ISODOW FROM now_local) = 1
        THEN (date_trunc('day', now_local) - interval '3 days')::timestamp
      ELSE (date_trunc('day', now_local) - interval '1 day')::timestamp
    END AS day1_local,
    now_ts
  FROM now_ctx
),
shift_def AS (
  SELECT * FROM (VALUES
    (1, '08-16', interval '8 hours',  interval '16 hours'),
    (2, '16-24', interval '16 hours', interval '24 hours'),
    (3, '00-08', interval '24 hours', interval '32 hours')
  ) v(turno_id, turno, start_off, end_off)
),
shifts AS (
  SELECT
    sd.turno_id,
    sd.turno,
    (b.day1_local + sd.start_off) AT TIME ZONE 'Europe/Lisbon' AS start_ts,
    (b.day1_local + sd.end_off)   AT TIME ZONE 'Europe/Lisbon' AS end_ts,
    CASE
      WHEN b.now_ts < ((b.day1_local + sd.start_off) AT TIME ZONE 'Europe/Lisbon')
        THEN ((b.day1_local + sd.start_off) AT TIME ZONE 'Europe/Lisbon')
      WHEN b.now_ts > ((b.day1_local + sd.end_off) AT TIME ZONE 'Europe/Lisbon')
        THEN ((b.day1_local + sd.end_off) AT TIME ZONE 'Europe/Lisbon')
      ELSE b.now_ts
    END AS end_eff
  FROM base b
  CROSS JOIN shift_def sd
),
baseline AS (
  SELECT
    s.turno_id, s.turno, s.start_ts, s.end_eff,
    COALESCE(t.created_at, s.start_ts) AS created_at,
    COALESCE(t.carga, 0) AS carga,
    COALESCE(t.funcionamento, 0) AS funcionamento
  FROM shifts s
  LEFT JOIN LATERAL (
    SELECT x.created_at, x.carga, x.funcionamento
    FROM trituracao.md x
    WHERE x.created_at < s.start_ts
      AND x.created_at >= s.start_ts - interval '12 hours'
    ORDER BY x.created_at DESC
    LIMIT 1
  ) t ON true
),
in_shift AS (
  SELECT
    s.turno_id, s.turno, s.start_ts, s.end_eff,
    m.created_at, m.carga, m.funcionamento
  FROM shifts s
  JOIN trituracao.md m
    ON m.created_at >= s.start_ts
   AND m.created_at <  s.end_eff
),
samples AS (
  SELECT * FROM baseline
  UNION ALL
  SELECT * FROM in_shift
),
segments AS (
  SELECT
    turno_id, turno, start_ts, end_eff,
    created_at,
    carga,
    funcionamento,
    lead(created_at) OVER (
      PARTITION BY turno_id
      ORDER BY created_at
    ) AS next_at
  FROM samples
),
durations AS (
  SELECT
    turno_id, turno,
    EXTRACT(EPOCH FROM (
      LEAST(
        COALESCE(next_at, end_eff),
        created_at + interval '2 minutes',
        end_eff
      ) - GREATEST(created_at, start_ts)
    )) / 60.0 AS minutes,
    carga,
    funcionamento
  FROM segments
  WHERE LEAST(COALESCE(next_at, end_eff), created_at + interval '2 minutes', end_eff)
        > GREATEST(created_at, start_ts)
),
work_by_shift AS (
  SELECT
    turno_id, turno,
    SUM(CASE WHEN funcionamento = 1 THEN minutes ELSE 0 END) AS min_func,
    SUM(CASE WHEN funcionamento = 1 AND carga = 1 THEN minutes ELSE 0 END) AS min_carga
  FROM durations
  GROUP BY turno_id, turno
),
pivot AS (
  SELECT
    COALESCE(MAX(min_carga) FILTER (WHERE turno_id=1), 0) AS t1_carga,
    COALESCE(MAX(min_func)  FILTER (WHERE turno_id=1), 0) AS t1_func,
    COALESCE(MAX(min_carga) FILTER (WHERE turno_id=2), 0) AS t2_carga,
    COALESCE(MAX(min_func)  FILTER (WHERE turno_id=2), 0) AS t2_func,
    COALESCE(MAX(min_carga) FILTER (WHERE turno_id=3), 0) AS t3_carga,
    COALESCE(MAX(min_func)  FILTER (WHERE turno_id=3), 0) AS t3_func
  FROM work_by_shift
)
SELECT
  to_char(make_interval(mins => t1_func::int), 'HH24"h"MI')
    || ' (' || (CASE WHEN t1_func > 0 THEN round(100.0 * t1_carga / t1_func)::int ELSE 0 END) || '%)'
    AS "T1(08-16)",
  to_char(make_interval(mins => t2_func::int), 'HH24"h"MI')
    || ' (' || (CASE WHEN t2_func > 0 THEN round(100.0 * t2_carga / t2_func)::int ELSE 0 END) || '%)'
    AS "T2(16-24)",
  to_char(make_interval(mins => t3_func::int), 'HH24"h"MI')
    || ' (' || (CASE WHEN t3_func > 0 THEN round(100.0 * t3_carga / t3_func)::int ELSE 0 END) || '%)'
    AS "T3(00-08)",
  to_char(make_interval(mins => (t1_func + t2_func + t3_func)::int), 'HH24"h"MI')
    || ' (' || (
        CASE
          WHEN (t1_func + t2_func + t3_func) > 0
            THEN round(100.0 * (t1_carga + t2_carga + t3_carga) / (t1_func + t2_func + t3_func))::int
          ELSE 0
        END
      ) || '%)'
    AS "TOTAL"
FROM pivot;
"""
QUERY_HORAS_MOINHOS = """
WITH
now_local AS (
  SELECT (now() AT TIME ZONE 'Europe/Lisbon')::timestamp AS now_local
),
base AS (
  SELECT
    date_trunc('day', now_local)::timestamp AS today_local,
    CASE
      WHEN EXTRACT(ISODOW FROM now_local) = 1
        THEN (date_trunc('day', now_local) - interval '3 days')::timestamp   -- 2ª feira => 6ª feira
      ELSE (date_trunc('day', now_local) - interval '1 day')::timestamp     -- resto => ontem
    END AS day1_local
  FROM now_local
),
shift_def AS (
  SELECT * FROM (VALUES
    (1, '08-16', interval '8 hours', interval '16 hours'),
    (2, '16-24', interval '16 hours', interval '24 hours'),
    (3, '00-08', interval '24 hours', interval '32 hours')
    --(3, '00-08', interval '0 hour',  interval '8 hours')
  ) v(turno_id, turno, start_off, end_off)
),
days AS (
  SELECT * FROM (
    VALUES
      ('Dia',   (SELECT today_local FROM base)),
      ('Dia-1', (SELECT day1_local  FROM base))
  ) v(dia_ref, day_local)
),
shifts AS (
  SELECT
    d.dia_ref,
    sd.turno_id,
    sd.turno,
    (d.day_local + sd.start_off) AT TIME ZONE 'Europe/Lisbon' AS start_ts,
    (d.day_local + sd.end_off)   AT TIME ZONE 'Europe/Lisbon' AS end_ts
  FROM days d
  CROSS JOIN shift_def sd
),
shifts_eff AS (
  SELECT
    *,
    LEAST(end_ts, now()) AS end_eff
  FROM shifts
),
in_shift AS (
  SELECT
    s.dia_ref, s.turno_id, s.turno, s.start_ts, s.end_eff,
    t.sf_id, t.freq_sf, t.created_at
  FROM shifts_eff s
  JOIN trituracao.sem_fins t
    ON t.created_at >= s.start_ts
   AND t.created_at <  s.end_eff
  WHERE t.sf_id IN (1,2,3,4,5)  -- SF54/SF55/SF56/SF57/SF45
),
baseline AS (
  SELECT
    s.dia_ref, s.turno_id, s.turno, s.start_ts, s.end_eff,
    t.sf_id, t.freq_sf, t.created_at
  FROM shifts_eff s
  JOIN LATERAL (
    SELECT DISTINCT ON (x.sf_id)
      x.sf_id, x.freq_sf, x.created_at
    FROM trituracao.sem_fins x
    WHERE x.sf_id IN (1,2,3,4,5)
      AND x.created_at < s.start_ts
      AND x.created_at >= s.start_ts - interval '12 hours'
    ORDER BY x.sf_id, x.created_at DESC
  ) t ON true
),
samples AS (
  SELECT * FROM baseline
  UNION ALL
  SELECT * FROM in_shift
),
segments AS (
  SELECT
    dia_ref, turno_id, turno, start_ts, end_eff,
    sf_id, freq_sf, created_at,
    lead(created_at) OVER (
      PARTITION BY dia_ref, turno_id, sf_id
      ORDER BY created_at
    ) AS next_at
  FROM samples
),
durations AS (
  SELECT
    dia_ref, turno_id, turno, sf_id,
    CASE
      WHEN freq_sf > 1 THEN
        EXTRACT(EPOCH FROM (
          LEAST(
            COALESCE(next_at, end_eff),
            created_at + interval '2 minutes',  -- anti overcount se falharem amostras
            end_eff
          ) - GREATEST(created_at, start_ts)
        ))
      ELSE 0
    END AS work_seconds
  FROM segments
  WHERE LEAST(COALESCE(next_at, end_eff), created_at + interval '2 minutes', end_eff)
        > GREATEST(created_at, start_ts)
),
work_by_sf AS (
  SELECT
    dia_ref, turno_id, turno, sf_id,
    SUM(work_seconds) AS work_seconds
  FROM durations
  GROUP BY dia_ref, turno_id, turno, sf_id
),
work_by_shift AS (
  -- “considerar tempo de trabalho máximo entre os SF”
  SELECT
    dia_ref, turno_id, turno,
    MAX(work_seconds) AS work_seconds
  FROM work_by_sf
  GROUP BY dia_ref, turno_id, turno
),
pivot AS (
  SELECT
    COALESCE(MAX(work_seconds) FILTER (WHERE dia_ref='Dia-1' AND turno_id=1), 0) AS t1_sec,
    COALESCE(MAX(work_seconds) FILTER (WHERE dia_ref='Dia-1' AND turno_id=2), 0) AS t2_sec,
    COALESCE(MAX(work_seconds) FILTER (WHERE dia_ref='Dia-1' AND turno_id=3), 0) AS t3_sec
  FROM work_by_shift
)
SELECT
  to_char(make_interval(secs => t1_sec::int), 'HH24hMI') AS "T1(08-16)",
  to_char(make_interval(secs => t2_sec::int), 'HH24hMI') AS "T2(16-24)",
  to_char(make_interval(secs => t3_sec::int), 'HH24hMI') AS "T3(00-08)",
  to_char(make_interval(secs => (t1_sec + t2_sec + t3_sec)::int), 'HH24hMI') AS "TOTAL"
FROM pivot;
"""
QUERY_KGS_SILOS = """
WITH prod AS (
  WITH
  now_local AS (
    SELECT (now() AT TIME ZONE 'Europe/Lisbon')::timestamp AS now_local
  ),
  base AS (
    SELECT
      date_trunc('day', now_local)::timestamp AS today_local,
      CASE
        WHEN EXTRACT(ISODOW FROM now_local) = 1
          THEN (date_trunc('day', now_local) - interval '3 days')::timestamp   -- 2ª feira => 6ª feira
        ELSE (date_trunc('day', now_local) - interval '1 day')::timestamp     -- resto => ontem
      END AS day1_local
    FROM now_local
  ),
  shift_def AS (
    SELECT * FROM (VALUES
      (1, '08-16', interval '8 hours', interval '16 hours'),
      (2, '16-24', interval '16 hours', interval '24 hours'),
      (3, '00-08', interval '24 hours', interval '32 hours')
      --(3, '00-08', interval '0 hour',  interval '8 hours')
    ) v(turno_id, turno, start_off, end_off)
  ),
  days AS (
    SELECT * FROM (
      VALUES
        ('Dia',   (SELECT today_local FROM base)),
        ('Dia-1', (SELECT day1_local  FROM base))
    ) v(dia_ref, day_local)
  ),
  shifts AS (
    SELECT
      d.dia_ref,
      sd.turno_id,
      sd.turno,
      (d.day_local + sd.start_off) AT TIME ZONE 'Europe/Lisbon' AS start_ts,
      (d.day_local + sd.end_off)   AT TIME ZONE 'Europe/Lisbon' AS end_ts
    FROM days d
    CROSS JOIN shift_def sd
  ),
  in_shift AS (
    SELECT
      s.dia_ref, s.turno_id, s.turno,
      t.silo_id, t.estado_silo, t.qtd_silo, t.created_at
    FROM shifts s
    JOIN trituracao.silos1a5 t
      ON t.created_at >= s.start_ts
     AND t.created_at <  s.end_ts
  ),
  baseline AS (
    SELECT
      s.dia_ref, s.turno_id, s.turno,
      t.silo_id, t.estado_silo, t.qtd_silo, t.created_at
    FROM shifts s
    JOIN LATERAL (
      SELECT DISTINCT ON (x.silo_id)
        x.*
      FROM trituracao.silos1a5 x
      WHERE x.created_at < s.start_ts
        AND x.created_at >= s.start_ts - interval '12 hours'
      ORDER BY x.silo_id, x.created_at DESC
    ) t ON true
  ),
  samples AS (
    SELECT * FROM baseline
    UNION ALL
    SELECT * FROM in_shift
  ),
  deltas AS (
    SELECT
      dia_ref, turno_id, turno, silo_id, created_at, estado_silo,
      qtd_silo::numeric AS qtd_silo,
      (qtd_silo::numeric - lag(qtd_silo::numeric) OVER (
        PARTITION BY dia_ref, turno_id, silo_id
        ORDER BY created_at
      )) AS delta_kg
    FROM samples
  )
  SELECT
    dia_ref, turno_id, turno,
    sum(
      CASE
        WHEN estado_silo = 1 AND delta_kg > 0 THEN delta_kg
        ELSE 0
      END
    ) AS kg_produzidos
  FROM deltas
  GROUP BY dia_ref, turno_id, turno
),
pivot AS (
  SELECT
    coalesce(max(kg_produzidos) FILTER (WHERE turno_id=1 AND dia_ref='Dia-1'), 0) AS "T1(08-16)",
    coalesce(max(kg_produzidos) FILTER (WHERE turno_id=2 AND dia_ref='Dia-1'), 0) AS "T2(16-24)",
    coalesce(max(kg_produzidos) FILTER (WHERE turno_id=3 AND dia_ref='Dia-1'), 0) AS "T3(00-08)"
  FROM prod
)
SELECT
  "T1(08-16)",
  "T2(16-24)",
  "T3(00-08)",
  ("T1(08-16)" + "T2(16-24)" + "T3(00-08)") AS "TOTAL"
FROM pivot;
"""
QUERY_OEE = """ 
WITH
params AS (
  SELECT 1450 AS cadencia_kg_h
),
now_ctx AS (
  SELECT
    now() AS now_ts,
    (now() AT TIME ZONE 'Europe/Lisbon')::timestamp AS now_local
),
base AS (
  SELECT
    date_trunc('day', now_local)::timestamp AS today_local,
    CASE
      WHEN EXTRACT(ISODOW FROM now_local) = 1
        THEN (date_trunc('day', now_local) - interval '3 days')::timestamp
      ELSE (date_trunc('day', now_local) - interval '1 day')::timestamp
    END AS day1_local,
    now_ts
  FROM now_ctx
),
shift_def AS (
  SELECT * FROM (VALUES
    (1, '08-16', interval '8 hours',  interval '16 hours'),
    (2, '16-24', interval '16 hours', interval '24 hours'),
    (3, '00-08', interval '24 hours', interval '32 hours')
  ) v(turno_id, turno, start_off, end_off)
),
shifts AS (
  SELECT
    sd.turno_id,
    sd.turno,
    (b.day1_local + sd.start_off) AT TIME ZONE 'Europe/Lisbon' AS start_ts,
    (b.day1_local + sd.end_off)   AT TIME ZONE 'Europe/Lisbon' AS end_ts,
    CASE
      WHEN b.now_ts < ((b.day1_local + sd.start_off) AT TIME ZONE 'Europe/Lisbon')
        THEN ((b.day1_local + sd.start_off) AT TIME ZONE 'Europe/Lisbon')
      WHEN b.now_ts > ((b.day1_local + sd.end_off) AT TIME ZONE 'Europe/Lisbon')
        THEN ((b.day1_local + sd.end_off) AT TIME ZONE 'Europe/Lisbon')
      ELSE b.now_ts
    END AS end_eff
  FROM base b
  CROSS JOIN shift_def sd
),
in_shift AS (
  SELECT
    s.turno_id, s.turno, s.start_ts, s.end_eff,
    t.silo_id, t.estado_silo, t.qtd_silo, t.created_at
  FROM shifts s
  JOIN trituracao.silos1a5 t
    ON t.created_at >= s.start_ts
   AND t.created_at <  s.end_eff
),
baseline AS (
  SELECT
    s.turno_id, s.turno, s.start_ts, s.end_eff,
    t.silo_id, t.estado_silo, t.qtd_silo, t.created_at
  FROM shifts s
  JOIN LATERAL (
    SELECT DISTINCT ON (x.silo_id)
      x.*
    FROM trituracao.silos1a5 x
    WHERE x.created_at < s.start_ts
      AND x.created_at >= s.start_ts - interval '12 hours'
    ORDER BY x.silo_id, x.created_at DESC
  ) t ON true
),
samples AS (
  SELECT * FROM baseline
  UNION ALL
  SELECT * FROM in_shift
),
deltas AS (
  SELECT
    turno_id, turno, start_ts, end_eff,
    silo_id, created_at, estado_silo,
    qtd_silo::numeric AS qtd_silo,
    (qtd_silo::numeric - lag(qtd_silo::numeric) OVER (
      PARTITION BY turno_id, silo_id
      ORDER BY created_at
    )) AS delta_kg
  FROM samples
),
kg_by_shift AS (
  SELECT
    turno_id, turno,
    SUM(CASE WHEN estado_silo = 1 AND delta_kg > 0 THEN delta_kg ELSE 0 END) AS kg_produzidos
  FROM deltas
  GROUP BY turno_id, turno
),
plan_by_shift AS (
  SELECT
    turno_id,
    EXTRACT(EPOCH FROM (end_eff - start_ts)) / 3600.0 AS horas_planeadas
  FROM shifts
),
oee_by_shift AS (
  SELECT
    p.turno_id,
    COALESCE(k.kg_produzidos, 0) AS kg_produzidos,
    COALESCE(p.horas_planeadas, 0) AS horas_planeadas,
    CASE
      WHEN COALESCE(p.horas_planeadas, 0) > 0
        THEN LEAST(
          100.0,
          100.0 * COALESCE(k.kg_produzidos, 0)
          / ((SELECT cadencia_kg_h FROM params) * p.horas_planeadas)
        )
      ELSE 0
    END AS oee_pct
  FROM plan_by_shift p
  LEFT JOIN kg_by_shift k USING (turno_id)
),
pivot AS (
  SELECT
    COALESCE(MAX(oee_pct) FILTER (WHERE turno_id=1), 0) AS t1_oee,
    COALESCE(MAX(oee_pct) FILTER (WHERE turno_id=2), 0) AS t2_oee,
    COALESCE(MAX(oee_pct) FILTER (WHERE turno_id=3), 0) AS t3_oee,
    CASE
      WHEN SUM(horas_planeadas) > 0
        THEN 100.0 * SUM(kg_produzidos) / ((SELECT cadencia_kg_h FROM params) * SUM(horas_planeadas))
      ELSE 0
    END AS total_oee
  FROM oee_by_shift
)
SELECT
  round(t1_oee, 1) AS "T1(08-16)",
  round(t2_oee, 1) AS "T2(16-24)",
  round(t3_oee, 1) AS "T3(00-08)",
  round(total_oee, 1) AS "TOTAL"
FROM pivot;
"""
QUERY_TRIT_TOTAL_SILOS_8H = """
WITH ref AS (
    SELECT
        (
            date_trunc('day', now() AT TIME ZONE 'Europe/Lisbon')
            + interval '8 hours'
        ) AT TIME ZONE 'Europe/Lisbon' AS ref_ts
)
SELECT SUM(qtd_silo) AS "TOTAL"
FROM (
    SELECT DISTINCT ON (silo_id)
        silo_id,
        qtd_silo
    FROM trituracao.silos1a5, ref
    WHERE silo_id BETWEEN 1 AND 5
      AND created_at <= ref.ref_ts
    ORDER BY silo_id, created_at DESC
) t;
"""
QUERY_DESINF_TRIT_KGS_SILOS_DIA_ANTERIOR = """
WITH prod AS (
  WITH
  now_local AS (
    SELECT (now() AT TIME ZONE 'Europe/Lisbon')::timestamp AS now_local
  ),
  base AS (
    SELECT
      date_trunc('day', now_local)::timestamp AS today_local,
      CASE
        WHEN EXTRACT(ISODOW FROM now_local) = 1
          THEN (date_trunc('day', now_local) - interval '3 days')::timestamp   -- 2ª feira => 6ª feira
        ELSE (date_trunc('day', now_local) - interval '1 day')::timestamp     -- resto => ontem
      END AS day1_local
    FROM now_local
  ),
  shift_def AS (
    SELECT * FROM (VALUES
      (1, '08-16', interval '8 hours', interval '16 hours'),
      (2, '16-24', interval '16 hours', interval '24 hours'),
      (3, '00-08', interval '24 hours',  interval '32 hours')
    ) v(turno_id, turno, start_off, end_off)
  ),
  days AS (
    SELECT * FROM (
      VALUES
        ('Dia',   (SELECT today_local FROM base)),
        ('Dia-1', (SELECT day1_local  FROM base))
    ) v(dia_ref, day_local)
  ),
  shifts AS (
    SELECT
      d.dia_ref,
      sd.turno_id,
      sd.turno,
      (d.day_local + sd.start_off) AT TIME ZONE 'Europe/Lisbon' AS start_ts,
      (d.day_local + sd.end_off)   AT TIME ZONE 'Europe/Lisbon' AS end_ts
    FROM days d
    CROSS JOIN shift_def sd
  ),
  in_shift AS (
    SELECT
      s.dia_ref, s.turno_id, s.turno,
      t.silo_id, t.estado_silo, t.qtd_silo, t.created_at
    FROM shifts s
    JOIN desinfecao.silos6a10 t
      ON t.created_at >= s.start_ts
     AND t.created_at <  s.end_ts
  ),
  baseline AS (
    SELECT
      s.dia_ref, s.turno_id, s.turno,
      t.silo_id, t.estado_silo, t.qtd_silo, t.created_at
    FROM shifts s
    JOIN LATERAL (
      SELECT DISTINCT ON (x.silo_id)
        x.*
      FROM desinfecao.silos6a10 x
      WHERE x.created_at < s.start_ts
        AND x.created_at >= s.start_ts - interval '12 hours'
      ORDER BY x.silo_id, x.created_at DESC
    ) t ON true
  ),
  samples AS (
    SELECT * FROM baseline
    UNION ALL
    SELECT * FROM in_shift
  ),
  deltas AS (
    SELECT
      dia_ref, turno_id, turno, silo_id, created_at, estado_silo,
      qtd_silo::numeric AS qtd_silo,
      (qtd_silo::numeric - lag(qtd_silo::numeric) OVER (
        PARTITION BY dia_ref, turno_id, silo_id
        ORDER BY created_at
      )) AS delta_kg
    FROM samples
  )
  SELECT
    dia_ref, turno_id, turno,
    sum(
      CASE
        WHEN estado_silo = 1 AND delta_kg > 0 THEN delta_kg
        ELSE 0
      END
    ) AS kg_produzidos
  FROM deltas
  GROUP BY dia_ref, turno_id, turno
),
pivot AS (
  SELECT
    coalesce(max(kg_produzidos) FILTER (WHERE turno_id=1 AND dia_ref='Dia-1'), 0) AS "T1(08-16)",
    coalesce(max(kg_produzidos) FILTER (WHERE turno_id=2 AND dia_ref='Dia-1'), 0) AS "T2(16-24)",
    coalesce(max(kg_produzidos) FILTER (WHERE turno_id=3 AND dia_ref='Dia-1'), 0) AS "T3(00-08)"
  FROM prod
)
SELECT
  "T1(08-16)",
  "T2(16-24)",
  "T3(00-08)",
  ("T1(08-16)" + "T2(16-24)" + "T3(00-08)") AS "TOTAL"
FROM pivot;
"""
QUERY_DESINF_TRIT_TOTAL_SILOS_8H = """
WITH ref AS (
    SELECT
        (
            date_trunc('day', now() AT TIME ZONE 'Europe/Lisbon')
            + interval '8 hours'
        ) AT TIME ZONE 'Europe/Lisbon' AS ref_ts
)
SELECT SUM(qtd_silo) AS "TOTAL"
FROM (
    SELECT DISTINCT ON (silo_id)
        silo_id,
        qtd_silo
    FROM desinfecao.silos6a10, ref
    WHERE silo_id BETWEEN 6 AND 10
      AND created_at <= ref.ref_ts
    ORDER BY silo_id, created_at DESC
) t;
"""
QUERY_CALIB_GRANULADO_DIA_ANTERIOR = """
WITH
now_ctx AS (
  SELECT
    now() AS now_ts,
    (now() AT TIME ZONE 'Europe/Lisbon')::timestamp AS now_local
),
base AS (
  SELECT
    CASE
      WHEN EXTRACT(ISODOW FROM now_local) = 1
        THEN (date_trunc('day', now_local) - interval '3 day')::timestamp
      ELSE (date_trunc('day', now_local) - interval '1 day')::timestamp
    END AS day_ref_local
  FROM now_ctx
),
shift_def AS (
  SELECT * FROM (VALUES
    (1, 'T1 (06-14)', interval '6 hours',  interval '14 hours'),
    (2, 'T2 (14-22)', interval '14 hours', interval '22 hours'),
    (3, 'T3 (22-06)', interval '22 hours', interval '30 hours')
  ) v(turno_id, turno, start_off, end_off)
),
shifts AS (
  SELECT
    sd.turno_id,
    sd.turno,
    ((b.day_ref_local + sd.start_off) AT TIME ZONE 'Europe/Lisbon') AS start_ts,
    ((b.day_ref_local + sd.end_off)   AT TIME ZONE 'Europe/Lisbon') AS end_ts
  FROM base b
  CROSS JOIN shift_def sd
),
products AS (
  SELECT * FROM (VALUES
    ('05A1', '05_1'),
    ('1A2',  '1_2'),
    ('2A3',  '2_3'),
    ('3A7',  '3_7')
  ) p(produto_label, col_name)
),
calc AS (
  SELECT
    p.produto_label AS produto,
    p.col_name,
    s.turno_id,
    s.turno,
    GREATEST(
      COALESCE((
        SELECT
          CASE p.col_name
            WHEN '05_1' THEN g."05_1"
            WHEN '1_2'  THEN g."1_2"
            WHEN '2_3'  THEN g."2_3"
            WHEN '3_7'  THEN g."3_7"
          END::numeric
        FROM calibracao.granulados g
        WHERE g.created_at <= s.end_ts
        ORDER BY g.created_at DESC
        LIMIT 1
      ), 0)
      -
      COALESCE((
        SELECT
          CASE p.col_name
            WHEN '05_1' THEN g."05_1"
            WHEN '1_2'  THEN g."1_2"
            WHEN '2_3'  THEN g."2_3"
            WHEN '3_7'  THEN g."3_7"
          END::numeric
        FROM calibracao.granulados g
        WHERE g.created_at <= s.start_ts
        ORDER BY g.created_at DESC
        LIMIT 1
      ), 0),
      0
    ) AS kg_turno
  FROM shifts s
  CROSS JOIN products p
),
pivot_prod AS (
  SELECT
    produto,
    COALESCE(SUM(kg_turno) FILTER (WHERE turno_id = 1), 0) AS t1,
    COALESCE(SUM(kg_turno) FILTER (WHERE turno_id = 2), 0) AS t2,
    COALESCE(SUM(kg_turno) FILTER (WHERE turno_id = 3), 0) AS t3
  FROM calc
  GROUP BY produto
),
prod_with_total AS (
  SELECT
    produto,
    t1,
    t2,
    t3,
    (t1 + t2 + t3) AS total_kg
  FROM pivot_prod
),
total_row AS (
  SELECT
    'Total' AS produto,
    SUM(t1) AS t1,
    SUM(t2) AS t2,
    SUM(t3) AS t3,
    SUM(total_kg) AS total_kg
  FROM prod_with_total
),
all_rows AS (
  SELECT * FROM prod_with_total
  UNION ALL
  SELECT * FROM total_row
),
grand_total AS (
  SELECT total_kg
  FROM total_row
)
SELECT
  produto,
  ROUND(t1, 0) AS "T1 (06-14)",
  ROUND(t2, 0) AS "T2 (14-22)",
  ROUND(t3, 0) AS "T3 (22-06)",
  ROUND(total_kg, 0) AS "Total (Kg)",
  CASE
    WHEN produto = 'Total' THEN NULL
    ELSE COALESCE(
      ROUND(100.0 * total_kg / NULLIF((SELECT total_kg FROM grand_total), 0)),
      0
    )
  END AS "%"
FROM all_rows
ORDER BY
  CASE produto
    WHEN '05A1' THEN 1
    WHEN '1A2' THEN 2
    WHEN '2A3' THEN 3
    WHEN '3A7' THEN 4
    WHEN 'Total' THEN 5
    ELSE 99
  END;
"""
