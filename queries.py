##### queries.py #####
# quarda queries SQL
# cada query é uma string multi-linha, e deve ser escrita de forma a ser legível e fácil de manter
##################

QUERY_TEMPO_PRODUCAO_MD = """
WITH
base AS (
  SELECT
    (%(report_date)s::date)::timestamp AS day1_local,
    now() AS now_ts
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
    || ' (' || (CASE WHEN t1_func > 0 THEN round(100.0 * t1_carga / t1_func)::int ELSE 0 END) || '%%)'
    AS "T1(08-16)",
  to_char(make_interval(mins => t2_func::int), 'HH24"h"MI')
    || ' (' || (CASE WHEN t2_func > 0 THEN round(100.0 * t2_carga / t2_func)::int ELSE 0 END) || '%%)'
    AS "T2(16-24)",
  to_char(make_interval(mins => t3_func::int), 'HH24"h"MI')
    || ' (' || (CASE WHEN t3_func > 0 THEN round(100.0 * t3_carga / t3_func)::int ELSE 0 END) || '%%)'
    AS "T3(00-08)",
  to_char(make_interval(mins => (t1_func + t2_func + t3_func)::int), 'HH24"h"MI')
    || ' (' || (
        CASE
          WHEN (t1_func + t2_func + t3_func) > 0
            THEN round(100.0 * (t1_carga + t2_carga + t3_carga) / (t1_func + t2_func + t3_func))::int
          ELSE 0
        END
      ) || '%%)'
    AS "TOTAL"
FROM pivot;
"""
QUERY_HORAS_MOINHOS = """
WITH
base AS (
  SELECT
    (%(report_date)s::date)::timestamp AS day1_local
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
  SELECT 'Dia-1' AS dia_ref, day1_local AS day_local
  FROM base
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
  base AS (
  SELECT
    (%(report_date)s::date)::timestamp AS day1_local,
    now() AS now_ts
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
base AS (
  SELECT
    (%(report_date)s::date)::timestamp AS day1_local,
    now() AS now_ts
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
  base AS (
  SELECT
    (%(report_date)s::date)::timestamp AS day1_local
  ),
  shift_def AS (
    SELECT * FROM (VALUES
      (1, '08-16', interval '8 hours', interval '16 hours'),
      (2, '16-24', interval '16 hours', interval '24 hours'),
      (3, '00-08', interval '24 hours',  interval '32 hours')
    ) v(turno_id, turno, start_off, end_off)
  ),
  days AS (
  SELECT 'Dia-1' AS dia_ref, day1_local AS day_local
  FROM base
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
base AS (
  SELECT
    (%(report_date)s::date)::timestamp AS day_ref_local
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
  END AS "Percentagem"
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
QUERY_DESINF_VINC_DESINFECOES_DIA_ANTERIOR = """
WITH
base AS (
  SELECT
    (%(report_date)s::date)::timestamp AS day_ref_local
),
shift_def AS (
  SELECT * FROM (VALUES
    (1, 'T1 (08-16)', interval '8 hours',  interval '16 hours'),
    (2, 'T2 (16-24)', interval '16 hours', interval '24 hours'),
    (3, 'T3 (00-08)', interval '24 hours', interval '32 hours')
  ) v(turno_id, turno, start_off, end_off)
),
shifts AS (
  SELECT
    sd.turno_id,
    sd.turno,
    b.day_ref_local + sd.start_off AS start_local,
    b.day_ref_local + sd.end_off   AS end_local
  FROM base b
  CROSS JOIN shift_def sd
),
raw_data AS (
  SELECT
    'VAPEX 1' AS vapex,
    record_id,
    created_at,
    (created_at AT TIME ZONE 'Europe/Lisbon')::timestamp AS created_at_local,
    operation_id,
    tempo_teorico
  FROM desinfecao.machine_1

  UNION ALL

  SELECT
    'VAPEX 2' AS vapex,
    record_id,
    created_at,
    (created_at AT TIME ZONE 'Europe/Lisbon')::timestamp AS created_at_local,
    operation_id,
    tempo_teorico
  FROM desinfecao.machine_2

  UNION ALL

  SELECT
    'VAPEX 3' AS vapex,
    record_id,
    created_at,
    (created_at AT TIME ZONE 'Europe/Lisbon')::timestamp AS created_at_local,
    operation_id,
    tempo_teorico
  FROM desinfecao.machine_3

  UNION ALL

  SELECT
    'VAPEX 4' AS vapex,
    record_id,
    created_at,
    (created_at AT TIME ZONE 'Europe/Lisbon')::timestamp AS created_at_local,
    operation_id,
    tempo_teorico
  FROM desinfecao.machine_4
),
base_data AS (
  SELECT
    r.*,
    LAG(operation_id) OVER (
      PARTITION BY vapex
      ORDER BY created_at, record_id
    ) AS prev_operation_id
  FROM raw_data r
  CROSS JOIN base b
  WHERE r.created_at_local >= b.day_ref_local - interval '12 hours'
    AND r.created_at_local <  b.day_ref_local + interval '36 hours'
    AND r.operation_id IS NOT NULL
),
marcados AS (
  SELECT
    *,
    CASE
      WHEN prev_operation_id IS DISTINCT FROM operation_id THEN 1
      ELSE 0
    END AS novo_ciclo
  FROM base_data
),
grupos AS (
  SELECT
    *,
    SUM(novo_ciclo) OVER (
      PARTITION BY vapex
      ORDER BY created_at, record_id
    ) AS ciclo_grp
  FROM marcados
),
ciclos AS (
  SELECT
    vapex,
    ciclo_grp,
    MIN(operation_id) AS operation_id,
    MIN(created_at_local) AS inicio_ciclo_local,
    MAX(tempo_teorico) AS tempo_teorico,
    MIN(created_at_local) + (MAX(tempo_teorico) * interval '1 minute') AS fim_programado_local
  FROM grupos
  GROUP BY vapex, ciclo_grp
),
ciclos_validos AS (
  SELECT *
  FROM ciclos
  WHERE tempo_teorico > 45
),
counts_by_shift AS (
  SELECT
    cv.vapex,
    s.turno_id,
    COUNT(*) AS qtd
  FROM ciclos_validos cv
  JOIN shifts s
    ON cv.fim_programado_local >= s.start_local
   AND cv.fim_programado_local <  s.end_local
  GROUP BY cv.vapex, s.turno_id
),
pivot_vapex AS (
  SELECT
    vapex,
    COALESCE(SUM(qtd) FILTER (WHERE turno_id = 1), 0) AS t1,
    COALESCE(SUM(qtd) FILTER (WHERE turno_id = 2), 0) AS t2,
    COALESCE(SUM(qtd) FILTER (WHERE turno_id = 3), 0) AS t3
  FROM counts_by_shift
  GROUP BY vapex
),
all_vapex AS (
  SELECT * FROM (VALUES
    ('VAPEX 1'),
    ('VAPEX 2'),
    ('VAPEX 3'),
    ('VAPEX 4')
  ) v(vapex)
),
rows_vapex AS (
  SELECT
    a.vapex,
    COALESCE(p.t1, 0) AS t1,
    COALESCE(p.t2, 0) AS t2,
    COALESCE(p.t3, 0) AS t3
  FROM all_vapex a
  LEFT JOIN pivot_vapex p USING (vapex)
),
total_row AS (
  SELECT
    'TOTAL' AS vapex,
    SUM(t1) AS t1,
    SUM(t2) AS t2,
    SUM(t3) AS t3
  FROM rows_vapex
)
SELECT
  vapex AS "VAPEX",
  t1 AS "T1 (08-16)",
  t2 AS "T2 (16-24)",
  t3 AS "T3 (00-08)",
  (t1 + t2 + t3) AS "Total"
