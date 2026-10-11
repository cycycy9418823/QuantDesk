"""T32: actual draft writer must never become a market-data writer."""
from pathlib import Path
import hashlib
import json
from unittest.mock import Mock

import pytest
from flask import Flask
from quantdesk import theme_map_import_draft as draft
from scripts.server.routes.theme_map import ThemeMapRouteDeps, register_theme_map_routes

POSTS = ['/api/theme-map/import-draft', '/api/theme-map/generate-import-draft',
         '/api/theme-map/import', '/api/theme-map/refresh', '/api/theme-map/update']

@pytest.fixture
def isolated(monkeypatch, tmp_path):
    root=tmp_path/'project'; root.mkdir()
    original_root=draft.PROJECT_ROOT
    for name,value in vars(draft).copy().items():
        if name.isupper() and isinstance(value,Path):
            relative=value.relative_to(original_root) if value.is_relative_to(original_root) else Path(value.name)
            monkeypatch.setattr(draft,name,root/relative)
    monkeypatch.setattr(draft,'PROJECT_ROOT',root)
    pending=draft.STOCK_THEME_MAP_PENDING_CSV
    pending.parent.mkdir(parents=True,exist_ok=True)
    pending.write_text('symbol,name\n600000,浦发银行\n',encoding='utf-8')
    callbacks={key:Mock(return_value={'ok':True}) for key in ['import','update','rebuild']}
    generator=Mock(wraps=draft.generate_import_draft)
    def old_resolver(value,default):
        p=Path(value) if value else default
        return p if p.is_absolute() else root/p
    app=Flask(__name__)
    register_theme_map_routes(app,ThemeMapRouteDeps(
        stock_theme_map_csv=lambda:draft.STOCK_THEME_MAP_CSV,
        stock_theme_map_import_csv=lambda:draft.STOCK_THEME_MAP_IMPORT_CSV,
        theme_map_status_payload=lambda:{},theme_map_preview=lambda n:[],
        preview_theme_map_import_draft=lambda n:{},generate_theme_map_import_draft=generator,
        import_theme_map=callbacks['import'],update_theme_map=callbacks['update'],
        rebuild_limit_up_review_attribution_cache=callbacks['rebuild'],
        resolve_project_csv_path=old_resolver,request_truthy=bool))
    return root,app.test_client(),generator,callbacks

@pytest.mark.parametrize('via',['direct',*POSTS[:2]])
@pytest.mark.parametrize('relative',['data/daily/600000.csv','data/intraday/600000.csv','data/benchmark/hs300.csv','state/book.csv','results/research/draft.csv','data/universe/stock_theme_map.csv','../escape.csv'])
def test_t32_protected_output_untouched(isolated,via,relative):
    root,client,generator,_=isolated
    target=(root/relative).resolve();target.parent.mkdir(parents=True,exist_ok=True)
    target.write_bytes(b'date,open,high,low,close,adjust\n2026-09-29,10,11,9,10,raw\n')
    before=hashlib.sha256(target.read_bytes()).hexdigest()
    if via=='direct':
        with pytest.raises(ValueError): draft.generate_import_draft(source='pending',overwrite=True,output_path=target)
    else:
        response=client.post(via,json={'source':'pending','overwrite':True,'output':str(target)})
        assert response.status_code==400,response.get_json()
        assert response.get_json()['status']=='bad_output_path'
        generator.assert_not_called()
    assert hashlib.sha256(target.read_bytes()).hexdigest()==before

@pytest.mark.parametrize('endpoint',POSTS)
@pytest.mark.parametrize('body',['[]','false','0','null','"text"','[1]','{broken',''])
def test_t32_invalid_body_no_business(isolated,endpoint,body):
    _,client,generator,callbacks=isolated
    response=client.post(endpoint,data=body,content_type='application/json')
    assert response.status_code==400
    assert response.get_json()['status']=='bad_request_body'
    assert response.get_json()['message_for_ui']
    generator.assert_not_called()
    for callback in callbacks.values(): callback.assert_not_called()

