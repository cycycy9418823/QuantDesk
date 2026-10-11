"""T34 compares selected CSV evidence with actual temporary SQLite values."""
import csv
from pathlib import Path
import pytest
from quantdesk.data_store.migration import migrate_csv_to_sqlite
from quantdesk.data_store.repositories import DailyBarRepository
from quantdesk.data_store.sqlite_store import QuantDeskDB


def row(day='2026-09-28',close=10.,volume=100.):
    return {'symbol':'600000','date':day,'open':10.,'high':11.,'low':9.,'close':close,'volume':volume,'amount':1000.}


def write_csv(root,rows,adjustment='raw',symbol='600000'):
    folder={'raw':'raw','forward':'forward_adjusted','back':'back_adjusted'}[adjustment]
    path=root/'daily'/folder/f'{symbol}.csv';path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('w',newline='',encoding='utf-8') as handle:
        writer=csv.DictWriter(handle,fieldnames=list(row()));writer.writeheader();writer.writerows(rows)
    return path


@pytest.fixture
def store(tmp_path):
    root=tmp_path/'data';root.mkdir();db=QuantDeskDB(tmp_path/'shadow.db');db.initialize()
    yield root,db,DailyBarRepository(db)
    db.close()


def compare(root,db,**kwargs):
    return migrate_csv_to_sqlite({'data_root':str(root),'db_path':str(db.db_path),'adjustment':'raw','action':'compare','compare':True,**kwargs})


def test_no_csv_cannot_borrow_unrelated_table_success(store):
    root,db,repo=store;repo.upsert_daily_bars([row()],'forward')
    result=compare(root,db);summary=result['compare_summary']
    assert summary['hash_match_ratio'] is None
    assert summary['status']=='no_evidence'
    assert summary['common_row_count']==0 and summary['compared_file_count']==0


def test_exact_comparison_has_real_denominator_and_no_migration(store):
    root,db,repo=store;rows=[row(),row('2026-09-29')];write_csv(root,rows);repo.upsert_daily_bars(rows,'raw')
    result=compare(root,db);summary=result['compare_summary']
    assert summary['status']=='matched' and summary['hash_match_ratio']==1.
    assert summary['common_row_count']==summary['hash_match_count']==2
    assert summary['scope']=='selected_csv_files'
    assert result['migrated_file_count']==result['migrated_row_count']==result['partial_daily_migrated_row_count']==0


@pytest.mark.parametrize('field,new_value,counter',[('close',20.,'close_diff_count'),('volume',777.,'volume_diff_count'),('open',5.,'hash_mismatch_count')])
def test_compare_detects_actual_change_despite_stale_stored_hash(store,field,new_value,counter):
    root,db,repo=store;write_csv(root,[row()]);repo.upsert_daily_bars([row()],'raw')
    old=db.execute_one('SELECT row_hash FROM daily_bars_raw')['row_hash']
    db.execute(f'UPDATE daily_bars_raw SET {field}=?',[new_value])
    result=compare(root,db);summary=result['compare_summary']
    actual=db.execute_one('SELECT * FROM daily_bars_raw')
    assert actual[field]==new_value and actual['row_hash']==old
    assert summary[counter]==1 and summary['hash_match_count']==0
    assert summary['hash_match_ratio']==0. and summary['status']=='mismatch'
    assert summary['sample_mismatches']


def test_stored_hash_corruption_is_visible_without_false_content_difference(store):
    root,db,repo=store;write_csv(root,[row()]);repo.upsert_daily_bars([row()],'raw')
    db.execute("UPDATE daily_bars_raw SET row_hash='stale'")
    summary=compare(root,db)['compare_summary']
    assert summary['stored_hash_mismatch_count']==1
    assert summary['hash_match_ratio']==1. and summary['hash_mismatch_count']==0
    assert summary['status']=='mismatch'


def test_date_missing_and_extra_do_not_cancel(store):
    root,db,repo=store;write_csv(root,[row('2026-09-27'),row('2026-09-28')]);repo.upsert_daily_bars([row('2026-09-28'),row('2026-09-29')],'raw')
    summary=compare(root,db)['compare_summary']
    assert summary['row_count_diff']==0 and summary['date_set_diff']==2
    assert summary['missing_in_db_count']==summary['extra_in_db_count']==1
    assert summary['common_row_count']==1 and summary['status']=='mismatch'


def test_zero_common_rows_is_not_a_measured_ratio(store):
    root,db,repo=store;write_csv(root,[row()]);repo.upsert_daily_bars([row('2026-09-29')],'raw')
    summary=compare(root,db)['compare_summary']
    assert summary['hash_match_ratio'] is None and summary['common_row_count']==0
    assert summary['status']=='mismatch' and summary['date_set_diff']==2


def test_adjustment_and_selected_sample_scopes_are_separate(store):
    root,db,repo=store
    write_csv(root,[row()],'raw');write_csv(root,[row(close=15.)],'forward')
    repo.upsert_daily_bars([row()],'raw');repo.upsert_daily_bars([row(close=16.)],'forward')
    summary=compare(root,db,adjustment='all')['compare_summary']
    assert summary['common_row_count']==2 and summary['hash_match_count']==1
    assert summary['hash_match_ratio']==.5 and summary['close_diff_count']==1
    assert summary['compared_file_count']==2