FROM (
  SELECT * FROM rows_vapex
  UNION ALL
  SELECT * FROM total_row
) x
ORDER BY
  CASE vapex
    WHEN 'VAPEX 1' THEN 1
    WHEN 'VAPEX 2' THEN 2
    WHEN 'VAPEX 3' THEN 3
    WHEN 'VAPEX 4' THEN 4
    WHEN 'TOTAL' THEN 5
    ELSE 99
  END;
"""
QUERY_CALIB_OEE_TABELA_DIA_ANTERIOR = """
WITH
params_oee AS (
  SELECT 1250::numeric AS cadencia_kg_h
),
base AS (
  SELECT
    (%(report_date)s::date)::timestamp AS day_ref_local
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
    ((b.day_ref_local + sd.end_off)   AT TIME ZONE 'Europe/Lisbon') AS end_ts,
    28800.0 AS planned_s
  FROM base b
  CROSS JOIN shift_def sd
),
bounds AS (
  SELECT
    MIN(start_ts) AS start_ts,
    MAX(end_ts) AS end_ts
  FROM shifts
),
produced_by_shift AS (
  SELECT
    s.turno_id,
    s.turno,
    GREATEST(
      COALESCE((
        SELECT g.total::numeric
        FROM calibracao.granulados g
        WHERE g.created_at <= s.end_ts
        ORDER BY g.created_at DESC
        LIMIT 1
      ), 0)
      -
      COALESCE((
        SELECT g.total::numeric
        FROM calibracao.granulados g
        WHERE g.created_at <= s.start_ts
        ORDER BY g.created_at DESC
        LIMIT 1
      ), 0),
      0
    ) AS kg_produzidos
  FROM shifts s
),
remoagem_filtrada AS (
  SELECT
    r.created_at,
    CASE
      WHEN COALESCE(r.status_rotex_m23, 0) = 1
        OR COALESCE(r.status_rotex_m45, 0) = 1
      THEN 1 ELSE 0
    END AS algum_rotex_on
  FROM mov_granulados.remoagem r
  CROSS JOIN bounds b
  WHERE r.created_at >= b.start_ts
    AND r.created_at <  b.end_ts
),
silos_base AS (
  SELECT DISTINCT ON (s.silo_id)
    s.silo_id,
    s.estado_silo,
    b.start_ts AS created_at
  FROM desinfecao.silos6a10 s
  CROSS JOIN bounds b
  WHERE s.silo_id BETWEEN 6 AND 10
    AND s.created_at < b.start_ts
  ORDER BY s.silo_id, s.created_at DESC
),
silos_intervalo AS (
  SELECT
    s.silo_id,
    s.estado_silo,
    s.created_at
  FROM desinfecao.silos6a10 s
  CROSS JOIN bounds b
  WHERE s.silo_id BETWEEN 6 AND 10
    AND s.created_at >= b.start_ts
    AND s.created_at <  b.end_ts
),
silos_samples AS (
  SELECT * FROM silos_base
  UNION ALL
  SELECT * FROM silos_intervalo
),
silos_segmentos AS (
  SELECT
    silo_id,
    estado_silo,
    created_at AS seg_start,
    LEAD(created_at, 1, (SELECT end_ts FROM bounds)) OVER (
      PARTITION BY silo_id
      ORDER BY created_at
    ) AS seg_end
  FROM silos_samples
),
estado_por_ts AS (
  SELECT
    r.created_at,
    r.algum_rotex_on,
    COALESCE(
      MAX(CASE WHEN ss.estado_silo = 2 THEN 1 ELSE 0 END),
      0
    ) AS algum_silo_vazar
  FROM remoagem_filtrada r
  LEFT JOIN silos_segmentos ss
    ON r.created_at >= ss.seg_start
   AND r.created_at <  ss.seg_end
  GROUP BY r.created_at, r.algum_rotex_on
),
status_in_shift AS (
  SELECT
    sh.turno_id,
    sh.turno,
    sh.end_ts,
    e.created_at,
    COALESCE(
      LEAD(e.created_at) OVER (
        PARTITION BY sh.turno_id
        ORDER BY e.created_at
      ),
      sh.end_ts
    ) AS next_ts,
    e.algum_rotex_on,
    e.algum_silo_vazar
  FROM shifts sh
  JOIN estado_por_ts e
    ON e.created_at >= sh.start_ts
   AND e.created_at <  sh.end_ts
),
durations AS (
  SELECT
    turno_id,
    turno,
    EXTRACT(EPOCH FROM (
      GREATEST(
        interval '0 second',
        LEAST(next_ts, end_ts) - created_at
      )
    )) AS dur_s,
    algum_rotex_on,
    algum_silo_vazar
  FROM status_in_shift
),
time_by_shift AS (
  SELECT
    sh.turno_id,
    sh.turno,
    sh.planned_s,
    COALESCE(SUM(
      CASE
        WHEN d.algum_rotex_on = 1 AND d.algum_silo_vazar = 1
        THEN d.dur_s ELSE 0
      END
    ), 0) AS tempo_produtivo_s,
    COALESCE(SUM(
      CASE
        WHEN d.algum_rotex_on = 1 AND d.algum_silo_vazar = 0
        THEN d.dur_s ELSE 0
      END
    ), 0) AS tempo_sem_granulado_s
  FROM shifts sh
  LEFT JOIN durations d
    ON d.turno_id = sh.turno_id
  GROUP BY sh.turno_id, sh.turno, sh.planned_s
),
kpis AS (
  SELECT
    t.turno_id,
    t.turno,
    p.kg_produzidos,
    t.planned_s,
    t.tempo_produtivo_s,
    t.tempo_sem_granulado_s,
    CASE
      WHEN t.planned_s > 0
        THEN 100.0 * GREATEST(t.planned_s - t.tempo_sem_granulado_s, 0) / t.planned_s
      ELSE 0
    END AS disponibilidade_pct,
    CASE
      WHEN t.tempo_produtivo_s > 0
        THEN 100.0 * p.kg_produzidos / ((t.tempo_produtivo_s / 3600.0) * po.cadencia_kg_h)
      ELSE 0
    END AS performance_pct
  FROM time_by_shift t
  JOIN produced_by_shift p
    ON p.turno_id = t.turno_id
  CROSS JOIN params_oee po
),
kpis_day AS (
  SELECT
    CASE
      WHEN SUM(planned_s) > 0
        THEN 100.0 * GREATEST(SUM(planned_s) - SUM(tempo_sem_granulado_s), 0) / SUM(planned_s)
      ELSE 0
    END AS disponibilidade_dia,
    CASE
      WHEN SUM(tempo_produtivo_s) > 0
        THEN 100.0 * SUM(kg_produzidos) / ((SUM(tempo_produtivo_s) / 3600.0) * MAX(po.cadencia_kg_h))
      ELSE 0
    END AS performance_dia,
    SUM(tempo_sem_granulado_s) AS tempo_sem_granulado_dia
  FROM kpis
  CROSS JOIN params_oee po
),
pivot AS (
  SELECT
    COALESCE(MAX(performance_pct) FILTER (WHERE turno_id = 1), 0) AS perf_t1,
    COALESCE(MAX(performance_pct) FILTER (WHERE turno_id = 2), 0) AS perf_t2,
    COALESCE(MAX(performance_pct) FILTER (WHERE turno_id = 3), 0) AS perf_t3,

    COALESCE(MAX(disponibilidade_pct) FILTER (WHERE turno_id = 1), 0) AS disp_t1,
    COALESCE(MAX(disponibilidade_pct) FILTER (WHERE turno_id = 2), 0) AS disp_t2,
    COALESCE(MAX(disponibilidade_pct) FILTER (WHERE turno_id = 3), 0) AS disp_t3,

    COALESCE(MAX((performance_pct * disponibilidade_pct) / 100.0) FILTER (WHERE turno_id = 1), 0) AS oee_t1,
    COALESCE(MAX((performance_pct * disponibilidade_pct) / 100.0) FILTER (WHERE turno_id = 2), 0) AS oee_t2,
    COALESCE(MAX((performance_pct * disponibilidade_pct) / 100.0) FILTER (WHERE turno_id = 3), 0) AS oee_t3,

    COALESCE(MAX(tempo_sem_granulado_s) FILTER (WHERE turno_id = 1), 0) AS tempo_t1,
    COALESCE(MAX(tempo_sem_granulado_s) FILTER (WHERE turno_id = 2), 0) AS tempo_t2,
    COALESCE(MAX(tempo_sem_granulado_s) FILTER (WHERE turno_id = 3), 0) AS tempo_t3
  FROM kpis
)
SELECT
  'Performance' AS "Indicador",
  ROUND(LEAST(100.0, perf_t1), 1) AS "T1 (06-14)",
  ROUND(LEAST(100.0, perf_t2), 1) AS "T2 (14-22)",
  ROUND(LEAST(100.0, perf_t3), 1) AS "T3 (22-06)",
  ROUND(LEAST(100.0, (SELECT performance_dia FROM kpis_day)), 1) AS "Dia"
