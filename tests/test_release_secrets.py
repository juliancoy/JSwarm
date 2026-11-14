import subprocess
import pytest
from scripts.release_secrets import scan_chunk, scan_git, check_repositories, check_tree


def commit(folder,files):
    subprocess.run(['git','init','-q',str(folder)],check=True)
    for name,data in files.items():
        path=folder/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data)
    subprocess.run(['git','-C',str(folder),'add','-f','.'],check=True)
    subprocess.run(['git','-C',str(folder),'-c','user.name=Tests','-c','user.email=test@example.invalid','commit','-qm','fixture'],check=True)


def test_committed_ignored_files_are_checked_and_values_are_redacted(tmp_path):
    value=b'gh'+b'p_'+b'Z9ab'*10
    commit(tmp_path,{'.gitignore':b'.env\n','.env':b'TOKEN='+value})
    with pytest.raises(RuntimeError) as error:check_repositories([('fixture',tmp_path,'HEAD')])
    assert '.env:1' in str(error.value)
    assert value.decode() not in str(error.value)


def test_scan_uses_commit_not_working_copy_and_checks_export_ignored_files(tmp_path):
    value=b'-----BEGIN '+b'PRIVATE KEY-----\nabc'
    commit(tmp_path,{'.gitattributes':b'hidden export-ignore\n','hidden':value,'normal.py':b'print(1)'})
    (tmp_path/'hidden').write_bytes(b'clean now')
    assert any(f.path=='hidden' and f.rule=='private key' for f in scan_git(tmp_path))


def test_placeholder_and_public_key_are_allowed_but_literal_secrets_block():
    assert not scan_chunk('sample',b'api_key="YOUR_API_KEY_PLACEHOLDER"')
    assert not scan_chunk('public.pem',b'-----BEGIN PUBLIC KEY-----')
    assert scan_chunk('config.py',b'api_key="'+b'9abcDEF123456789xyz'+b'"')
    assert scan_chunk('account.json',b'{"type": ' + b'"service_account"}')


def test_binary_and_chunk_boundary_secrets_block(tmp_path):
    token=b'gh'+b'p_'+b'aB9x'*10
    commit(tmp_path,{'blob.bin':b'\x00'*(1024*1024-10)+token+b'\x00'})
    assert any(f.rule=='GitHub token' for f in scan_git(tmp_path))


def test_exported_tree_does_not_follow_symlinks(tmp_path):
    outside=tmp_path/'outside';outside.mkdir();(outside/'private.key').write_text('private')
    root=tmp_path/'release';root.mkdir();(root/'alias').symlink_to(outside,target_is_directory=True)
    check_tree(root)
    (root/'.env').write_text('sensitive')
    with pytest.raises(RuntimeError,match='sensitive filename'):check_tree(root)


def test_git_failure_blocks_scan(tmp_path):
    with pytest.raises(subprocess.CalledProcessError):scan_git(tmp_path)


def test_fixture_exception_is_bound_to_exact_repository_path_rule_and_value(monkeypatch,tmp_path):
    import json
    from scripts import release_secrets as scanner
    value=b'api_key="'+b'fixture-secret-value-123456'+b'"'
    finding=scan_chunk('fixture.py',value)[0]
    (tmp_path/'release-secrets-allowlist.json').write_text(json.dumps([{'repository':'fixture','path':'fixture.py','rule':finding.rule,'sha256':finding.fingerprint,'reason':'Synthetic test example'}]))
    monkeypatch.setattr(scanner,'__file__',str(tmp_path/'release_secrets.py'))
    assert scanner.allowed('fixture',finding)
    assert not scanner.allowed('other',finding)
    assert not scanner.allowed('fixture',scan_chunk('other.py',value)[0])
    assert not scanner.allowed('fixture',scan_chunk('fixture.py',value.replace(b'123456',b'987654'))[0])
