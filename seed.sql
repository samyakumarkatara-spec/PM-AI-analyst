-- Demo warehouse: a fake B2B SaaS product. Runs automatically on first `docker compose up`.
SELECT setseed(0.42);

CREATE TABLE users (
  id            serial PRIMARY KEY,
  email         text NOT NULL,
  plan          text NOT NULL,
  country       text NOT NULL,
  signup_source text NOT NULL,
  created_at    timestamptz NOT NULL
);
INSERT INTO users (email, plan, country, signup_source, created_at)
SELECT 'user' || g || '@example.com',
       (ARRAY['free','free','free','pro','pro','enterprise'])[1 + floor(random()*6)::int],
       (ARRAY['US','US','IN','DE','UK','BR'])[1 + floor(random()*6)::int],
       (ARRAY['organic','paid','referral','partner'])[1 + floor(random()*4)::int],
       now() - random() * 365 * interval '1 day'
FROM generate_series(1, 5000) g;

CREATE TABLE sessions (
  id         serial PRIMARY KEY,
  user_id    int NOT NULL REFERENCES users(id),
  started_at timestamptz NOT NULL,
  duration_s int NOT NULL,
  platform   text NOT NULL
);
INSERT INTO sessions (user_id, started_at, duration_s, platform)
SELECT u.id,
       u.created_at + random() * (now() - u.created_at),
       (30 + random() * 1800)::int,
       (ARRAY['web','web','ios','android'])[1 + floor(random()*4)::int]
FROM users u
CROSS JOIN LATERAL generate_series(1, (1 + floor(random() * 30))::int) g;

CREATE TABLE events (
  id          serial PRIMARY KEY,
  user_id     int NOT NULL REFERENCES users(id),
  session_id  int NOT NULL REFERENCES sessions(id),
  event_name  text NOT NULL,
  occurred_at timestamptz NOT NULL
);
INSERT INTO events (user_id, session_id, event_name, occurred_at)
SELECT s.user_id, s.id,
       (ARRAY['view_dashboard','create_report','invite_teammate','export_csv'])[1 + floor(random()*4)::int],
       s.started_at + random() * interval '20 minutes'
FROM sessions s
CROSS JOIN LATERAL generate_series(1, (1 + floor(random() * 3))::int) g;

CREATE TABLE subscriptions (
  id          serial PRIMARY KEY,
  user_id     int NOT NULL REFERENCES users(id),
  plan        text NOT NULL,
  mrr         numeric(10,2) NOT NULL,
  status      text NOT NULL,
  started_at  timestamptz NOT NULL,
  canceled_at timestamptz
);
INSERT INTO subscriptions (user_id, plan, mrr, status, started_at, canceled_at)
SELECT id, plan,
       CASE plan WHEN 'pro' THEN 49 ELSE 499 END,
       st,
       created_at + random() * interval '30 days',
       CASE WHEN st = 'canceled' THEN created_at + interval '30 days' + random() * interval '120 days' END
FROM (
  SELECT u.*, CASE WHEN random() < 0.7 THEN 'active' ELSE 'canceled' END AS st
  FROM users u WHERE plan <> 'free'
) x;

COMMENT ON TABLE users IS 'One row per registered account owner. plan is the CURRENT plan.';
COMMENT ON TABLE sessions IS 'One row per app visit. Used for engagement metrics.';
COMMENT ON TABLE events IS 'Product usage events inside sessions.';
COMMENT ON TABLE subscriptions IS 'Paid subscriptions only (free users have no row). status: active | canceled.';
COMMENT ON COLUMN subscriptions.mrr IS 'Monthly recurring revenue in USD.';

CREATE ROLE analyst_ro LOGIN PASSWORD 'analyst_ro_pw';
GRANT CONNECT ON DATABASE warehouse TO analyst_ro;
GRANT USAGE ON SCHEMA public TO analyst_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO analyst_ro;
ALTER ROLE analyst_ro SET default_transaction_read_only = on;
ALTER ROLE analyst_ro SET statement_timeout = '15s';

ANALYZE;
