-- 015_universe.sql — Investment universe registry + watchlist.
-- The universe is everything we might own or monitor, not just holdings.
-- Categories: holding_lineup | sector | factor | international | bond | commodity | macro_monitor
CREATE TABLE IF NOT EXISTS universe (
    ticker       text PRIMARY KEY,
    name         text,
    category     text NOT NULL,
    is_watchlist boolean DEFAULT false,
    active       boolean DEFAULT true,
    notes        text,
    added_at     timestamptz DEFAULT now()
);
ALTER TABLE universe ENABLE ROW LEVEL SECURITY;

CREATE OR REPLACE VIEW v_universe AS
SELECT u.ticker, u.name, u.category, u.is_watchlist, u.notes,
       t.price, t.trend, t.timing, t.rsi, t.rsi_regime, t.vol20,
       t.off_high_52w, t.off_low_52w, t.rel_strength_63d,
       t.range_1m_lo, t.range_1m_hi, t.pos_in_range_1m,
       t.avwap_ytd, t.ma200, t.score_date, t.computed_at
FROM universe u
LEFT JOIN v_technicals t ON t.ticker = u.ticker
WHERE u.active AND fn_is_approved();

GRANT SELECT ON v_universe TO authenticated;

INSERT INTO universe (ticker, name, category, is_watchlist) VALUES
 ('SPY','S&P 500','macro_monitor',false),('QQQ','Nasdaq 100','macro_monitor',false),
 ('IWM','Russell 2000','macro_monitor',false),('^VIX','VIX','macro_monitor',false),
 ('^TNX','US 10Y Yield','macro_monitor',false),('UUP','US Dollar','macro_monitor',false),
 ('GLD','Gold','macro_monitor',false),('USO','Oil (WTI)','macro_monitor',false),
 ('HYG','High Yield Credit','macro_monitor',false),('IEF','7-10Y Treasuries','macro_monitor',false),
 ('TLT','20Y+ Treasuries','macro_monitor',false),('EEM','Emerging Markets','macro_monitor',false),
 ('FXI','China Large Cap','macro_monitor',false),
 ('XLK','Tech Sector','sector',false),('XLE','Energy Sector','sector',false),
 ('XLF','Financials','sector',false),('XLV','Health Care','sector',false),
 ('XLI','Industrials','sector',false),('XLB','Materials','sector',false),
 ('XLU','Utilities','sector',false),('XLRE','Real Estate','sector',false),
 ('XLC','Comm Services','sector',false),('XLP','Staples','sector',false),
 ('XLY','Discretionary','sector',false),
 ('SMH','Semiconductors','sector',true),
 ('MTUM','Momentum Factor','factor',false),('QUAL','Quality Factor','factor',false),
 ('VLUE','Value Factor','factor',false),('USMV','Min Volatility','factor',false),
 ('SPHB','High Beta','factor',false),('SPLV','Low Volatility','factor',false),
 ('EFA','EAFE Developed','international',false),('EWJ','Japan','international',true),
 ('ILF','Latin America','international',true),
 ('AGG','US Aggregate Bond','bond',false),('LQD','IG Credit','bond',false),
 ('SHY','1-3Y Treasuries','bond',false),('EMB','EM Bonds','bond',false),
 ('SLV','Silver','commodity',false),('DBC','Broad Commodities','commodity',false),
 ('COPX','Copper Miners','commodity',true)
ON CONFLICT (ticker) DO NOTHING;

-- fund lineup: every ticker ever held enters the universe as holding_lineup
INSERT INTO universe (ticker, name, category)
SELECT DISTINCT h.ticker, MAX(h.fund_name), 'holding_lineup'
FROM model_holdings_snapshot h WHERE h.ticker != 'CASH'
GROUP BY h.ticker
ON CONFLICT (ticker) DO NOTHING;
