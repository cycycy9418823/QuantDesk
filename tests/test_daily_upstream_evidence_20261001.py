"""Upstream daily evidence regressions using synthetic transport and temp files."""
import json
from pathlib import Path
import pandas as pd
import pytest
from quantdesk import daily_price_providers as provider
from quantdesk.price_basis import classify_frame_price_basis
from scripts import update_market_data as upstream

DAY='2026-09-30'
STAMP='2026-09-30T15:06:03+08:00'

def row(**updates):
    value={'date':DAY,'symbol':'000001','open':10.0,'high':11.0,'low':9.0,'close':10.5,
        'volume':1000,'amount':10500,'adjust':'raw','source':'original-provider','updated_at':STAMP}
    value.update(updates)
    return value

def fetch(monkeypatch,frame):
    calls=[]
    def fake_fetch(symbol,start,end,adjust):
        calls.append([symbol,start,end,adjust]);return frame.copy(deep=True)
    monkeypatch.setattr(upstream,'fetch_hist_daily',fake_fetch)
    def no_fallback(*args,**kwargs):
        raise RuntimeError('synthetic fallback unavailable')
    monkeypatch.setattr(upstream,'fetch_sina_daily',no_fallback)
    result,warnings=provider.fetch_current_project_default('000001','2026-09-01',DAY,network=True)
    assert calls==[['000001','20260901','20260930','']]
    return result,warnings

@pytest.mark.parametrize('defect',['bad_date','bad_close','duplicate_day','preflight_attrs'])
def test_original_negative_basis_must_survive_default_chain(monkeypatch,tmp_path,defect):
    if defect=='bad_date':
        frame=pd.DataFrame([row(date='not-a-date',adjust='qfq'),row()])
    elif defect=='bad_close':
        frame=pd.DataFrame([row(date='2026-09-29',close=-1,adjust='qfq'),row()])
    elif defect=='duplicate_day':
        frame=pd.DataFrame([row(adjust='qfq'),row()])
    else:
        frame=pd.DataFrame([row()]);frame.attrs.update(observed_price_basis='mixed_or_unknown',price_basis_reason='mixed_price_basis')
    before=classify_frame_price_basis(frame)
    assert before[1],before
    output,warnings=fetch(monkeypatch,frame)
    after=classify_frame_price_basis(output)
    record={'defect':defect,'input_basis':before,'input_rows':len(frame),'output_basis':after,
        'output_rows':len(output),'output_adjust':output['adjust'].astype(str).tolist(),
        'output_source':output['source'].astype(str).tolist(),'warnings':warnings}
    (tmp_path/'observation.json').write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding='utf-8')
    assert after[1],record

def test_existing_source_identity_must_survive_default_chain(monkeypatch,tmp_path):
    frame=pd.DataFrame([row()]);output,warnings=fetch(monkeypatch,frame)
    record={'input_source':frame['source'].tolist(),'output_source':output['source'].tolist(),
        'output_updated_at':output['updated_at'].tolist(),'warnings':warnings}
    (tmp_path/'observation.json').write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding='utf-8')
    assert output['source'].tolist()==['original-provider'],record
    assert output['updated_at'].tolist()==[STAMP]

def test_local_normalization_must_not_invent_acquisition_time(tmp_path):
    frame=pd.DataFrame([row()]).drop(columns=['source','updated_at'])
    output=upstream.normalize_daily_bars(frame,'000001')
    record={'source':output['source'].astype(str).tolist(),'updated_at':output['updated_at'].astype(str).tolist()}
    (tmp_path/'observation.json').write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding='utf-8')
    assert output['updated_at'].isna().all(),record
    assert output['source'].isna().all(),record

def test_explicit_raw_normal_price_control(monkeypatch):
    output,_=fetch(monkeypatch,pd.DataFrame([row()]))
    assert classify_frame_price_basis(output)==('raw','')
    assert output[['open','high','low','close']].iloc[0].tolist()==[10.0,11.0,9.0,10.5]
    assert output['updated_at'].tolist()==[STAMP]

def test_missing_ohl_stays_missing(monkeypatch):
    frame=pd.DataFrame([row()]).drop(columns=['open','high','low'])
    output,_=fetch(monkeypatch,frame)
    assert output[['open','high','low']].isna().all().all()
    assert output['close'].tolist()==[10.5]


@pytest.mark.parametrize('basis,expected',[('raw','raw'),('qfq','forward_adjusted'),('hfq','back_adjusted')])
def test_explicit_request_basis_keeps_known_research_modes(basis,expected):
    frame=pd.DataFrame([row()]).drop(columns=['adjust'])
    output=upstream.normalize_daily_bars(frame,'000001',adjust_mode=basis)
    assert classify_frame_price_basis(output)==(expected,'')
    assert output['source'].tolist()==['original-provider']


