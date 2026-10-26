"""Unit and subprocess end-to-end coverage for the personal coding workflow."""

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness2.coding import (
    CodingAdapter, load_tasks, rollback, run_coding, safe_relative, verify,
)
from harness2.core.proposer import Proposer, ProposerError
from harness2.core.spec import ChannelSpec
from harness2.local_codex import CodexProposer, LocalCodex, run_process, write_json

FAKE_CODEX = r'''
import json
import sys
from pathlib import Path

prompt = sys.stdin.read()
root = Path.cwd()
model = sys.argv[sys.argv.index('--model') + 1]
assert '--sandbox' in sys.argv and sys.argv[sys.argv.index('--sandbox') + 1] == 'workspace-write'
assert '--dangerously-bypass-approvals-and-sandbox' not in sys.argv
assert 'PRIVATE_VERIFIER_SENTINEL' not in prompt
assert not list(root.rglob('_verify.py'))
if model == 'failed':
    print(json.dumps({'type': 'turn.failed', 'error': {'message': 'failure'}}))
    sys.exit(1)
if (root / 'context/adoption.json').exists():
    if model == 'reflection-failed':
        print(json.dumps({'type': 'turn.failed'}))
        sys.exit(1)
    text = '<experience>\ncue: Coding boundary cases\nlesson: Check boundaries before finalizing.\nevidence: The edited harness prompted an executable boundary check.\nconfidence: limited to observed runs\n</experience>'
elif list((root / 'context/candidates').glob('*.md')):
    chosen = sorted((root / 'context/candidates').glob('*.md'))[0].stem
    text = '<why>Meets the task requirements.</why>\n```choice\n' + chosen + '\n```'
elif (root / 'context/harness').exists():
    if model == 'invalid':
        config = root / 'context/harness/.codex/config.toml'
        config.parent.mkdir(exist_ok=True)
        config.write_text('sandbox_mode="danger-full-access"')
    agents = root / 'context/harness/AGENTS.md'
    if 'Check boundaries.' not in agents.read_text():
        agents.write_text(agents.read_text() + '\nCheck boundaries.\n')
    text = '<notes>Add a reusable boundary check.</notes>'
else:
    solution = root / 'solution.py'
    improved = 'Check boundaries.' in (root / 'AGENTS.md').read_text()
    holdout = '# holdout' in solution.read_text()
    good = improved and not (model == 'regression' and holdout)
    solution.write_text('def solve(x):\n    return x * 2\n' if good else 'def solve(x):\n    return x + 1\n')
    text = 'Implemented solution.py'
print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':text}}))
print(json.dumps({'type':'turn.completed','usage':{'input_tokens':10,'output_tokens':5}}))
'''


class CodingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.base = self.root / 'base'
        self.base.mkdir()
        (self.base / 'AGENTS.md').write_text('Implement the requested function.\n')
        self.binary = self.root / 'fake-codex'
        self.binary.write_text(f'#!{sys.executable}\n' + FAKE_CODEX)
        self.binary.chmod(0o755)
        self.make_task('tasks', 'double')
        self.make_task('holdout', 'other', holdout=True)
        self.output = self.root / 'output'

    def make_task(self, folder, task_id, holdout=False):
        root = self.root / folder / task_id
        (root / 'source').mkdir(parents=True)
        (root / 'source/solution.py').write_text(
            ('# holdout\n' if holdout else '') + 'def solve(x):\n    return x + 1\n'
        )
        (root / 'task.json').write_text(json.dumps({
            'id': task_id, 'prompt': 'Implement solve(x) returning twice x.',
            'outputs': ['solution.py'],
        }))
        (root / 'verify.py').write_text(
            "# PRIVATE_VERIFIER_SENTINEL\n"
            "import runpy\n"
            "solve = runpy.run_path('solution.py')['solve']\n"
            "assert solve(3) == 6\nassert solve(-1) == -2\n"
        )
        return root

    def runner(self, model='fake', max_calls=100):
        return LocalCodex(self.output, model, binary=str(self.binary),
                          max_calls=max_calls, timeout=10)

    def run_cycle(self, model='fake', k=1, mode='sequential'):
        return run_coding(load_tasks(self.root / 'tasks'), load_tasks(self.root / 'holdout'),
                          self.base, self.output, self.runner(model), k=k, mode=mode)

    def test_end_to_end_promotes_and_transfers_experience(self):
        self.make_task('tasks', 'next')
        result = self.run_cycle()
        first, second = result['tasks']
        self.assertTrue(first['accepted'])
        self.assertTrue(first['quality_gain_observed'])
        self.assertFalse(first['checks']['base1']['passed'])
        self.assertTrue(first['checks']['candidate']['passed'])
        self.assertTrue(first['checks']['holdout/other/candidate']['passed'])
        self.assertEqual(second['baseline_version'], first['candidate_version'])
        self.assertTrue(second['checks']['base1']['passed'])
        self.assertFalse(second['changed'])
        contexts = list((self.output / 'iterations/next').rglob('context/inv1/library/INDEX.md'))
        self.assertTrue(contexts)
        self.assertIn('double', contexts[0].read_text())
        for prompt in (self.output / 'sessions').glob('*/prompt.md'):
            self.assertNotIn('PRIVATE_VERIFIER_SENTINEL', prompt.read_text())
        original = self.root / 'tasks/double/source/solution.py'
        self.assertIn('return x + 1', original.read_text())
        self.assertIsNone(result['cost_usd'])
        self.assertTrue((self.output / 'versions' / first['candidate_version'] / 'AGENTS.md').exists())

    def test_parallel_recursion_and_selection(self):
        result = self.run_cycle(k=2, mode='parallel')
        self.assertTrue(result['tasks'][0]['accepted'])
        summary = next((self.output / 'iterations').rglob('summary.json'))
        data = json.loads(summary.read_text())
        self.assertEqual(data['modes']['agg']['ledger']['R'], 8)
        self.assertEqual(data['modes']['agg']['ledger']['P'], 6)

    def test_regression_retains_baseline_and_excludes_lesson(self):
        result = self.run_cycle(model='regression')
        report = result['tasks'][0]
        self.assertTrue(report['checks']['candidate']['passed'])
        self.assertFalse(report['checks']['holdout/other/candidate']['passed'])
        self.assertFalse(report['accepted'])
        self.assertEqual(result['active']['version'], report['baseline_version'])
        self.assertFalse((self.output / 'library/coding/INDEX.md').exists())
        again = self.run_cycle(model='regression')
        self.assertEqual(again['calls_started'], result['calls_started'])
        artifact = Path(next(iter(report['artifacts']))) / 'artifacts/solution.py'
        artifact.write_text('tampered')
        with self.assertRaisesRegex(ValueError, 'artifacts changed'):
            self.run_cycle(model='regression')

    def test_rollback_restores_version_and_clears_experience(self):
        result = self.run_cycle()
        baseline = result['tasks'][0]['baseline_version']
        rollback(self.output, baseline)
        self.assertEqual(json.loads((self.output / 'active.json').read_text())['version'], baseline)
        self.assertEqual(list((self.output / 'library/coding/harnesses').iterdir()), [])
        with self.assertRaises(ValueError):
            rollback(self.output, '../escape')
        (self.output / 'versions' / baseline / 'AGENTS.md').write_text('tampered')
        with self.assertRaisesRegex(ValueError, 'modified'):
            rollback(self.output, baseline)

    def test_configuration_changes_require_new_output(self):
        self.run_cycle()
        with self.assertRaisesRegex(ValueError, 'configuration changed'):
            self.run_cycle(model='other-model')
        (self.root / 'tasks/double/verify.py').write_text('raise AssertionError("new check")')
        with self.assertRaisesRegex(ValueError, 'configuration changed'):
            self.run_cycle()

    def test_interrupted_promotion_recovers_without_new_model_calls(self):
        def interrupt_activation(path, value):
            if path.name == 'active.json' and value.get('reason') == 'verified promotion':
                raise OSError('interrupted activation')
            write_json(path, value)

        with patch('harness2.coding.write_json', side_effect=interrupt_activation):
            with self.assertRaisesRegex(OSError, 'interrupted'):
                self.run_cycle()
        before = json.loads((self.output / 'calls.json').read_text())['started']
        result = self.run_cycle()
        self.assertEqual(result['calls_started'], before)
        self.assertEqual(result['active']['version'], result['tasks'][0]['candidate_version'])

    def test_rollback_is_not_undone_by_cached_promotion(self):
        result = self.run_cycle()
        baseline = result['tasks'][0]['baseline_version']
        rollback(self.output, baseline)
        resumed = self.run_cycle()
        self.assertEqual(resumed['active']['version'], baseline)
        self.assertEqual(resumed['calls_started'], result['calls_started'])

    def test_base_harness_rejects_symlinks_and_unknown_files(self):
        (self.base / 'secret.txt').write_text('not a harness component')
        with self.assertRaisesRegex(ValueError, 'outside its editable surface'):
            self.run_cycle()
        (self.base / 'secret.txt').unlink()
        (self.base / 'AGENTS.md').unlink()
        (self.base / 'AGENTS.md').symlink_to(self.root / 'tasks/double/verify.py')
        with self.assertRaisesRegex(ValueError, 'Symlinks'):
            self.run_cycle()

    def test_holdout_ids_must_be_disjoint(self):
        tasks = load_tasks(self.root / 'tasks')
        with self.assertRaisesRegex(ValueError, 'distinct'):
            run_coding(tasks, tasks, self.base, self.output, self.runner())

    def test_call_budget_persists_and_failure_never_promotes(self):
        runner = self.runner('failed', max_calls=1)
        workspace = self.root / 'work'
        workspace.mkdir()
        with self.assertRaisesRegex(RuntimeError, 'session failed'):
            runner.run('work', workspace)
        with self.assertRaisesRegex(RuntimeError, 'budget exhausted'):
            self.runner('failed', max_calls=1).run('work', workspace)
        self.assertEqual(json.loads((self.output / 'calls.json').read_text())['started'], 1)
        self.assertFalse((self.output / 'active.json').exists())

    def test_full_cycle_failure_is_loud(self):
        with self.assertRaisesRegex(RuntimeError, 'cycle failed'):
            self.run_cycle(model='failed')
        self.assertFalse((self.output / 'report.json').exists())

    def test_failed_reflection_cannot_promote(self):
        with self.assertRaisesRegex(RuntimeError, 'reflection failed'):
            self.run_cycle(model='reflection-failed')
        active = json.loads((self.output / 'active.json').read_text())
        self.assertEqual(active['reason'], 'initial baseline')

    def test_invalid_edits_cannot_silently_fall_back_to_success(self):
        with self.assertRaisesRegex(RuntimeError, 'Improvement or reflection failed'):
            self.run_cycle(model='invalid')
        self.assertFalse((self.output / 'report.json').exists())

    def test_codex_timeout_is_recorded_and_charged_to_call_limit(self):
        with patch('harness2.local_codex.run_process',
                   side_effect=subprocess.TimeoutExpired('codex', 1)):
            with self.assertRaises(subprocess.TimeoutExpired):
                self.runner().run('work', self.root)
        result = json.loads((self.output / 'sessions/0001/result.json').read_text())
        self.assertFalse(result['ok'])
        self.assertEqual(result['error'], 'timeout')
        self.assertEqual(json.loads((self.output / 'calls.json').read_text())['started'], 1)

    def test_proposer_rejects_symlink_in_harvest(self):
        context = self.root / 'context'
        (context / 'harness').mkdir(parents=True)
        (context / 'harness/AGENTS.md').write_text('Work carefully.')
        runner = self.runner()

        def create_symlink(prompt, workspace, **kwargs):
            (workspace / 'context/harness/link').symlink_to(self.base / 'AGENTS.md')
            return {'text': 'done', 'wall_s': 0}

        proposer = CodexProposer(ChannelSpec('codex_cli', 'fake'), runner, self.root / 'audit')
        with patch.object(runner, 'run', side_effect=create_symlink):
            with self.assertRaisesRegex(ProposerError, 'symlinks'):
                proposer.call('Improve', context, 'inv1', harvest='context/harness')

    def test_workspace_escape_and_task_validation(self):
        for name in ('../x', '/x', 'a/../x', '', 'a\\x'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                safe_relative(name)
        source = self.root / 'tasks/double/source'
        (source / 'link.py').symlink_to(self.base / 'AGENTS.md')
        with self.assertRaisesRegex(ValueError, 'Symlinks'):
            load_tasks(self.root / 'tasks')
        (source / 'link.py').unlink()
        (source / '.env').write_text('fixture')
        with self.assertRaisesRegex(ValueError, 'environment'):
            load_tasks(self.root / 'tasks')
        with self.assertRaisesRegex(ValueError, 'No .*task.json'):
            load_tasks(self.root / 'missing')

    def test_editable_surface_rejects_configuration_and_hooks(self):
        adapter = CodingAdapter(load_tasks(self.root / 'tasks'), self.base,
                                self.output, self.runner())
        for extra in ('.codex/config.toml', '.codex/hooks.json', 'test_policy.py'):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                adapter.validate_tree({'AGENTS.md': 'Work carefully.', extra: '{}'})
        adapter.validate_tree({'AGENTS.md': 'Work carefully.',
                               'scripts/check.py': 'print("check")\n'})

    def test_adapter_cache_checks_inputs_and_artifacts(self):
        adapter = CodingAdapter(load_tasks(self.root / 'tasks'), self.base,
                                self.output, self.runner())
        rd = adapter.execute('double', None, 'same')
        adapter.execute('double', None, 'same')
        self.assertEqual(json.loads((self.output / 'calls.json').read_text())['started'], 1)
        (rd / 'artifacts/solution.py').write_text('changed')
        with self.assertRaisesRegex(ValueError, 'changed'):
            adapter.execute('double', None, 'same')
        self.assertFalse(adapter.delivered(rd))

    def test_project_lesson_changes_invalidate_execution_cache(self):
        tasks = load_tasks(self.root / 'tasks')
        tasks['double']['prompt'] += ' Fix contradictory audio-device error messages.'
        runner = self.runner()
        adapter = CodingAdapter(tasks, self.base, self.output, runner)
        run = adapter.execute('double', None, 'same')
        recorded = json.loads((self.output / 'sessions/0001/project-lessons.json').read_text())
        self.assertEqual(recorded['selected'], runner.lessons_for(tasks['double']['prompt']))
        self.assertTrue(recorded['selected'])
        adapter.execute('double', None, 'same')
        self.assertEqual(json.loads((self.output / 'calls.json').read_text())['started'], 1)

        changed = copy.deepcopy(runner.project_lessons)
        changed[0]['lesson'] += ' Check that a later operation refreshes the snapshot.'
        with patch('harness2.local_codex.load_project_lessons', return_value=changed):
            adapter.runner = self.runner()
        with self.assertRaisesRegex(ValueError, 'Execution inputs or artifacts changed'):
            adapter.execute('double', None, 'same')
        adapter.runner = LocalCodex(self.output, 'fake', binary=str(self.binary),
                                    timeout=10, use_project_lessons=False)
        with self.assertRaisesRegex(ValueError, 'Execution inputs or artifacts changed'):
            adapter.execute('double', None, 'same')
        self.assertTrue(adapter.artifacts_intact(run))
        self.assertEqual(json.loads((self.output / 'calls.json').read_text())['started'], 1)

    def test_project_lesson_retrieval_ignores_source_and_verifier_text(self):
        tasks = load_tasks(self.root / 'tasks')
        cue = '# Fix contradictory audio-device error messages.\n'
        tasks['double']['files']['solution.py'] += cue
        tasks['double']['verifier'] += cue
        runner = self.runner()
        adapter = CodingAdapter(tasks, self.base, self.output, runner)
        adapter.execute('double', None, 'same')
        recorded = json.loads((self.output / 'sessions/0001/project-lessons.json').read_text())
        self.assertEqual(recorded, {'selected': []})
        with patch('harness2.local_codex.load_project_lessons', return_value=[]):
            adapter.runner = self.runner()
        adapter.execute('double', None, 'same')
        self.assertEqual(json.loads((self.output / 'calls.json').read_text())['started'], 1)

    def test_project_lessons_in_experiment_identity_include_holdout_public_prompts(self):
        manifest = self.root / 'holdout/other/task.json'
        task = json.loads(manifest.read_text())
        task['prompt'] += ' Fix contradictory service availability diagnostics.'
        manifest.write_text(json.dumps(task))
        result = self.run_cycle()
        self.assertTrue(result['tasks'][0]['accepted'])
        inputs = json.loads((self.output / 'experiment.json').read_text())['inputs']
        self.assertEqual(set(inputs['project_lessons']), {'other'})
        self.assertTrue(inputs['project_lessons']['other'])
        sessions = list((self.output / 'sessions').glob('*/project-lessons.json'))
        self.assertEqual(sum(bool(json.loads(path.read_text())['selected'])
                             for path in sessions), 2)
        changed = copy.deepcopy(self.runner().project_lessons)
        changed[0]['confidence'] += ' Updated review.'
        for catalog in (changed, []):
            with self.subTest(catalog=catalog):
                with patch('harness2.local_codex.load_project_lessons', return_value=catalog):
                    with self.assertRaisesRegex(ValueError, 'Experiment configuration changed'):
                        self.run_cycle()

    def test_independent_verifier_fails_and_times_out(self):
        task = load_tasks(self.root / 'tasks')['double']
        self.assertFalse(verify(task, task['source'])['passed'])
        task['verifier'] = 'import time; time.sleep(10)'
        result = verify(task, task['source'], timeout=0.05)
        self.assertEqual(result['error'], 'verification timeout')
        task['verifier'] = 'from pathlib import Path; Path(__file__).write_text("tampered")'
        with self.assertRaisesRegex(ValueError, 'modified'):
            verify(task, task['source'])

    def test_proposer_cache_and_transport_errors(self):
        context = self.root / 'context'
        (context / 'harness').mkdir(parents=True)
        (context / 'harness/AGENTS.md').write_text('Work carefully.')
        proposer = CodexProposer(ChannelSpec('codex_cli', 'fake'), self.runner(), self.root / 'audit')
        reply = proposer.call('Improve the harness', context, 'inv1', harvest='context/harness')
        self.assertIn('Check boundaries.', (reply.workspace / 'AGENTS.md').read_text())
        cached = proposer.call('Improve the harness', context, 'inv1', harvest='context/harness')
        self.assertTrue(cached.cached)
        with patch.object(proposer.runner, 'run', side_effect=RuntimeError('failed')):
            with self.assertRaises(ProposerError):
                proposer.call('New mission', context, 'inv2')
        with self.assertRaisesRegex(ValueError, 'CodexProposer'):
            Proposer(ChannelSpec('codex_cli', 'fake'))

    def test_terminal_completion_is_required(self):
        result = subprocess.CompletedProcess([], 0, json.dumps({
            'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'done'},
        }), '')
        with patch('harness2.local_codex.run_process', return_value=result):
            with self.assertRaisesRegex(RuntimeError, 'session failed'):
                self.runner().run('work', self.root)

    def test_cli_dry_run_and_full_subprocess_workflow(self):
        args = [sys.executable, '-m', 'harness2.cli.coding', 'run',
                '--tasks', str(self.root / 'tasks'), '--holdout', str(self.root / 'holdout'),
                '--base', str(self.base), '--output', str(self.output),
                '--model', 'fake', '--codex', str(self.binary), '--timeout', '10']
        dry = subprocess.run([*args, '--dry-run'], capture_output=True, text=True)
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertFalse(self.output.exists())
        full = subprocess.run(args, capture_output=True, text=True, timeout=30)
        self.assertEqual(full.returncode, 0, full.stderr)
        self.assertTrue(json.loads((self.output / 'report.json').read_text())['tasks'][0]['accepted'])

    def test_timeout_kills_process(self):
        with self.assertRaises(subprocess.TimeoutExpired):
            run_process([sys.executable, '-c', 'import time; time.sleep(10)'],
                        cwd=self.root, timeout=0.05)


if __name__ == '__main__':
    unittest.main()
