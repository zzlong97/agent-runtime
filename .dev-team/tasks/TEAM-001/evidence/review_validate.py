"""只读核对 TEAM-001 审核出口及其版本引用，不修改被审产物或状态。"""
import argparse
import hashlib
import json
from pathlib import Path

import yaml


def version(path):
    return 'sha256:' + hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--candidate', type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[4]
    task = root / '.dev-team/tasks/TEAM-001'
    actual_path = task / 'state.yaml'
    actual = yaml.safe_load(actual_path.read_text(encoding='utf-8'))
    selected = args.candidate or actual_path
    state = yaml.safe_load(selected.read_text(encoding='utf-8'))
    if args.candidate:
        assert actual['state_version'] == 3
        assert actual['status'] == '待审核'
        assert actual['current_role'] == 'dev-team-reviewer'
        assert version(actual_path) == 'sha256:150fa81dd10b4903a2f92fd711041d429fc85fccd1491eb093281b9ea8447ef6'
        assert state['baselines'] == actual['baselines']
        assert state['artifacts'] == actual['artifacts']
        assert state['project_gates'] == actual['project_gates']
        assert state['checkpoints'][0] == actual['checkpoints'][0]
    assert state['task_id'] == 'TEAM-001'
    assert state['state_version'] == 4
    assert state['status'] == '待负责人决策'
    assert state['resume_from'] == '已验收'
    assert state['current_role'] == 'dev-team-controller'
    assert state['updated_by'] == 'dev-team-reviewer'
    assert state['blocked_reason'] is None
    assert state['current_operation']['kind'] == '角色工作'
    assert state['current_operation']['status'] == '已完成'
    assert state['latest_handoff'] == '.dev-team/tasks/TEAM-001/handoffs/0002-review.md'
    assert (root / state['latest_handoff']).is_file()
    index = yaml.safe_load((root / '.dev-team/index.yaml').read_text(encoding='utf-8'))
    assert set(index['tasks']) == {'TEAM-001'}
    assert index['tasks']['TEAM-001']['state_file'] == '.dev-team/tasks/TEAM-001/state.yaml'
    assert 'status' not in index['tasks']['TEAM-001']
    assert not (task / 'context.md').exists()
    for baseline in state['baselines'].values():
        assert version(root / baseline['path']) == baseline['version']
        assert baseline['approval']['status'] == '已批准'
        assert '29dcca1e71d3b4faa7517fc95e5468ea6a826bb0' in baseline['approval']['source_ref']
    checked = 0
    for item in state['artifacts']:
        assert version(root / item['path']) == item['version'], item['path']
        checked += 1
    boundaries = [item['boundary'] for item in state['checkpoints']]
    assert set(boundaries) == {'开发交付', '开发验收'}
    assert len(boundaries) == len(set(boundaries))
    for checkpoint in state['checkpoints']:
        assert checkpoint['validation_status'] == '已确认有效'
        assert (root / checkpoint['handoff']).is_file()
        for name, expected in checkpoint['baseline_versions'].items():
            assert state['baselines'][name]['version'] == expected
        for item in checkpoint['artifact_versions'] + checkpoint['evidence_refs']:
            assert version(root / item['path']) == item['version'], item['path']
            checked += 1
    gates = state['project_gates']
    assert {item['name'] for item in gates} == {'负责人终验', '任务提交', '双远程同步'}
    assert all(item['status'] == '待满足' and item['evidence_ref'] is None for item in gates)
    operation_input = task / 'evidence/review-inputs.json'
    payload = json.loads(operation_input.read_text(encoding='utf-8'))
    operation = {key: payload[key] for key in ('task_id', 'operation', 'inputs')}
    encoded = json.dumps(operation, ensure_ascii=False, sort_keys=True,
                         separators=(',', ':'), allow_nan=False).encode('utf-8')
    expected_key = 'sha256:' + hashlib.sha256(encoded).hexdigest()
    assert state['current_operation']['operation_key'] == expected_key
    reviewed = next(item for item in state['checkpoints'] if item['boundary'] == '开发验收')
    assert reviewed['operation_key'] == expected_key
    print(json.dumps({'selected': str(selected), 'state_version': state['state_version'],
                      'status': state['status'], 'current_role': state['current_role'],
                      'checked_version_refs': checked, 'boundaries': boundaries,
                      'all_project_gates_pending': True}, ensure_ascii=False))


if __name__ == '__main__':
    main()
