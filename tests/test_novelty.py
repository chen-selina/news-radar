from radar.novelty import is_recap, stale_ids


def test_recap_titles_describe_past_price_moves():
    assert is_recap("台積電收盤大漲3% 站上千元")
    assert is_recap("聯發科盤中亮燈漲停")
    assert not is_recap("台積電10月營收年增三成 創單月新高")
    assert not is_recap("鴻海取得輝達伺服器大單")


def test_stale_needs_similar_title_two_to_ten_days_earlier():
    news = [(1, "鴻海取得輝達伺服器大單 明年出貨倍增", "2026-10-01T09:00:00"),
            (2, "鴻海取得輝達伺服器大單 法人按讚", "2026-10-02T09:00:00"),      # 隔天：同事件，不算舊聞
            (3, "鴻海取得輝達伺服器大單 明年出貨倍增", "2026-10-06T09:00:00"),   # 5 天後：舊聞
            (4, "鴻海取得輝達伺服器大單 明年出貨倍增", "2026-10-20T09:00:00"),   # 距離上一則 14 天：太久，不算
            (5, "台積電法說會登場 外資聚焦毛利率", "2026-10-06T09:00:00"),
            (6, "鴻海取得輝達伺服器大單 明年出貨倍增", None)]
    assert stale_ids(news) == {3}
