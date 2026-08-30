import pandas as pd

from quantdesk.data_quality import (
    DATA_GRADE_LABELS,
    MARKET_SH,
    MARKET_SZ,
    NETWORK_BLOCKED_CODE,
    DataGrade,
    assess_daily_frame_grade,
    assess_data_grade,
    classify_market,
    classify_provider_error,
    grade_distribution,
)


def test_classify_market_sh():
    assert classify_market('600000') == MARKET_SH

def test_classify_market_sz():
    assert classify_market('000001') == MARKET_SZ

def test_classify_market_star():
    assert classify_market('688001') == 'STAR'

def test_classify_market_unknown_invalid():
    assert classify_market('12345') == 'UNKNOWN'
    assert classify_market('abc') == 'UNKNOWN'

def test_data_grade_unavailable_empty():
    result = assess_data_grade(row_count=0)
    assert result.data_grade == DataGrade.UNAVAILABLE.value

def test_data_grade_backtest_ready():
    result = assess_data_grade(row_count=600, min_date='2020-01-01', max_date='2023-12-31')
    assert result.data_grade == DataGrade.BACKTEST_READY.value

def test_classify_provider_error_network_blocked():
    assert classify_provider_error('WinError 10013 permission denied') == NETWORK_BLOCKED_CODE

def test_assess_daily_frame_grade():
    df = pd.DataFrame({'date': pd.date_range('2020-01-01', periods=600, freq='B'), 'close': range(600)})
    result = assess_daily_frame_grade(df)
    assert result.row_count == 600
    assert result.backtest_allowed == True

def test_grade_distribution():
    items = [{'data_grade': 'BACKTEST_READY'}, {'data_grade': 'OBSERVE_ONLY'}, {'data_grade': 'BACKTEST_READY'}]
    dist = grade_distribution(items)
    assert dist['BACKTEST_READY'] == 2
    assert dist['OBSERVE_ONLY'] == 1

def test_data_grade_labels():
    assert DATA_GRADE_LABELS[DataGrade.BACKTEST_READY.value] == '可回测'
    assert DATA_GRADE_LABELS[DataGrade.UNAVAILABLE.value] == '不可用'
