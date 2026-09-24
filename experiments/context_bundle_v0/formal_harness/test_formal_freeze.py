import hashlib, json, unittest
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).parent))
import formal_harness as h

ROOT=Path(__file__).parent.parent/"formal"
class FormalFreezeTests(unittest.TestCase):
 def test_formal_artifact_closure(self):
  manifest=json.loads((ROOT/"formal-manifest.json").read_text(encoding="utf-8")); cells=manifest["cells"]
  self.assertEqual("D9-C5-FORMAL",manifest["experiment_id"]); self.assertEqual(36,len(cells)); self.assertEqual(36,len({c["cell_id"] for c in cells}))
  self.assertEqual(18,sum(c["arm"]=="A" for c in cells)); self.assertEqual(18,sum(c["arm"]=="B" for c in cells))
  self.assertEqual(set(h.FROZEN_CASES),set(manifest["prompt_config_sha256"]))
  self.assertEqual(h.digest(manifest), (ROOT/"formal-manifest.sha256").read_text().strip())
 def test_prompts_are_real_and_contract_delta_is_only_bundle(self):
  contract=json.loads((ROOT/"dispatch-contract.json").read_text()); self.assertEqual({"task-context"},set(contract["inventories"]["B"])-set(contract["inventories"]["A"])); self.assertFalse(set(contract["inventories"]["A"])-set(contract["inventories"]["B"]))
  for p in (ROOT/"prompts").glob("*.json"):
   data=json.loads(p.read_text(encoding="utf-8")); self.assertEqual(hashlib.sha256(data["problem_statement"].encode()).hexdigest(),data["problem_statement_sha256"])
