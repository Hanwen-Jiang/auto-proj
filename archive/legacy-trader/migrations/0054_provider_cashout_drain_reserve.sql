ALTER TABLE a6_trade.provider_fund_reserves
    DROP CONSTRAINT IF EXISTS provider_fund_reserve_basis_known;

ALTER TABLE a6_trade.provider_fund_reserves
    ADD CONSTRAINT provider_fund_reserve_basis_known CHECK (
        reserve_basis IN (
            'configured_minimum',
            'immediate_refill',
            'pending_topup',
            'cashout_drain'
        )
    );
