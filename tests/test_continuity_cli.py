import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from sprint_coordinator.cli import main
from sprint_coordinator.config import example_config

class ContinuityCLITest(unittest.TestCase):
    def test_missing_delivery_fails_without_acknowledging_anything(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);cfg=example_config(tmp);p=root/'config.json';p.write_text(json.dumps(cfg))
            out=io.StringIO()
            with contextlib.redirect_stdout(out):
                rc=main(['continuity-ack','--config',str(p),'--delivery','missing','--through-seq','20'])
            self.assertEqual(rc,1)
            self.assertEqual(json.loads(out.getvalue())['reason'],'unknown_delivery')
            out=io.StringIO()
            with contextlib.redirect_stdout(out):
                rc=main(['continuity-status','--config',str(p)])
            self.assertEqual(rc,0)
            self.assertEqual(json.loads(out.getvalue())['pending'],[])