@pytest.mark.parametrize('basis,expected',[('unknown','mixed_or_unknown'),('qfq','forward_adjusted'),('hfq','back_adjusted')])
def test_returned_canonical_basis_overrides_raw_request(monkeypatch,basis,expected):
    frame=pd.DataFrame([row()]).drop(columns=['adjust'])
    frame['price_basis']=basis
    output,_=fetch(monkeypatch,frame)
    assert classify_frame_price_basis(output)[0]==expected
    assert output['adjust'].tolist()==[basis]


@pytest.mark.parametrize('alias',['price_basis','复权','复权类型'])
def test_dual_basis_declarations_are_not_silently_dropped(alias):
    frame=pd.DataFrame([row()]);frame[alias]='qfq'
    with pytest.raises(ValueError,match='ambiguous_daily_adjust_columns'):
        upstream.normalize_daily_bars(frame,'000001',adjust_mode='')


def test_fetch_can_record_missing_acquisition_columns_only(monkeypatch):
    frame=pd.DataFrame([row()]).drop(columns=['adjust','source','updated_at'])
    monkeypatch.setattr(upstream,'now_text',lambda:STAMP)
    output,_=fetch(monkeypatch,frame)
    assert output['adjust'].tolist()==['raw']
    assert output['source'].tolist()==['eastmoney_hist']
    assert output['updated_at'].tolist()==[STAMP]


def test_returned_null_acquisition_columns_are_not_replaced(monkeypatch):
    frame=pd.DataFrame([row(source=None,updated_at=None)])
    output,_=fetch(monkeypatch,frame)
    assert output[['source','updated_at']].isna().all().all()


def test_local_source_label_does_not_declare_raw_or_fetch_time():
    frame=pd.DataFrame([row()]).drop(columns=['adjust','source','updated_at'])
    output=upstream.normalize_daily_bars(frame,'000001',source='raw-looking-provider')
    assert classify_frame_price_basis(output)[1]=='unknown_price_basis'
    assert output[['source','updated_at']].isna().all().all()


def test_empty_rejection_keeps_negative_evidence_and_cannot_merge():
    empty=pd.DataFrame();empty.attrs.update(observed_price_basis='mixed_or_unknown',price_basis_reason='mixed_price_basis')
    output=upstream.normalize_daily_bars(empty,'000001')
    assert classify_frame_price_basis(output)[1]=='mixed_price_basis'
    with pytest.raises(ValueError,match='daily_merge_rejected_empty_evidence'):
        upstream.merge_daily_frames(pd.DataFrame([row()]),output)


def test_mixed_merge_remains_mixed_before_cache_write(tmp_path):
    merged=upstream.merge_daily_frames(pd.DataFrame([row()]),pd.DataFrame([row(date='not-a-date',adjust='qfq')]))
    assert classify_frame_price_basis(merged)[1]=='mixed_price_basis'
    path=tmp_path/'000001.csv';path.write_bytes(b'old source file\n')
    with pytest.raises(ValueError,match='daily_cache_write_price_basis'):
        upstream.write_daily_cache(path,merged)
    assert path.read_bytes()==b'old source file\n'


@pytest.mark.parametrize('basis',['raw','qfq','hfq'])
def test_valid_cache_writer_preserves_basis_and_acquisition(tmp_path,basis):
    frame=pd.DataFrame([row(adjust=basis)]);path=tmp_path/'000001.csv'
    upstream.write_daily_cache(path,frame)
    restored=pd.read_csv(path)
    assert restored['adjust'].tolist()==[basis]
    assert restored['source'].tolist()==['original-provider']
    assert restored['updated_at'].tolist()==[STAMP]


@pytest.mark.parametrize('reason',['unknown_price_basis','mixed_price_basis'])
def test_preflight_cache_write_never_changes_old_file(tmp_path,reason):
    frame=pd.DataFrame([row()]);frame.attrs['price_basis_reason']=reason
    path=tmp_path/'000001.csv';path.write_bytes(b'old bytes\n')
    with pytest.raises(ValueError,match='daily_cache_write_price_basis'):
        upstream.write_daily_cache(path,frame)
    assert path.read_bytes()==b'old bytes\n'


def test_conflicting_old_cache_is_not_replaced_as_if_absent(tmp_path):
    path=tmp_path/'000001.csv'
    old=pd.DataFrame([row(date='2026-09-29',price_basis='qfq')])
    old.to_csv(path,index=False)
    before=path.read_bytes()
    with pytest.raises(ValueError,match='daily_merge_rejected_empty_evidence'):
        existing=upstream.read_daily_cache(path,'000001')
        merged=upstream.merge_daily_frames(existing,pd.DataFrame([row()]))
        upstream.write_daily_cache(path,merged)
    assert path.read_bytes()==before


