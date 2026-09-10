"""Read-only audit of explicitly authorized historical TB2 trace inputs.

Writes only a manifest beneath the new project when --output is supplied.
Never imports old implementation code or sends inference requests.
"""
import argparse
from collections import Counter
from pathlib import Path
import datetime
import hashlib
import json


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    base = Path('<OPENCLAW_ROOT>/docs/plan/summary-spine/terminal-bench-results/raw')
    rows = []
    for arm in ['baseline', 'spineB']:
        root = base / ('dsv4-v1d-core12-paired-r1-20260904-' + arm)
        for result_path in sorted(root.glob('*/*/result.json')):
            trial = result_path.parent
            result = json.loads(result_path.read_text())
            config = json.loads((trial / 'config.json').read_text())
            row = {'arm': arm, 'instance_id': result['task_name'], 'trial_path': str(trial),
                   'task_checksum': result.get('task_checksum'), 'finished_at': result.get('finished_at'),
                   'reward': (result.get('verifier_result') or {}).get('rewards', {}).get('reward'),
                   'exception_type': (result.get('exception_info') or {}).get('exception_type'),
                   'model': config.get('agent', {}).get('model_name'),
                   'agent_class': config.get('agent', {}).get('name'),
                   'agent_kwargs': {k: v for k, v in config.get('agent', {}).get('kwargs', {}).items()
                                    if k.startswith('summary_spine') or k in ['context_tokens', 'thinking', 'reasoning_effort']},
                   'artifacts': {}, 'audit_errors': []}
            for relative in ['agent/instruction.txt', 'agent/openclaw.session.jsonl', 'agent/trajectory.json',
                             'agent/openclaw.txt', 'result.json', 'config.json', 'lock.json',
                             'verifier/reward.txt', 'verifier/ctrf.json']:
                path = trial / relative
                if path.is_file():
                    row['artifacts'][relative] = {'path': str(path), 'bytes': path.stat().st_size, 'sha256': digest(path)}
            session = trial / 'agent/openclaw.session.jsonl'
            atif_path = trial / 'agent/trajectory.json'
            if not session.is_file():
                row['eligibility'] = 'no_agent_trace'
                rows.append(row)
                continue
            entries = []
            for number, line in enumerate(session.read_text().splitlines(), 1):
                if line.strip():
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        row['audit_errors'].append(f'invalid_jsonl_line:{number}')
            ids = [e['id'] for e in entries if 'id' in e]
            if len(set(ids)) != len(ids):
                row['audit_errors'].append('duplicate_session_entry_ids')
            entry_ids = set(ids)
            missing_parents = [e['parentId'] for e in entries if e.get('parentId') and e['parentId'] not in entry_ids]
            if missing_parents:
                row['audit_errors'].append('missing_parent_entries')
            messages = [e['message'] for e in entries if isinstance(e.get('message'), dict)]
            assistants = [m for m in messages if m.get('role') == 'assistant']
            tools = [m for m in messages if m.get('role') == 'toolResult']
            calls = [c for m in assistants for c in m.get('content', []) if isinstance(c, dict) and c.get('type') == 'toolCall']
            call_ids = Counter(c.get('id') for c in calls)
            result_ids = Counter(m.get('toolCallId') for m in tools)
            unmatched_calls = sorted(str(x) for x in call_ids.keys() - result_ids.keys())
            unmatched_results = sorted(str(x) for x in result_ids.keys() - call_ids.keys())
            if unmatched_calls or unmatched_results:
                row['audit_errors'].append('unmatched_tool_association')
            blocks = [c for m in messages for c in m.get('content', []) if isinstance(c, dict)]
            thinking_chars = sum(len(c.get('thinking', '')) for c in blocks if c.get('type') == 'thinking')
            assistant_text_chars = sum(len(c.get('text', '')) for m in assistants for c in m.get('content', [])
                                       if isinstance(c, dict) and c.get('type') == 'text')
            row['session'] = {'entries': len(entries), 'messages': len(messages), 'assistant_messages': len(assistants),
                              'tool_calls': len(calls), 'tool_results': len(tools), 'tool_names': dict(Counter(c.get('name') for c in calls)),
                              'thinking_chars': thinking_chars, 'assistant_text_chars': assistant_text_chars,
                              'content_types': dict(Counter(c.get('type') for c in blocks)),
                              'compaction_entries': sum(e.get('type') == 'compaction' for e in entries),
                              'stop_reasons': dict(Counter(m.get('stopReason') for m in assistants)),
                              'last_assistant_stop_reason': assistants[-1].get('stopReason') if assistants else None,
                              'unmatched_call_ids': unmatched_calls, 'unmatched_result_ids': unmatched_results,
                              'missing_parent_ids': missing_parents}
            if atif_path.is_file():
                atif = json.loads(atif_path.read_text())
                steps = atif.get('steps', [])
                atif_calls = [c for step in steps for c in step.get('tool_calls', [])]
                row['atif'] = {'schema_version': atif.get('schema_version'), 'agent': atif.get('agent'),
                               'steps': len(steps), 'agent_steps': sum(s.get('source') == 'agent' for s in steps),
                               'tool_calls': len(atif_calls), 'observations': sum(len(s.get('observation', {}).get('results', [])) for s in steps),
                               'placeholder_agent_messages': sum(s.get('source') == 'agent' and s.get('message') == '(no assistant text)' for s in steps),
                               'source_call_ids_match_session': Counter(c.get('tool_call_id') for c in atif_calls) == call_ids}
                if not row['atif']['source_call_ids_match_session']:
                    row['audit_errors'].append('atif_session_call_ids_differ')
            reward_path = trial / 'verifier/reward.txt'
            if reward_path.is_file():
                row['reward_file_matches_result'] = float(reward_path.read_text()) == row['reward']
                if not row['reward_file_matches_result']:
                    row['audit_errors'].append('reward_mismatch')
            if arm == 'spineB':
                spine = trial / 'agent/summary-spine'
                files = sorted(p for p in spine.rglob('*') if p.is_file()) if spine.is_dir() else []
                row['spine_artifacts'] = {'files': len(files), 'bytes': sum(p.stat().st_size for p in files),
                                          'inventory': [{'path': str(p), 'bytes': p.stat().st_size, 'sha256': digest(p)} for p in files]}
                row['spine_non_raw_files'] = [str(p.relative_to(trial)) for p in files if '-raw-store/' not in str(p)][:40]
            row['eligibility'] = 'structurally_usable_raw_session' if not row['audit_errors'] and assistants and row['reward'] is not None else 'needs_review'
            rows.append(row)
    paired = []
    for task in sorted(set(r['instance_id'] for r in rows)):
        matches = {r['arm']: r for r in rows if r['instance_id'] == task}
        if len(matches) == 2:
            a, b = matches['baseline'], matches['spineB']
            paired.append({'instance_id': task, 'same_task_checksum': a['task_checksum'] == b['task_checksum'],
                           'same_instruction_hash': (a['artifacts']['agent/instruction.txt']['sha256'] == b['artifacts']['agent/instruction.txt']['sha256'])
                           if 'agent/instruction.txt' in a['artifacts'] and 'agent/instruction.txt' in b['artifacts'] else None,
                           'baseline_eligibility': a['eligibility'], 'spine_eligibility': b['eligibility'],
                           'baseline_reward': a['reward'], 'spine_reward': b['reward']})
    report = {'schema_version': 1, 'audited_at_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
              'scope': 'Read-only structural audit of authorized prior traces. No inference or benchmark rerun.',
              'limitations': ['Structural integrity does not prove every original tool output was untruncated.',
                             'No exact tokenization of manager extraction inputs performed.',
                             'Spine-session trajectory and compressed Spine model-visible representation are different inputs.',
                             'Paired executions may have different actions and outcomes; they do not isolate compression.'],
              'rows': rows, 'pairs': paired}
    if args.output:
        allowed = Path('<PROJECT_ROOT>').resolve()
        assert args.output.resolve().is_relative_to(allowed)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    for row in rows:
        print(json.dumps({k: row.get(k) for k in ['arm', 'instance_id', 'eligibility', 'reward', 'exception_type', 'audit_errors', 'session', 'atif']}, ensure_ascii=False))
    print('PAIRS', json.dumps(paired, ensure_ascii=False))


if __name__ == '__main__':
    main()
