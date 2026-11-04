from task_progress import current_tasks


def test_only_verified_complete_recovery_replaces_failed_current_run():
    original={'id':'old','kind':'application-tests','status':'failed'}
    recovery={'id':'new','kind':'application-tests','status':'completed','recovery_of':['old'],'children':[{'status':'passed'}]}
    assert current_tasks([original],[recovery])==[]
    assert original['status']=='failed'  # The actual historical record is never rewritten.
    for status in ('running','failed','partial'):
        assert current_tasks([original],[{**recovery,'status':status}])==[original]
    assert current_tasks([original],[{**recovery,'children':[{'status':'failed'}]}])==[original]
    assert current_tasks([original],[{**recovery,'children':[]}])==[original]
    assert current_tasks([{**original,'status':'running'}],[recovery])==[{**original,'status':'running'}]
