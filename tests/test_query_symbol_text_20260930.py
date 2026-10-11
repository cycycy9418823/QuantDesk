"""T35: symbol literals are identifiers, never floating-point quantities."""
import pytest
from quantdesk.data_store.query import parse_where_clauses,run_safe_query
from quantdesk.data_store.repositories import DailyBarRepository
from quantdesk.data_store.sqlite_store import QuantDeskDB


@pytest.fixture
def database(tmp_path):
    path=tmp_path/'query.db';db=QuantDeskDB(path);db.initialize()
    DailyBarRepository(db).upsert_daily_bars([
        {'symbol':'000001','date':'2026-09-28','close':10.,'volume':100.},
        {'symbol':'600000','date':'2026-09-28','close':20.,'volume':200.},
        {'symbol':'600001','date':'2026-09-29','close':30.,'volume':300.},
    ],'raw')
    yield path,db
    db.close()


@pytest.mark.parametrize('kind',['daily','cross-section'])
@pytest.mark.parametrize('literal,identity',[
    ('000001','000001'),("'000001'",'000001'),
    ('600000','600000'),('"600000"','600000'),
    ("'000001.SZ'",'000001'),("'sh600000'",'600000'),
])
@pytest.mark.parametrize('operator',['=','IN','!='])
def test_symbol_literals_match_keyword_and_aliases(database,kind,literal,identity,operator):
    path,_=database
    where=f'symbol IN ({literal})' if operator=='IN' else f'symbol {operator} {literal}'
    result=run_safe_query(kind,where=where,db_path=str(path),adjustment='raw')
    direct=run_safe_query(kind,symbol=identity,db_path=str(path),adjustment='raw')
    assert result['ok'] and direct['row_count']==1
    if operator=='!=':
        assert {r['symbol'] for r in result['rows']}=={'000001','600000','600001'}-{identity}
    else:assert result['rows']==direct['rows']


@pytest.mark.parametrize('kind',['daily','cross-section'])
def test_multiple_numeric_in_retains_leading_zero(database,kind):
    path,_=database
    result=run_safe_query(kind,where="symbol IN (000001, '600000')",db_path=str(path),adjustment='raw')
    assert [r['symbol'] for r in result['rows']]==['000001','600000']
    assert parse_where_clauses("symbol IN (000001, '600000')",'daily')[0]['values']==['000001','600000']


@pytest.mark.parametrize('where',["symbol = '000001'",'symbol IN (000001)'])
def test_matching_numeric_identity_conflict_cannot_disappear(database,where):
    path,db=database
    original=db.execute_one("SELECT * FROM daily_bars_raw WHERE symbol='000001'")
    original['symbol']='000001.SZ';original['close']=99.
    db.upsert_many('daily_bars_raw',[original],['symbol','date'])
    result=run_safe_query('daily',where=where,db_path=str(path),adjustment='raw')
    assert result['status']=='identity_conflict' and result['ok'] is False
    unrelated=run_safe_query('daily',where="symbol='600000'",db_path=str(path),adjustment='raw')
    assert unrelated['ok'] and unrelated['row_count']==1


def test_numeric_and_date_filters_keep_existing_semantics(database):
    path,_=database
    clauses=parse_where_clauses("close >= 20 AND date = '2026-09-28'",'daily')
    assert clauses[0]['values']==[20.]
    assert clauses[1]['values']==['2026-09-28']
    result=run_safe_query('daily',where="symbol IN (000001,600000) AND close >= 20 AND date = '2026-09-28'",db_path=str(path),adjustment='raw')
    assert result['ok'] and [r['symbol'] for r in result['rows']]==['600000']


@pytest.mark.parametrize('where',["symbol='000001'; DROP TABLE daily_bars_raw",'secret=1',"symbol='000001' -- comment"])
def test_symbol_fix_does_not_weaken_whitelist(database,where):
    path,_=database
    with pytest.raises(ValueError):run_safe_query('daily',where=where,db_path=str(path),adjustment='raw')