@pytest.mark.parametrize('value',[[],{},False,0,42])
def test_t32_bad_output_type(isolated,value):
    _,client,generator,_=isolated
    response=client.post(POSTS[0],json={'output':value})
    assert response.status_code==400
    generator.assert_not_called()
    with pytest.raises(ValueError): draft.generate_import_draft(output_path=value)

@pytest.mark.parametrize('via',['direct',*POSTS[:2]])
@pytest.mark.parametrize('output',[None,'results/theme_map_drafts/demo.csv'])
def test_t32_valid_draft_real_write(isolated,via,output):
    root,client,_,_=isolated
    if via=='direct': payload=draft.generate_import_draft(source='pending',output_path=output)
    else:
        response=client.post(via,json={'source':'pending','output':output})
        assert response.status_code==200,response.get_json()
        payload=response.get_json()
    target=draft.STOCK_THEME_MAP_IMPORT_CSV if output is None else root/output
    assert target.exists()
    assert payload['ok'] and payload['written_rows']==1
    assert payload['filled_count']+payload['pending_manual_count']==1
    assert payload['network'] is False and payload['provider_results']
    assert 'symbol' in target.read_text(encoding='utf-8-sig')

@pytest.mark.parametrize('endpoint',POSTS)
def test_t32_empty_object_preserves_endpoint(isolated,endpoint):
    _,client,_,_=isolated
    response=client.post(endpoint,json={})
    assert response.status_code==200,response.get_json()

def test_t32_private_default_cannot_authorize_arbitrary_csv(isolated):
    root,*_=isolated
    with pytest.raises(ValueError): draft._resolve_project_csv_path(None,root/'data/daily/600000.csv')

@pytest.mark.parametrize('kind',['parent','default_file','dedicated_root'])
def test_t32_symlink_redirect_rejected(isolated,kind):
    root,client,generator,_=isolated
    protected=root/'data/daily';protected.mkdir(parents=True)
    victim=protected/'600000.csv';victim.write_bytes(b'protected')
    path=root/'results/theme_map_drafts/link/600000.csv'
    link=path.parent;dest=protected;directory=True
    if kind=='default_file':
        path=draft.STOCK_THEME_MAP_IMPORT_CSV;link=path;dest=victim;directory=False
    elif kind=='dedicated_root':
        path=root/'results/theme_map_drafts/600000.csv';link=path.parent
    link.parent.mkdir(parents=True,exist_ok=True)
    try: link.symlink_to(dest,target_is_directory=directory)
    except OSError as exc: pytest.skip(f'host symlink unavailable: {exc}')
    with pytest.raises(ValueError): draft.generate_import_draft(source='pending',overwrite=True,output_path=path)
    response=client.post(POSTS[0],json={'source':'pending','overwrite':True,'output':str(path)})
    assert response.status_code==400
    generator.assert_not_called()
    assert victim.read_bytes()==b'protected'


@pytest.mark.parametrize('endpoint',POSTS[:2])
@pytest.mark.parametrize('overwrite',[True,False])
def test_t32_literal_relative_daily_output_rejected(isolated,endpoint,overwrite):
    root,client,generator,_=isolated
    target=root/'data/daily/600000.csv'
    target.parent.mkdir(parents=True,exist_ok=True)
    target.write_bytes(b'date,open,high,low,close,adjust\n2026-09-29,10,11,9,10,raw\n')
    before=hashlib.sha256(target.read_bytes()).hexdigest()
    response=client.post(endpoint,json={
        'source':'pending','overwrite':overwrite,'output':'data/daily/600000.csv',
    })
    assert response.status_code==400,response.get_json()
    assert response.get_json()['status']=='bad_output_path'
    generator.assert_not_called()
    assert hashlib.sha256(target.read_bytes()).hexdigest()==before
