from unittest.mock import Mock, patch
from scripts.launcher_agent import Agent, PerformanceCollector


def test_only_idle_running_command_polls_are_throttled():
    agent=object.__new__(Agent)
    agent.commands=Mock()
    for op,args,result,wait in [
        ('command.poll',{}, {'stdout':'','stderr':'','exit_code':None},True),
        ('command.poll',{}, {'stdout':'data','stderr':'','exit_code':None},False),
        ('command.poll',{'data':'input'}, {'stdout':'','stderr':'','exit_code':None},False),
        ('command.poll',{}, {'stdout':'','stderr':'','exit_code':0},False),
        ('command.start',{}, {'started':True},False),
    ]:
        agent.commands.dispatch.return_value=result
        with patch('scripts.launcher_agent.time.sleep') as sleep:
            assert agent.dispatch(op,args)==result
            assert sleep.called==wait
            if wait:sleep.assert_called_once_with(.05)


def test_fresh_native_monitor_avoids_duplicate_container_sampling():
    collector=PerformanceCollector()
    with patch('scripts.launcher_agent.Path.exists',return_value=True), \
         patch('scripts.launcher_agent.Path.read_text',return_value='{"status":"published","sampled_at":95}'), \
         patch('scripts.launcher_agent.time.time',return_value=100), \
         patch('scripts.launcher_agent.load_performance_runtime') as load:
        assert collector.sample()==[]
        load.assert_not_called()


def test_stale_native_monitor_keeps_fallback_sampling():
    collector=PerformanceCollector();collector.sampler=Mock();collector.sampler.sample.return_value={'status':'live'}
    with patch('scripts.launcher_agent.Path.exists',return_value=True), \
         patch('scripts.launcher_agent.Path.read_text',return_value='{"status":"published","sampled_at":70}'), \
         patch('scripts.launcher_agent.time.time',return_value=100):
        assert collector.sample()=={'status':'live'}