def test_unreadable_existing_cache_reports_failure(tmp_path,monkeypatch):
    path=tmp_path/'000001.csv';path.write_bytes(b'existing history')
    def denied(_path):
        raise PermissionError('synthetic read failure')
    monkeypatch.setattr(upstream,'read_csv_frame',denied)
    failed=upstream.read_daily_cache(path,'000001')
    assert failed.empty
    assert 'synthetic read failure' in str(failed.attrs.get('warnings',[]))
    with pytest.raises(ValueError,match='daily_merge_rejected_empty_evidence'):
        upstream.merge_daily_frames(failed,pd.DataFrame([row()]))
    assert path.read_bytes()==b'existing history'


def test_absent_cache_still_allows_first_valid_write(tmp_path):
    path=tmp_path/'000001.csv'
    existing=upstream.read_daily_cache(path,'000001')
    assert existing.empty
    merged=upstream.merge_daily_frames(existing,pd.DataFrame([row()]))
    upstream.write_daily_cache(path,merged)
    restored=upstream.read_daily_cache(path,'000001')
    assert restored['source'].tolist()==['original-provider']
    assert restored['updated_at'].tolist()==[STAMP]
    assert classify_frame_price_basis(restored)==('raw','')


@pytest.mark.parametrize('basis',['raw','qfq','hfq'])
def test_cache_writer_canonicalizes_basis_alias_for_roundtrip(tmp_path,basis):
    frame=pd.DataFrame([row(adjust=basis)]).rename(columns={'adjust':'price_basis'})
    path=tmp_path/'000001.csv'
    upstream.write_daily_cache(path,frame)
    restored=pd.read_csv(path)
    assert restored['adjust'].tolist()==[basis]
    assert 'price_basis' not in restored.columns
    assert classify_frame_price_basis(restored)[1]==''


@pytest.mark.parametrize('warning',['csv_parse_failed:x','csv_bad_lines_skipped:x','csv_read_failed:x'])
def test_lossy_old_cache_read_does_not_overwrite_history(tmp_path,monkeypatch,warning):
    path=tmp_path/'000001.csv';path.write_bytes(b'original history')
    partial=pd.DataFrame([row(date='2026-09-29')]);partial.attrs['warnings']=[warning]
    monkeypatch.setattr(upstream,'read_csv_frame',lambda _path:partial)
    failed=upstream.read_daily_cache(path,'000001')
    assert warning in str(failed.attrs.get('warnings',[]))
    with pytest.raises(ValueError,match='daily_merge_rejected_empty_evidence'):
        merged=upstream.merge_daily_frames(failed,pd.DataFrame([row()]))
        upstream.write_daily_cache(path,merged)
    assert path.read_bytes()==b'original history'


def test_existing_empty_cache_is_not_an_absent_cache(tmp_path):
    path=tmp_path/'000001.csv';path.write_bytes(b'')
    failed=upstream.read_daily_cache(path,'000001')
    with pytest.raises(ValueError,match='daily_merge_rejected_empty_evidence'):
        upstream.merge_daily_frames(failed,pd.DataFrame([row()]))
    assert path.read_bytes()==b''


def test_real_parser_skipped_line_cannot_become_clean_cache(tmp_path):
    path=tmp_path/'000001.csv'
    path.write_text('date,close,adjust\n2026-09-29,10,raw\n2026-09-30,11,qfq,extra\n',encoding='utf-8')
    before=path.read_bytes()
    failed=upstream.read_daily_cache(path,'000001')
    assert 'csv_bad_lines_skipped' in str(failed.attrs.get('warnings',[]))
    with pytest.raises(ValueError,match='daily_merge_rejected_empty_evidence'):
        merged=upstream.merge_daily_frames(failed,pd.DataFrame([row()]))
        upstream.write_daily_cache(path,merged)
    assert path.read_bytes()==before


def test_normal_daily_cache_keeps_standard_header_and_prices(tmp_path):
    frame=pd.DataFrame([row()]).reindex(columns=provider.STANDARD_DAILY_COLUMNS)
    path=tmp_path/'000001.csv'
    upstream.write_daily_cache(path,frame)
    restored=pd.read_csv(path,dtype={'symbol':str})
    assert restored.columns.tolist()==provider.STANDARD_DAILY_COLUMNS
    assert restored[['date','symbol','open','high','low','close']].iloc[0].tolist()==[DAY,'000001',10.0,11.0,9.0,10.5]
