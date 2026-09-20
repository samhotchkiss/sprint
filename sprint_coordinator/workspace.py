"""Trusted, assignment-scoped code worktrees and independently captured changes."""
import subprocess
from pathlib import Path


def git(root,*args):
    return subprocess.run(['git','-C',str(root),*args],check=True,capture_output=True,text=True,timeout=30).stdout.strip()


def prepare(config, row):
    base=config.raw.get('code_base_ref')
    if not base:
        raise ValueError('code_base_ref must identify verified production code')
    commit=git(config.project_root,'rev-parse','--verify',base+'^{commit}')
    root=config.board_data_dir/'coordinator-worktrees'
    root.mkdir(parents=True,exist_ok=True)
    path=root/row['id']
    branch='coordinator/'+row['id']
    if not path.exists():
        git(config.project_root,'worktree','add','-b',branch,str(path),commit)
    elif git(path,'rev-parse','--show-toplevel')!=str(path.resolve()):
        raise ValueError('assignment worktree identity mismatch')
    return {'path':str(path.resolve()),'base_commit':commit,'branch':branch}


def evidence(workspace):
    path=Path(workspace['path'])
    untracked=git(path,'ls-files','--others','--exclude-standard')
    if untracked:
        raise ValueError('commit new source files before requesting verification')
    diff=git(path,'diff','--no-ext-diff',workspace['base_commit'],'--')
    if not diff:
        raise ValueError('no code changes to verify')
    if len(diff.encode())>200_000:
        raise ValueError('change exceeds bounded review size; split the work')
    return {'base_commit':workspace['base_commit'],'head_commit':git(path,'rev-parse','HEAD'),
            'diff':diff,'source':'coordinator git inspection'}
