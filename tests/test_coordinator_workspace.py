from pathlib import Path
from types import SimpleNamespace
import subprocess
import tempfile
import unittest
from sprint_coordinator.workspace import prepare,evidence,git

class WorkspaceTests(unittest.TestCase):
    def test_build_and_review_are_bound_to_an_isolated_production_base(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();git(root,'init')
            git(root,'config','user.name','Test');git(root,'config','user.email','test@example.invalid')
            (root/'app.txt').write_text('original\n');git(root,'add','app.txt');git(root,'commit','-m','base')
            base=git(root,'rev-parse','HEAD')
            cfg=SimpleNamespace(project_root=root,board_data_dir=root/'.sprint',raw={'code_base_ref':base})
            work=prepare(cfg,{'id':'assignment-1'});path=Path(work['path'])
            self.assertNotEqual(path,root)
            (path/'app.txt').write_text('fixed\n')
            result=evidence(work)
            self.assertIn('+fixed',result['diff']);self.assertEqual(result['base_commit'],base)
            self.assertEqual((root/'app.txt').read_text(),'original\n')
            (path/'untracked.txt').write_text('new source')
            with self.assertRaisesRegex(ValueError,'commit new source'):evidence(work)
            git(root,'worktree','remove','--force',str(path))

if __name__=='__main__':unittest.main()
