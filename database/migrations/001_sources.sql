-- 001_sources.sql
-- Research sources registry

CREATE TABLE IF NOT EXISTS sources (
    id                      TEXT PRIMARY KEY,
    name                    TEXT NOT NULL,
    short_name              TEXT NOT NULL,
    active                  BOOLEAN NOT NULL DEFAULT TRUE,
    tier                    INTEGER NOT NULL DEFAULT 2 CHECK (tier IN (1, 2, 3)),
    specialty               TEXT[] NOT NULL DEFAULT '{}',
    -- Dimension weights (must sum to ~1.0 per source)
    w_macro_regime          NUMERIC(5,4) NOT NULL DEFAULT 0.25,
    w_micro_levels          NUMERIC(5,4) NOT NULL DEFAULT 0.25,
    w_options_flow          NUMERIC(5,4) NOT NULL DEFAULT 0.25,
    w_timing                NUMERIC(5,4) NOT NULL DEFAULT 0.25,
    -- Analytical layer weights
    w_layer_global_liquidity   NUMERIC(5,4) NOT NULL DEFAULT 0.00,
    w_layer_macro_regime       NUMERIC(5,4) NOT NULL DEFAULT 0.00,
    w_layer_market_structure   NUMERIC(5,4) NOT NULL DEFAULT 0.00,
    w_layer_cycle_sentiment    NUMERIC(5,4) NOT NULL DEFAULT 0.00,
    w_layer_options_flow       NUMERIC(5,4) NOT NULL DEFAULT 0.00,
    w_layer_macro_to_micro     NUMERIC(5,4) NOT NULL DEFAULT 0.00,
    -- Recency decay
    recency_half_life_days  INTEGER NOT NULL DEFAULT 7,
    notes                   TEXT,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Seed data from sources.yaml
INSERT INTO sources (
    id, name, short_name, tier, specialty,
    w_macro_regime, w_micro_levels, w_options_flow, w_timing,
    w_layer_global_liquidity, w_layer_macro_regime, w_layer_market_structure,
    w_layer_cycle_sentiment, w_layer_options_flow, w_layer_macro_to_micro,
    recency_half_life_days, notes
) VALUES
('hedgeye', 'Hedgeye Risk Management', 'Hedgeye', 1,
 ARRAY['macro_regime','risk_range','quad_framework'],
 0.35, 0.30, 0.00, 0.35, 0.10, 0.35, 0.05, 0.00, 0.00, 0.00, 7,
 'Quad framework (GIP model). Very process-driven. Weight timing heavily.'),

('42macro', '42 Macro', '42 Macro', 1,
 ARRAY['quantitative_macro','regime_scoring','cross_asset'],
 0.45, 0.25, 0.00, 0.30, 0.15, 0.40, 0.05, 0.00, 0.00, 0.00, 7,
 'Quantitative regime scoring. Strong cross-asset macro framework.'),

('fftt', 'FFTT / Luke Gromen', 'Gromen', 1,
 ARRAY['global_liquidity','dollar_cycle','fiscal_dominance'],
 0.25, 0.00, 0.00, 0.75, 0.55, 0.20, 0.15, 0.00, 0.00, 0.10, 14,
 'Long-cycle global liquidity and fiscal dominance thesis. Lower timing precision.'),

('crossborder', 'CrossBorder Capital / Michael Howell', 'Howell', 1,
 ARRAY['global_liquidity','collateral_cycles','capital_flows'],
 0.20, 0.00, 0.00, 0.80, 0.60, 0.15, 0.10, 0.00, 0.00, 0.15, 14,
 'Global liquidity cycle model. Proprietary GLI index. Complements Gromen.'),

('mike_green', 'Mike Green / Simplify', 'M. Green', 1,
 ARRAY['passive_flows','market_structure','vol_dynamics'],
 0.10, 0.20, 0.30, 0.40, 0.10, 0.10, 0.30, 0.00, 0.25, 0.25, 10,
 'Passive flow dynamics and structural market plumbing. Options vol overlay.'),

('milton_berg', 'Milton Berg', 'M. Berg', 1,
 ARRAY['cycle_analysis','sentiment_extremes','breadth'],
 0.15, 0.25, 0.15, 0.45, 0.05, 0.15, 0.25, 0.25, 0.10, 0.20, 5,
 'Cycle turning points, breadth divergences, sentiment extremes. High timing value.'),

('investech', 'InvesTech Research', 'InvesTech', 2,
 ARRAY['technical_fundamental','monetary_policy','historical_analogs'],
 0.30, 0.30, 0.00, 0.40, 0.15, 0.25, 0.15, 0.00, 0.00, 0.45, 21,
 'Monthly newsletter cadence. Historical analog analysis. Monetary policy focus.'),

('spotgamma', 'SpotGamma', 'SpotGamma', 1,
 ARRAY['options_flow','gamma_exposure','dealer_positioning'],
 0.00, 0.15, 0.70, 0.15, 0.00, 0.00, 0.05, 0.00, 0.65, 0.30, 2,
 'GEX, dealer gamma, key option levels. Very short decay — stale quickly.'),

('laduc', 'Samantha LaDuc', 'LaDuc', 2,
 ARRAY['market_structure','tape_reading','intermarket'],
 0.10, 0.35, 0.20, 0.35, 0.05, 0.10, 0.15, 0.25, 0.15, 0.30, 5,
 'Tape reading, intermarket analysis, sector rotation signals.'),

('dillian', 'Jarred Dillian', 'Dillian', 2,
 ARRAY['sentiment','contrarian','behavioral'],
 0.10, 0.15, 0.15, 0.60, 0.05, 0.10, 0.10, 0.40, 0.10, 0.25, 7,
 'Contrarian sentiment signals, behavioral finance overlay.')

ON CONFLICT (id) DO UPDATE SET
    name                    = EXCLUDED.name,
    short_name              = EXCLUDED.short_name,
    tier                    = EXCLUDED.tier,
    specialty               = EXCLUDED.specialty,
    w_macro_regime          = EXCLUDED.w_macro_regime,
    w_micro_levels          = EXCLUDED.w_micro_levels,
    w_options_flow          = EXCLUDED.w_options_flow,
    w_timing                = EXCLUDED.w_timing,
    w_layer_global_liquidity   = EXCLUDED.w_layer_global_liquidity,
    w_layer_macro_regime       = EXCLUDED.w_layer_macro_regime,
    w_layer_market_structure   = EXCLUDED.w_layer_market_structure,
    w_layer_cycle_sentiment    = EXCLUDED.w_layer_cycle_sentiment,
    w_layer_options_flow       = EXCLUDED.w_layer_options_flow,
    w_layer_macro_to_micro     = EXCLUDED.w_layer_macro_to_micro,
    recency_half_life_days  = EXCLUDED.recency_half_life_days,
    notes                   = EXCLUDED.notes,
    updated_at              = NOW();

CREATE INDEX IF NOT EXISTS idx_sources_active ON sources (active);
CREATE INDEX IF NOT EXISTS idx_sources_tier ON sources (tier);
