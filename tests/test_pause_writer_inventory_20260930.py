"""T36 only fake scheduler commands; all pause records are temporary."""
import json
from types import SimpleNamespace
import pytest
from quantdesk import desk_control as control

NEW_WRITERS=['QuantDesk AI Trader Evening (GLM)','QuantDesk Intraday Data Refresh','QuantDesk Snapshot Pull']


class FakeScheduler:
    def __init__(self,states,fail=None,still_running=False):
        self.states=dict(states);self.fail=fail;self.still_running=still_running;self.commands=[]
    def __call__(self,argv,**kwargs):
        self.commands.append(list(argv));operation=argv[1];name=argv[3]
        assert argv[0]=='schtasks' and operation in {'/Query','/End','/Change'}
        if self.fail==operation or self.fail==('/Query_after_end' if operation=='/Query' and any(c[1]=='/End' for c in self.commands) else 'never'):
            return SimpleNamespace(returncode=1,stdout='',stderr='Access is denied.')
        if operation=='/Query':
            if name not in self.states:return SimpleNamespace(returncode=1,stdout='',stderr='not found')
            return SimpleNamespace(returncode=0,stdout=f'Status: {self.states[name]}\n',stderr='')
        if operation=='/End':
            if not self.still_running:self.states[name]='Ready'
        else:self.states[name]='Ready' if '/ENABLE' in argv else 'Disabled'
        return SimpleNamespace(returncode=0,stdout='',stderr='')


@pytest.fixture(autouse=True)
def reject_real_runner(monkeypatch):
    def forbidden(*args,**kwargs):raise AssertionError('real scheduler runner forbidden')
    monkeypatch.setattr(control.subprocess,'run',forbidden)


@pytest.mark.parametrize('writer',NEW_WRITERS)
@pytest.mark.parametrize('state',['Ready','Running','Disabled','missing','unknown'])
def test_preview_new_writer_states_are_explicit_without_actions(tmp_path,writer,state):
    runner=FakeScheduler({} if state=='missing' else {writer:state})
    result=control.pause_writers(tmp_path,apply=False,runner=runner)
    preview={item['name']:item for item in result['preview']}
    expected={'Ready':'disable','Running':'end_then_disable','Disabled':'already_disabled','missing':'missing','unknown':'needs_confirmation'}[state]
    assert preview[writer]['planned_action']==expected
    assert preview[writer]['stop_confirmation_required']==(state=='Running')
    assert all(c[1]=='/Query' for c in runner.commands)
    assert result['actions']==[] and not (tmp_path/'state/desk_control.json').exists()
    assert result['runtime_quiescence_proven'] is False


def test_preview_query_failure_is_visible_not_success(tmp_path):
    runner=FakeScheduler({},fail='/Query')
    result=control.pause_writers(tmp_path,apply=False,runner=runner)
    assert result['ok'] is False
    assert all(p['planned_action']=='query_failed' for p in result['preview'])
    assert result['failed_tasks'] and not (tmp_path/'state/desk_control.json').exists()


@pytest.mark.parametrize('writer',NEW_WRITERS)
@pytest.mark.parametrize('state',['Ready','Running','Disabled','missing'])
def test_new_writer_pause_and_restore_only_previously_enabled(tmp_path,writer,state):
    reader='QuantDesk Daily Review'
    runner=FakeScheduler({reader:'Ready',**({} if state=='missing' else {writer:state})})
    result=control.pause_writers(tmp_path,apply=True,runner=runner)
    if state in {'Ready','Running'}:
        assert writer in result['paused_tasks'] and runner.states[writer]=='Disabled'
        resumed=control.resume_writers(tmp_path,apply=True,runner=runner)
        assert resumed['resumed_tasks']==[writer] and runner.states[writer]=='Ready'
    else:
        assert writer not in result['paused_tasks']
        control.resume_writers(tmp_path,apply=True,runner=runner)
        assert runner.states.get(writer)==('Disabled' if state=='Disabled' else None)
    assert runner.states[reader]=='Ready'
    assert all(c[3]!=reader for c in runner.commands)


@pytest.mark.parametrize('failure',['/Query','/End','/Change','/Query_after_end'])
def test_failure_never_becomes_success_or_loses_restore_record(tmp_path,failure):
    writer=NEW_WRITERS[0];runner=FakeScheduler({writer:'Running'},fail=failure)
    result=control.pause_writers(tmp_path,apply=True,runner=runner)
    assert result['ok'] is False and writer in result['failed_tasks']
    stored=json.loads((tmp_path/'state/desk_control.json').read_text(encoding='utf-8-sig'))
    assert (writer in stored['paused_tasks'])==(failure=='/Query_after_end')


def test_unconfirmed_end_stays_waiting_but_preserves_successful_disable(tmp_path):
    writer=NEW_WRITERS[1];runner=FakeScheduler({writer:'Running'},still_running=True)
    result=control.pause_writers(tmp_path,apply=True,runner=runner)
    assert result['ok'] is False and writer in result['paused_tasks']
    assert any(s['name']==writer and s['stop']=='waiting' for s in result['writer_stops'])


def test_failed_resume_preserves_only_failed_record(tmp_path):
    writer=NEW_WRITERS[0];runner=FakeScheduler({writer:'Ready'})
    control.pause_writers(tmp_path,apply=True,runner=runner)
    runner.fail='/Change'
    result=control.resume_writers(tmp_path,apply=True,runner=runner)
    assert result['ok'] is False
    assert json.loads((tmp_path/'state/desk_control.json').read_text(encoding='utf-8-sig'))['paused_tasks']==[writer]


def test_original_first_three_inventory_order_preserved():
    assert control.WRITER_TASKS[:3]==('QuantDesk Data Close Pipeline','QuantDesk AI Trader Evening','QuantDesk Snapshot Collector')
    assert set(NEW_WRITERS).issubset(control.WRITER_TASKS)
    assert len(control.WRITER_TASKS)==6


def test_resume_preview_does_not_enable_or_change_record(tmp_path):
    writer=NEW_WRITERS[0];runner=FakeScheduler({writer:'Ready'})
    control.pause_writers(tmp_path,apply=True,runner=runner)
    path=tmp_path/'state/desk_control.json';before=path.read_bytes();runner.commands.clear()
    result=control.resume_writers(tmp_path,apply=False,runner=runner)
    assert result['preview']==[{'name':writer,'planned_action':'enable_recorded_task'}]
    assert result['resumed_tasks']==[] and result['actions']==[]
    assert runner.commands==[] and path.read_bytes()==before
