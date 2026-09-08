"""Tests for GCS sentiment mirror (news, daily sentiment, lists)."""

from __future__ import annotations

from waystone3.research.sentiment import merge_csv


def test_merge_csv_news_on_symbol_url():
    local = b"date,ts,symbol,source,title,text,url,massive_sentiment,massive_reasoning\n2024-01-02,2024-01-02T10:00:00-05:00,AAPL,Benzinga,t1,d1,https://x/1,positive,r1\n"
    remote = b"date,ts,symbol,source,title,text,url,massive_sentiment,massive_reasoning\n2024-01-01,2024-01-01T10:00:00-05:00,AAPL,Benzinga,t0,d0,https://x/0,neutral,r0\n"
    out = merge_csv(local, remote, sub="news").decode()
    assert out.count("\n") == 3  # header + 2 rows
    assert "https://x/1" in out and "https://x/0" in out


def test_merge_csv_sentiment_daily_on_date():
    local = b"date,score,count,pos_share,neg_share,shock_z,count_z\n2024-01-02,0.5,3,0.6,0.1,1.2,0.3\n"
    remote = b"date,score,count,pos_share,neg_share,shock_z,count_z\n2024-01-01,0.1,1,0.5,0.0,0.0,0.0\n"
    out = merge_csv(local, remote, sub="sentiment").decode()
    assert "2024-01-01" in out and "2024-01-02" in out
    # local wins on same date
    merged_same = merge_csv(
        b"date,score,count,pos_share,neg_share,shock_z,count_z\n2024-01-02,0.9,5,0.6,0.1,2.0,0.5\n",
        b"date,score,count,pos_share,neg_share,shock_z,count_z\n2024-01-02,0.1,1,0.5,0.0,0.0,0.0\n",
        sub="sentiment",
    ).decode()
    assert ",0.9," in merged_same


def test_merge_csv_lists_sp500():
    local = b"symbol,company\nAAPL,Apple\nMSFT,Microsoft\n"
    remote = b"symbol,company\nAAPL,Apple Inc\nNVDA,Nvidia\n"
    out = merge_csv(local, remote, sub="lists").decode()
    assert "MSFT" in out and "NVDA" in out
    assert "Apple Inc" not in out  # local AAPL row wins
