import copy
from distributed_worker import SUITES
from github_sources import REPOSITORIES
from suite_cache import INPUTS
from test_scope import normalize, select

def test_lifetech_suite_has_immutable_sources_and_owned_checks():
    assert 'BmoreMedTech' in REPOSITORIES
    spec=SUITES['lifetech']
    assert spec['cwd']=='BmoreMedTech'
    assert 'home-tagline' not in str(spec['test'])  # The whole owned test catalog runs, including new files.
    assert 'scripts/*.test.mjs' in spec['test'][-1]
    assert 'build:lifetech' in spec['test'][-1]
    assert {'BmoreMedTech','OrgPortal'} <= set(INPUTS['lifetech'])
    assert normalize(suites=['lifetech'])=={'paths':[],'suites':['lifetech']}
    assert select(['lifetech'],copy.deepcopy(SUITES),normalize(suites=['lifetech']))==['lifetech']
