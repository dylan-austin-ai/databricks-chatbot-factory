-- Cost reconciliation over system tables (CST-9). Optional: created when the tables are
-- enabled and readable; each statement is tried on its own by jobs/setup_platform.py.

-- Real gateway tokens per bot (request tags sent by the agent, GW-4)
CREATE OR REPLACE VIEW {catalog}.{platform}.v_gateway_usage_by_bot AS
SELECT CAST(event_time AS DATE) AS day, request_tags['bot_id'] AS bot_id, request_tags['channel'] AS channel,
       destination_model, count(*) AS requests, sum(input_tokens) AS input_tokens,
       sum(output_tokens) AS output_tokens
FROM system.ai_gateway.usage WHERE request_tags['bot_id'] IS NOT NULL GROUP BY ALL;

-- Our token-based cost estimate vs gateway-reported tokens, per bot and day
CREATE OR REPLACE VIEW {catalog}.{platform}.v_cost_reconciliation AS
SELECT c.day, c.bot_id, c.answer_cost_usd AS estimated_cost_usd, g.input_tokens AS gateway_input_tokens,
       g.output_tokens AS gateway_output_tokens, g.requests AS gateway_requests
FROM {catalog}.{platform}.cost_daily c
LEFT JOIN (SELECT day, bot_id, sum(requests) AS requests, sum(input_tokens) AS input_tokens,
                  sum(output_tokens) AS output_tokens
           FROM {catalog}.{platform}.v_gateway_usage_by_bot GROUP BY ALL) g USING (day, bot_id);

-- SQL warehouse work attributed to bots through query tags (CST-10)
CREATE OR REPLACE VIEW {catalog}.{platform}.v_warehouse_by_bot AS
SELECT CAST(start_time AS DATE) AS day, query_tags['bot_id'] AS bot_id, query_tags['source'] AS source,
       count(*) AS statements, sum(total_duration_ms) / 1000.0 AS seconds
FROM system.query.history WHERE query_tags['component'] = 'chatbot-factory' GROUP BY ALL;
