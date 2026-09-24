"""Materialize D9-C2's already-frozen formal inputs; never dispatches work."""
from __future__ import annotations
import hashlib, json
from pathlib import Path
from formal_harness import ARM_SHA, FROZEN_CASES, FROZEN_INVENTORY_A, FROZEN_INVENTORY_B, cell_id, digest

DATASET = "c2855792b006af41c67202d33883fb9d46362853"
PARQUET_SHA = "2f56535bdc73eb8a68bf4ebb49789d8e9cd4f219ea60df6290b85278aee61ca8"

def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return digest(value)

def main(parquet: Path, output: Path) -> None:
    import pyarrow.parquet as pq
    if hashlib.sha256(parquet.read_bytes()).hexdigest() != PARQUET_SHA: raise ValueError("dataset hash drift")
    if output.exists(): raise ValueError("formal artifact root already exists")
    rows={r["instance_id"]: r["problem_statement"] for r in pq.read_table(parquet, columns=["instance_id","problem_statement"]).to_pylist()}
    if set(rows) & set(FROZEN_CASES) != set(FROZEN_CASES): raise ValueError("missing frozen prompt")
    output.mkdir(parents=True); prompts=output/"prompts"; prompts.mkdir()
    prompt_hashes={}
    for case, prompt in sorted((c,rows[c]) for c in FROZEN_CASES):
        payload={"schema":"d9-c5-formal-prompt-v1","case_id":case,"source":{"dataset_revision":DATASET,"file":"data/full.parquet","sha256":PARQUET_SHA,"field":"problem_statement"},"problem_statement":prompt,"problem_statement_sha256":hashlib.sha256(prompt.encode()).hexdigest()}
        prompt_hashes[case]=write_json(prompts/f"{case}.json",payload)
    contract={"schema":"d9-c5-formal-dispatch-contract-v1","experiment_id":"D9-C5-FORMAL","artifact_class":"formal","model":"deepseek-v4-flash","agent_adapter":"claude -p --output-format stream-json --verbose --dangerously-skip-permissions","working_directory":"fresh detached workspace at cell base_commit","timeout_s":3600,"environment_required":["ANTHROPIC_BASE_URL","ANTHROPIC_AUTH_TOKEN"],"environment_runtime":["PYTHONPATH"],"trace_capture":"raw stdout/stderr plus stream-json","source_repo":"facebook/zstd","runtime_sha":ARM_SHA,"inventories":{"A":sorted(FROZEN_INVENTORY_A),"B":sorted(FROZEN_INVENTORY_B)},"inventory_delta":["task-context"]}
    contract_hash=write_json(output/"dispatch-contract.json",contract)
    cells=[]
    for case,(original,base) in sorted(FROZEN_CASES.items()):
        for arm in ("A","B"):
            for repeat in (1,2,3):
                cells.append({"cell_id":cell_id(case,arm,repeat),"case_id":case,"original_instance_id":original,"source_repo":"facebook/zstd","base_commit":base,"arm":arm,"repeat":repeat,"runtime_sha":ARM_SHA[arm],"prompt_config_id":case,"prompt_config_sha256":prompt_hashes[case],"dispatch_contract_sha256":contract_hash,"command_inventory":contract["inventories"][arm],"workspace_identity_input":cell_id(case,arm,repeat)})
    manifest={"schema":"d9-c5-formal-manifest-v1","experiment_id":"D9-C5-FORMAL","artifact_class":"formal","dataset":{"revision":DATASET,"parquet_sha256":PARQUET_SHA},"dispatch_contract_sha256":contract_hash,"prompt_config_sha256":prompt_hashes,"cells":cells}
    manifest_hash=write_json(output/"formal-manifest.json",manifest)
    (output/"formal-manifest.sha256").write_text(manifest_hash+"\n",encoding="ascii")
    (output/"dispatch-contract.sha256").write_text(contract_hash+"\n",encoding="ascii")
    print(json.dumps({"cells":len(cells),"manifest_sha256":manifest_hash,"dispatch_sha256":contract_hash,"prompts":len(prompt_hashes)}))

if __name__ == "__main__":
    import argparse
    p=argparse.ArgumentParser(); p.add_argument("--parquet",type=Path,required=True); p.add_argument("--output",type=Path,required=True); a=p.parse_args(); main(a.parquet,a.output)