def test_compare_only_does_not_upsert_partial_or_daily(store):
    root,db,repo=store;write_csv(root,[row()]);repo.upsert_daily_bars([row(close=17.)],'raw')
    partial=root/'intraday/partial_daily/600000.csv';partial.parent.mkdir(parents=True)
    partial.write_text('symbol,date,close,volume\n600000,2026-09-29,99,10\n',encoding='utf-8')
    result=compare(root,db)
    assert db.execute_one('SELECT close FROM daily_bars_raw')['close']==17.
    assert db.execute_one('SELECT COUNT(*) AS n FROM intraday_partial_daily')['n']==0
    assert result['migrated_row_count']==result['migrated_file_count']==result['partial_daily_migrated_row_count']==0


def test_migration_compare_verifies_post_write_and_compare_false_stays_empty(store):
    root,db,_=store;write_csv(root,[row()])
    result=migrate_csv_to_sqlite({'data_root':str(root),'db_path':str(db.db_path),'adjustment':'raw','compare':True})
    assert result['migrated_row_count']==1 and result['compare_summary']['hash_match_ratio']==1.
    assert result['compare_summary']['common_row_count']==1
    result=migrate_csv_to_sqlite({'data_root':str(root),'db_path':str(db.db_path),'adjustment':'raw','compare':False})
    assert result['compare_summary']=={}


def test_dry_run_keeps_prediction_count_but_does_not_claim_comparison_match(store):
    root,db,_=store;write_csv(root,[row()])
    result=migrate_csv_to_sqlite({'data_root':str(root),'db_path':str(db.db_path),'adjustment':'raw','compare':True,'dry_run':True})
    assert result['migrated_row_count']==1
    assert result['compare_summary']['hash_match_ratio'] is None
    assert result['compare_summary']['missing_in_db_count']==1


def test_sample_zero_means_all_files_but_one_is_only_selected_scope(store):
    root,db,repo=store
    write_csv(root,[row()]);other={**row(),'symbol':'600001'};write_csv(root,[other],symbol='600001')
    repo.upsert_daily_bars([row(),other],'raw')
    assert compare(root,db,sample=0)['compare_summary']['compared_file_count']==2
    summary=compare(root,db,sample=1)['compare_summary']
    assert summary['compared_file_count']==1 and summary['common_row_count']==1


def test_duplicate_csv_identity_is_not_silently_called_matched(store):
    root,db,repo=store;write_csv(root,[row(),row()]);repo.upsert_daily_bars([row()],'raw')
    summary=compare(root,db)['compare_summary']
    assert summary['duplicate_csv_key_count']==1 and summary['status']=='mismatch'
    assert summary['common_row_count']==1


def test_consistent_legacy_symbol_hash_uses_normalized_identity(store):
    from quantdesk.data_store.hash_utils import compute_row_hash,DAILY_HASH_FIELDS
    root,db,repo=store;write_csv(root,[row()]);repo.upsert_daily_bars([row()],'raw')
    actual=db.execute_one('SELECT * FROM daily_bars_raw');actual['symbol']='600000.SH'
    db.execute('UPDATE daily_bars_raw SET symbol=?,row_hash=?',[actual['symbol'],compute_row_hash(actual,DAILY_HASH_FIELDS)])
    summary=compare(root,db)['compare_summary']
    assert summary['status']=='matched' and summary['hash_match_ratio']==1.


def test_conflicting_legacy_identity_is_comparison_error_not_empty_db(store):
    root,db,repo=store;write_csv(root,[row()]);repo.upsert_daily_bars([row()],'raw')
    alternate=db.execute_one('SELECT * FROM daily_bars_raw');alternate['symbol']='600000.SH';alternate['close']=25.
    db.upsert_many('daily_bars_raw',[alternate],['symbol','date'])
    before=db.execute('SELECT * FROM daily_bars_raw ORDER BY symbol')
    summary=compare(root,db)['compare_summary']
    assert summary['status']=='comparison_error' and summary['hash_match_ratio'] is None
    assert summary['date_set_diff'] is None and summary['comparison_errors']
    assert db.execute('SELECT * FROM daily_bars_raw ORDER BY symbol')==before


@pytest.mark.parametrize('bad_file',[False,True])
def test_bad_input_prevents_full_match_claim(store,bad_file):
    root,db,repo=store
    write_csv(root,[row()] if bad_file else [row(),row('2026-09-29',close=None)])
    if bad_file:write_csv(root,[],symbol='600001')
    repo.upsert_daily_bars([row()],'raw')
    summary=compare(root,db)['compare_summary']
    assert summary['status']=='partial_input' and summary['input_warnings']
    assert summary['common_row_count']==1 and summary['hash_match_ratio']==1.


def test_same_identity_across_files_and_multisymbol_file_compared_once(store,monkeypatch):
    root,db,repo=store
    other={**row(),'symbol':'600001'}
    write_csv(root,[row(),other]);write_csv(root,[row('2026-09-29')],symbol='part2')
    repo.upsert_daily_bars([row(),other,row('2026-09-29')],'raw')
    calls=[];original=DailyBarRepository.query_daily_bars
    def query(self,symbol,*args,**kwargs):
        calls.append(symbol)
        return original(self,symbol,*args,**kwargs)
    monkeypatch.setattr(DailyBarRepository,'query_daily_bars',query)
    summary=compare(root,db)['compare_summary']
    assert summary['status']=='matched' and summary['common_row_count']==3
    assert summary['database_row_count']==summary['csv_row_count']==3
    assert summary['duplicate_csv_key_count']==0 and sorted(calls)==['600000','600001']


def test_small_price_difference_cannot_be_matched_despite_hash_rounding(store):
    root,db,repo=store;write_csv(root,[row()]);repo.upsert_daily_bars([row()],'raw')
    db.execute('UPDATE daily_bars_raw SET close=10.0000001')
    summary=compare(root,db)['compare_summary']
    assert summary['hash_match_ratio']==1. and summary['close_diff_count']==1
    assert summary['status']=='mismatch'
