from pathlib import Path
import pytest
from core.bootstrap import prepare_project, snapshot, git, bootstrap_tests, configure_runtime
from core.git_manager import GitManager

def test_new_directory_git_without_user_index_changes(tmp_path):
    p = Path(prepare_project(tmp_path / 'new'))
    assert git(p, 'rev-parse', 'HEAD')
    assert git(p, 'ls-files') == ''

def test_snapshot_carries_untracked_tests_preserves_index_and_head(tmp_path):
    p = Path(prepare_project(tmp_path / 'new'))
    (p / 'tests').mkdir(); (p / 'tests/test_real.py').write_text('def test_real(): assert True\n')
    (p / '.env').write_text('PRIVATE=do-not-copy')
    before = git(p, 'status', '--porcelain'); head = git(p, 'rev-parse', 'HEAD')
    sha = snapshot(p)
    assert 'tests/test_real.py' in git(p, 'ls-tree', '-r', '--name-only', sha)
    assert '.env' not in git(p, 'ls-tree', '-r', '--name-only', sha)
    assert git(p, 'rev-parse', 'HEAD') == head
    assert git(p, 'status', '--porcelain') == before

def test_bootstrap_initial_test_and_existing_test_never_replaced(tmp_path):
    p = Path(prepare_project(tmp_path / 'new'))
    rel = bootstrap_tests(p)
    assert rel == 'tests/test_director_bootstrap.py'
    assert not git(p, 'status', '--porcelain')
    before = (p / rel).read_bytes()
    assert bootstrap_tests(p) is None
    assert (p / rel).read_bytes() == before

def test_existing_failed_test_not_bootstrapped(tmp_path):
    p = Path(prepare_project(tmp_path / 'new')); (p / 'tests').mkdir()
    (p / 'tests/test_failure.py').write_text('def test_failure(): assert False\n')
    assert bootstrap_tests(p) is None

def test_snapshot_preserves_staged_and_unstaged_versions(tmp_path):
    p = Path(prepare_project(tmp_path / 'new'))
    source = p / 'feature.py'
    source.write_text('value = 1\n')
    git(p, 'add', 'feature.py')
    source.write_text('value = 2\n')
    before = git(p, 'status', '--porcelain')
    sha = snapshot(p)
    assert git(p, 'show', ':feature.py') == 'value = 1'
    assert source.read_text() == 'value = 2\n'
    assert git(p, 'show', sha + ':feature.py') == 'value = 2'
    assert git(p, 'status', '--porcelain') == before

def test_snapshot_excludes_untracked_sensitive_directories(tmp_path):
    p = Path(prepare_project(tmp_path / 'new'))
    for rel in ['secrets/settings.json', 'credentials/config.json', 'data/fixture.json']:
        target = p / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text('{}')
    sha = snapshot(p)
    # Only names that look sensitive are excluded; an ordinary data/ fixture belongs in the working copy.
    assert git(p, 'ls-tree', '-r', '--name-only', sha) == 'data/fixture.json'

def test_control_root_rejected():
    from core.bootstrap import SOURCE_ROOT
    with pytest.raises(ValueError): prepare_project(SOURCE_ROOT)
    with pytest.raises(ValueError): prepare_project('')

def test_project_runtime_selected_without_global_config_mutation(tmp_path):
    exe = tmp_path / '.venv/Scripts/python.exe'; exe.parent.mkdir(parents=True); exe.touch()
    cfg = {'verification': {'test_command': 'python -m pytest tests/'}}
    assert configure_runtime(cfg, tmp_path)['verification']['test_command'][0] == str(exe.resolve())
    assert cfg['verification']['test_command'] == 'python -m pytest tests/'

def test_protected_glob_outside_grants_keeps_default_deny(tmp_path):
    from agents.antigravity_builder import AntigravityBuilder
    from schemas.order import OrderSchema
    builder = AntigravityBuilder()
    builder._setup_scoped_permissions(tmp_path, OrderSchema(goal='test', allowed_paths=['src/**'], protected_paths=['.env.*']))
    assert builder._native_protected_permission_paths == []
    assert not (tmp_path / '.env.local').exists()

def test_protected_glob_overlap_still_fails_closed(tmp_path):
    from agents.antigravity_builder import AntigravityBuilder
    from schemas.order import OrderSchema
    with pytest.raises(ValueError):
        AntigravityBuilder()._setup_scoped_permissions(tmp_path, OrderSchema(goal='test', allowed_paths=['src/**'], protected_paths=['src/secret*']))

@pytest.mark.parametrize('with_product_test', [True, False])
def test_empty_project_prepares_and_requires_real_tests(tmp_path, with_product_test):
    from tests.test_v0234_resume_parity import _mock_orch
    from schemas.order import OrderSchema
    orch = _mock_orch(bootstrap={'enabled': True}, verification={'test_command': 'python -m pytest tests/'})
    def build(b, order, wt):
        p = Path(wt)
        (p / 'feature.py').write_text('def double(x):\n    return x * 2\n')
        if with_product_test:
            (p / 'tests/test_feature.py').write_text('from feature import double\ndef test_double():\n    assert double(4) == 8\n')
        return ['feature.py'] + (['tests/test_feature.py'] if with_product_test else [])
    order = OrderSchema(goal='Create double helper and tests', allowed_paths=['feature.py', 'tests/**'], acceptance_criteria=['double(4) == 8'])
    result = orch.run(str(tmp_path / 'fresh'), order.goal, order=order, builder_fn=build)
    assert result['baseline_verification']['passed'] is True
    assert result['baseline_verification']['test_counts']['passed'] == 1
    assert result['final_state'] == ('PASS' if with_product_test else 'FAIL')