FROM pivot

UNION ALL

SELECT
  'Disponibilidade' AS "Indicador",
  ROUND(LEAST(100.0, disp_t1), 1) AS "T1 (06-14)",
  ROUND(LEAST(100.0, disp_t2), 1) AS "T2 (14-22)",
  ROUND(LEAST(100.0, disp_t3), 1) AS "T3 (22-06)",
  ROUND(LEAST(100.0, (SELECT disponibilidade_dia FROM kpis_day)), 1) AS "Dia"
FROM pivot

UNION ALL

SELECT
  'OEE' AS "Indicador",
  ROUND(LEAST(100.0, oee_t1), 1) AS "T1 (06-14)",
  ROUND(LEAST(100.0, oee_t2), 1) AS "T2 (14-22)",
  ROUND(LEAST(100.0, oee_t3), 1) AS "T3 (22-06)",
  ROUND(
    LEAST(
      100.0,
      ((SELECT performance_dia FROM kpis_day) * (SELECT disponibilidade_dia FROM kpis_day)) / 100.0
    ),
    1
  ) AS "Dia"
FROM pivot

UNION ALL

SELECT
  'Tempo Trabalho sem granulado' AS "Indicador",
  ROUND(tempo_t1, 0) AS "T1 (06-14)",
  ROUND(tempo_t2, 0) AS "T2 (14-22)",
  ROUND(tempo_t3, 0) AS "T3 (22-06)",
  ROUND((SELECT tempo_sem_granulado_dia FROM kpis_day), 0) AS "Dia"
FROM pivot;
"""