"""Unit and subprocess end-to-end coverage for the personal coding workflow."""

import copy
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from harness2.coding import (
    CodingAdapter, digest, load_tasks, rollback, run_coding, safe_relative, verify, verify_run,
)
from harness2.core import context
from harness2.core.proposer import Proposer, ProposerError
from harness2.core.spec import ChannelSpec, RunView
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
for path in (root / 'context').rglob('*'):
    if path.is_file():
        assert 'PRIVATE_VERIFIER_SENTINEL' not in path.read_text()
        assert 'FINAL_FAILURE_ONLY' not in path.read_text()
if model == 'failed':
    print(json.dumps({'type': 'turn.failed', 'error': {'message': 'failure'}}))
    sys.exit(1)
if (root / 'context/adoption.json').exists():
    if model == 'reflection-failed':
        print(json.dumps({'type': 'turn.failed'}))
        sys.exit(1)
    text = '<experience>\ncue: Coding boundary cases\nlesson: Check boundaries before finalizing.\nevidence: The edited harness prompted an executable boundary check.\nconfidence: limited to observed runs\n</experience>'
elif list((root / 'context/candidates').glob('*.md')):
    choices = sorted((root / 'context/candidates').glob('*.md'))
    if model.startswith('feedback'):
        assert 'context/development/<id>' in prompt
        choices.sort(key=lambda p: not json.loads(
            (root / 'context/development' / p.stem / 'result.json').read_text())['result']['passed'])
    chosen = choices[0].stem
    text = '<why>Meets the task requirements.</why>\n```choice\n' + chosen + '\n```'
elif (root / 'context/harness').exists():
    if model == 'invalid':
        config = root / 'context/harness/.codex/config.toml'
        config.parent.mkdir(exist_ok=True)
        config.write_text('sandbox_mode="danger-full-access"')
    agents = root / 'context/harness/AGENTS.md'
    if model.startswith('feedback'):
        evidence = sorted((root / 'context/development').iterdir())
        assert evidence, 'No public failure evidence reached refinement'
        results = {p.name: json.loads((p / 'result.json').read_text())['result'] for p in evidence}
        assert any(not r['passed'] for r in results.values())
        assert all('PUBLIC_DEVELOPMENT_SENTINEL' in (p / 'test.py').read_text() for p in evidence)
        if (root / 'context/pool/inv1').exists():
            assert 'inv1' in results and 'dom1' in results
        assert 'hypothesis' in prompt and 'executable check' in prompt
        if model == 'feedback-revision' and not (root / 'context/pool/c1').exists():
            text = '<notes>' + json.dumps({'evidence': list(results),
                'hypothesis': 'Boundary handling may be wrong', 'change': 'KEEP_BASE pending probe',
                'test': 'Run the public boundary regression'}) + '</notes>'
            print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':text}}))
            print(json.dumps({'type':'turn.completed','usage':{'input_tokens':10,'output_tokens':5}}))
            sys.exit(0)
    if 'Check boundaries.' not in agents.read_text():
        agents.write_text(agents.read_text() + '\nCheck boundaries.\n')
    text = '<notes>Add a reusable boundary check.</notes>'
    if model.startswith('feedback') and model != 'feedback-invalid-notes':
        citations = ([f'context/development/{label}' for label in results]
                     if model == 'feedback-paths' else list(results))
        text = '<notes>' + json.dumps({'evidence': citations,
            'hypothesis': 'The implementation adds one instead of doubling',
            'change': 'Require boundary checks before completion',
            'test': 'Run the public regression: solve(3) must be 6, not 4'}) + '</notes>'
else:
    assert not list(root.rglob('development.py'))
    assert 'PUBLIC_DEVELOPMENT_SENTINEL' not in prompt
    solution = root / 'solution.py'
    improved = 'Check boundaries.' in (root / 'AGENTS.md').read_text()
    holdout = '# holdout' in solution.read_text()
    good = improved and not (model in ('regression', 'feedback-regression') and holdout)
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
            "print('FINAL_FAILURE_ONLY')\n"
            "assert solve(3) == 6\nassert solve(-1) == -2\n"
        )
        return root

    def runner(self, model='fake', max_calls=100):
        return LocalCodex(self.output, model, binary=str(self.binary),
                          max_calls=max_calls, timeout=10)

    def run_cycle(self, model='fake', k=1, mode='sequential', **kwargs):
        return run_coding(load_tasks(self.root / 'tasks'), load_tasks(self.root / 'holdout'),
                          self.base, self.output, self.runner(model), k=k, mode=mode, **kwargs)

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

    def add_development_test(self):
        path = self.root / 'tasks/double/development.py'
        path.write_text(
            "# PUBLIC_DEVELOPMENT_SENTINEL\nimport runpy\n"
            "solve = runpy.run_path('solution.py')['solve']\n"
            "actual = solve(3)\n"
            "assert actual == 6, f'expected 6, observed {actual}'\n"
            "print('public regression succeeded')\n")
        return path

    def test_feedback_reaches_proposals_and_probes_before_composition(self):
        self.add_development_test()
        result = self.run_cycle(model='feedback', development_feedback=True, promotion_runs=2)
        report = result['tasks'][0]
        self.assertTrue(report['accepted'])
        self.assertEqual(report['fresh'], {'base': {'passed': 0, 'attempts': 2},
                                          'candidate': {'passed': 2, 'attempts': 2}})
        self.assertEqual(result['calls_started'], 14)
        records = {Path(p).name: r['result'] for p, r in report['development'].items()}
        self.assertFalse(records['base1']['passed'])
        self.assertIn('expected 6, observed 4', records['base1']['stderr'])
        self.assertTrue(records['inv1']['passed'])
        self.assertTrue(records['dom1']['passed'])
        self.assertTrue(records['c1']['passed'])
        comp = next((self.output / 'iterations').rglob('context/c1/development'))
        self.assertEqual({p.name for p in comp.iterdir()}, {'base1', 'inv1', 'dom1'})
        self.assertNotEqual(report['checks']['base1']['outputs'],
                            report['checks']['candidate']['outputs'])
        for path in (self.output / 'iterations').rglob('fresh/**/execution.json'):
            self.assertNotIn('development', json.loads(path.read_text()))

    def test_feedback_second_round_observes_failed_first_candidate(self):
        self.add_development_test()
        result = self.run_cycle(model='feedback-revision', k=2,
                                development_feedback=True, promotion_runs=2)
        report = result['tasks'][0]
        records = {Path(p).name: r['result'] for p, r in report['development'].items()}
        self.assertFalse(records['c1']['passed'])
        self.assertTrue(records['c2']['passed'])
        self.assertEqual(report['chosen_label'], 'c2')
        self.assertTrue(report['accepted'])
        self.assertEqual(result['calls_started'], 22)
        context_dir = next((self.output / 'iterations').rglob('context/inv2/development'))
        self.assertFalse(json.loads((context_dir / 'c1/result.json').read_text())['result']['passed'])

    def test_feedback_paths_complete_selection_reflection_and_evaluation_within_cap(self):
        self.add_development_test()
        self.make_task('holdout', 'another', holdout=True)
        private_checks = []

        def check(task, run_dir, timeout):
            if 'PRIVATE_VERIFIER_SENTINEL' in task['verifier']:
                # Reflection must finish before final evaluation, while its lesson
                # must not enter the persistent library until all gates pass.
                selection = next((self.output / 'iterations').rglob(
                    'selection/seq2/selection.json'))
                self.assertTrue(json.loads(selection.read_text())['experience']['lesson'])
                self.assertFalse((self.output / 'library/coding/INDEX.md').exists())
                self.assertEqual(json.loads((self.output / 'active.json').read_text())['reason'],
                                 'initial baseline')
                private_checks.append(run_dir)
            return verify_run(task, run_dir, timeout)

        with patch('harness2.coding.verify_run', side_effect=check):
            result = run_coding(
                load_tasks(self.root / 'tasks'), load_tasks(self.root / 'holdout'),
                self.base, self.output, self.runner('feedback-paths', max_calls=32),
                k=2, development_feedback=True, promotion_runs=5)

        report = result['tasks'][0]
        self.assertTrue(report['accepted'])
        self.assertEqual(result['calls_started'], 30)
        self.assertEqual(len(private_checks), 17)
        self.assertEqual(report['fresh'], {'base': {'passed': 0, 'attempts': 5},
                                          'candidate': {'passed': 5, 'attempts': 5}})
        summary = json.loads(next((self.output / 'iterations').rglob('summary.json')).read_text())
        stages = summary['modes']['seq']['stages']
        self.assertEqual(len(stages), 6)
        for stage in stages:
            self.assertEqual(stage['attempts'], 1)
            self.assertFalse(stage['fell_back'])
            self.assertTrue(all(ref.startswith('context/development/')
                                for ref in json.loads(stage['notes'])['evidence']))
        selected = summary['selections']['seq']
        self.assertTrue(selected['ok'])
        self.assertFalse(selected['fallback'])
        self.assertFalse(selected['trivial'])
        self.assertEqual(selected['experience'], report['experience'])
        lesson = self.output / 'library/coding/harnesses/double/EXPERIENCE.md'
        self.assertIn(report['experience']['lesson'], lesson.read_text())

    def test_feedback_parallel_mode_supplies_anonymous_selection_results(self):
        self.add_development_test()
        result = self.run_cycle(model='feedback', k=2, mode='parallel',
                                development_feedback=True, promotion_runs=1)
        self.assertTrue(result['tasks'][0]['accepted'])
        selected = next((self.output / 'iterations').rglob('selection/agg2/development'))
        self.assertEqual({p.name for p in selected.iterdir()}, {'A', 'B'})

    def test_feedback_rejects_private_regression_without_learning_its_result(self):
        self.add_development_test()
        report = self.run_cycle(model='feedback-regression', development_feedback=True,
                                promotion_runs=1)['tasks'][0]
        self.assertTrue(report['checks']['candidate']['passed'])
        self.assertFalse(report['checks']['holdout/other/candidate']['passed'])
        self.assertFalse(report['accepted'])
        self.assertFalse((self.output / 'library/coding/INDEX.md').exists())

    def test_feedback_requires_fresh_gain_not_just_development_success(self):
        self.add_development_test()
        # Both implementations satisfy this final check; only development distinguishes them.
        (self.root / 'tasks/double/verify.py').write_text(
            "# PRIVATE_VERIFIER_SENTINEL\nimport runpy\n"
            "assert runpy.run_path('solution.py')['solve'](1) == 2\n")
        report = self.run_cycle(model='feedback', development_feedback=True,
                                promotion_runs=2)['tasks'][0]
        self.assertEqual(report['fresh']['base']['passed'], 2)
        self.assertEqual(report['fresh']['candidate']['passed'], 2)
        self.assertFalse(report['accepted'])
        self.assertFalse(report['quality_gain_observed'])

    def test_feedback_rejects_inconsistent_fresh_candidate(self):
        self.add_development_test()
        runner = self.runner('feedback')
        original = runner.run
        candidate_executions = 0

        def occasionally_wrong(prompt, workspace, **kwargs):
            nonlocal candidate_executions
            result = original(prompt, workspace, **kwargs)
            if (workspace / 'solution.py').exists() and 'Check boundaries.' in (
                workspace / 'AGENTS.md').read_text():
                candidate_executions += 1
                if candidate_executions == 5:
                    (workspace / 'solution.py').write_text('def solve(x):\n    return x + 1\n')
            return result

        with patch.object(runner, 'run', side_effect=occasionally_wrong):
            result = run_coding(load_tasks(self.root / 'tasks'), load_tasks(self.root / 'holdout'),
                                self.base, self.output, runner,
                                development_feedback=True, promotion_runs=2)
        report = result['tasks'][0]
        self.assertEqual(report['fresh']['candidate'], {'passed': 1, 'attempts': 2})
        self.assertFalse(report['accepted'])
        self.assertFalse(report['quality_gain_observed'])

    def test_final_timeout_is_not_counted_as_a_baseline_behavior_failure(self):
        self.add_development_test()

        def final_timeout(task, run_dir, timeout):
            if 'PRIVATE_VERIFIER_SENTINEL' in task['verifier']:
                return {'passed': False, 'error': 'verification timeout'}
            return verify_run(task, run_dir, timeout)

        with patch('harness2.coding.verify_run', side_effect=final_timeout):
            with self.assertRaisesRegex(RuntimeError, 'Final check base1 could not finish'):
                self.run_cycle(model='feedback', development_feedback=True, promotion_runs=1)
        self.assertFalse((self.output / 'report.json').exists())
        self.assertEqual(json.loads((self.output / 'active.json').read_text())['reason'],
                         'initial baseline')
        raw = next((self.output / 'iterations').rglob('verification.json'))
        self.assertEqual(json.loads(raw.read_text())['error'], 'verification timeout')

    def test_feedback_invalid_proposal_notes_cannot_promote(self):
        self.add_development_test()
        with self.assertRaisesRegex(RuntimeError, 'Improvement or reflection failed'):
            self.run_cycle(model='feedback-invalid-notes', development_feedback=True,
                           promotion_runs=1)
        self.assertFalse((self.output / 'report.json').exists())

    def test_feedback_reserves_final_sessions_even_when_refinement_exhausts_budget(self):
        self.add_development_test()
        runner = self.runner('feedback', max_calls=13)
        with self.assertRaisesRegex(RuntimeError, 'reflection failed'):
            run_coding(load_tasks(self.root / 'tasks'), load_tasks(self.root / 'holdout'),
                       self.base, self.output, runner,
                       development_feedback=True, promotion_runs=2)
        # Four fresh executions and two holdouts remain reserved; reflection needs slot eight.
        self.assertEqual(json.loads((self.output / 'calls.json').read_text())['started'], 7)
        self.assertEqual(runner.max_calls, 13)
        self.assertFalse((self.output / 'report.json').exists())

    def development_adapter(self):
        self.add_development_test()
        return CodingAdapter(load_tasks(self.root / 'tasks'), self.base, self.output,
                             self.runner(), development_feedback=True)

    def test_development_evidence_integrity_and_final_isolation(self):
        adapter = self.development_adapter()
        run = adapter.execute('double', None, 'base1')
        (run / 'verification.json').write_text('FINAL_FAILURE_ONLY')
        built = self.evidence_context(adapter, run)
        self.assertEqual(built.development, 1)
        self.assertIn('public regression', built.layout)
        for p in built.root.rglob('*'):
            if p.is_file():
                self.assertNotIn('FINAL_FAILURE_ONLY', p.read_text())
                self.assertNotIn('PRIVATE_VERIFIER_SENTINEL', p.read_text())
        with patch('harness2.coding.verify_run', side_effect=AssertionError('unexpected rerun')):
            adapter.execute('double', None, 'base1')
            adapter.development_evidence(run)
        record_path = run / 'development.json'
        record = json.loads(record_path.read_text())
        record['result']['passed'] = True
        write_json(record_path, record)
        with self.assertRaisesRegex(ValueError, 'Development evidence changed'):
            adapter.development_evidence(run)

    def test_development_timeout_stays_loud_and_is_not_retried(self):
        adapter = self.development_adapter()
        with patch('harness2.coding.verify_run', return_value={
            'passed': False, 'error': 'verification timeout'
        }) as verifier:
            for _ in range(2):
                with self.assertRaisesRegex(RuntimeError, 'could not finish'):
                    adapter.execute('double', None, 'base1')
            self.assertEqual(verifier.call_count, 1)
        self.assertEqual(json.loads((self.output / 'calls.json').read_text())['started'], 1)

    def test_unbound_development_record_is_recomputed_without_model_retry(self):
        adapter = self.development_adapter()
        run = adapter.execute('double', None, 'base1')
        marker = run / 'execution.json'
        execution = json.loads(marker.read_text())
        execution.pop('development')
        write_json(marker, execution)
        with patch('harness2.coding.verify_run', wraps=verify_run) as verifier:
            adapter.execute('double', None, 'base1')
            self.assertEqual(verifier.call_count, 1)
        self.assertEqual(json.loads((self.output / 'calls.json').read_text())['started'], 1)

    def test_development_evidence_rejects_escaping_paths(self):
        adapter = self.development_adapter()
        run = adapter.execute('double', None, 'base1')
        with self.assertRaisesRegex(ValueError, 'label'):
            context.write_development(self.root / 'context', '../escape', adapter, run)
        with patch.object(adapter, 'development_evidence', return_value={'../escape': 'text'}):
            with self.assertRaisesRegex(ValueError, 'contained paths'):
                context.write_development(self.root / 'context', 'base1', adapter, run)

    def test_public_example_detects_failure_and_accepts_correction(self):
        examples = Path(__file__).resolve().parents[1] / 'examples/coding/tasks'
        task = load_tasks(examples)['signed_total']
        public_task = {**task, 'verifier': task['development_verifier']}
        result = verify(public_task, task['source'])
        self.assertFalse(result['passed'])
        self.assertIn('expected 10.25, got 12.50', result['stderr'])
        artifacts = self.root / 'example-artifacts'
        artifacts.mkdir()
        (artifacts / 'solution.py').write_text(
            "from decimal import Decimal\ndef total_amounts(rows):\n"
            "    return sum((Decimal(row['amount']) for row in rows), Decimal(0))\n")
        self.assertTrue(verify(public_task, artifacts)['passed'])

    def test_development_input_and_provenance_changes_are_rejected(self):
        adapter = self.development_adapter()
        run = adapter.execute('double', None, 'base1')
        adapter.items['double']['development_verifier'] += '\n# changed test\n'
        with self.assertRaisesRegex(ValueError, 'Development evidence changed'):
            adapter.execute('double', None, 'base1')
        adapter.items['double']['files']['solution.py'] += '\n# changed source\n'
        with self.assertRaisesRegex(ValueError, 'provenance changed'):
            adapter.development_evidence(run)

    def test_development_requires_separate_tests_and_keeps_verdict_guard(self):
        with self.assertRaisesRegex(ValueError, 'needs development.py'):
            self.run_cycle(development_feedback=True)
        path = self.add_development_test()
        path.write_text((path.parent / 'verify.py').read_text())
        with self.assertRaisesRegex(ValueError, 'must be separate'):
            self.run_cycle(development_feedback=True)
        path.unlink()
        path.symlink_to(path.parent / 'verify.py')
        with self.assertRaisesRegex(ValueError, 'must not be a symlink'):
            load_tasks(self.root / 'tasks')
        path.unlink()
        adapter = self.development_adapter()
        adapter.items['double']['development_verifier'] += '\nprint(\'{"pass_rate": 1}\')\n'
        run = adapter.execute('double', None, 'base1')
        with self.assertRaisesRegex(ValueError, 'grader verdict'):
            self.evidence_context(adapter, run)

    def test_development_notes_require_testable_explanation_and_real_evidence(self):
        adapter = self.development_adapter()
        run = adapter.execute('double', None, 'base1')
        built = self.evidence_context(adapter, run)
        valid = {'evidence': ['base1'], 'hypothesis': 'Incorrect arithmetic',
                 'change': 'Check boundaries', 'test': 'Run solve(3) and require 6'}
        adapter.validate_proposal_notes(json.dumps(valid), built.root)
        for invalid in ('plain prose', '[]', json.dumps({**valid, 'test': ''}),
                        json.dumps({**valid, 'evidence': ['unseen']})):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                adapter.validate_proposal_notes(invalid, built.root)

    def test_development_notes_accept_exact_evidence_paths_only(self):
        adapter = self.development_adapter()
        run = adapter.execute('double', None, 'base1')
        built = self.evidence_context(adapter, run)
        (built.root / 'development/stray.txt').write_text('Not a rollout')
        notes = {'hypothesis': 'Incorrect arithmetic', 'change': 'Check boundaries',
                 'test': 'Run solve(3) and require 6'}
        for refs in (['base1'], ['context/development/base1'],
                     ['base1', 'context/development/base1']):
            with self.subTest(refs=refs):
                adapter.validate_proposal_notes(json.dumps({**notes, 'evidence': refs}), built.root)
        for refs in ([], 'base1', [None], ['context/development/unseen'],
                     ['base1', 'context/development/unseen'], ['stray.txt'],
                     ['context/development/stray.txt'], ['context/pool/base1'],
                     ['/context/development/base1'], ['context/development/../base1'],
                     ['context/development/base1/result.json'], ['development/base1']):
            with self.subTest(refs=refs), self.assertRaisesRegex(ValueError, 'evidence'):
                adapter.validate_proposal_notes(json.dumps({**notes, 'evidence': refs}), built.root)

    def test_feedback_resume_preserves_evidence_and_budget(self):
        self.add_development_test()
        result = self.run_cycle(model='feedback-regression', development_feedback=True,
                                promotion_runs=1)
        again = self.run_cycle(model='feedback-regression', development_feedback=True,
                               promotion_runs=1)
        self.assertEqual(again['calls_started'], result['calls_started'])
        with self.assertRaisesRegex(ValueError, 'configuration changed'):
            self.run_cycle(model='feedback-regression', development_feedback=True,
                           promotion_runs=2)
        marker = next((self.output / 'iterations').rglob('executions/*/double/inv1/development.json'))
        marker.write_text('{}')
        with self.assertRaises((ValueError, KeyError)):
            self.run_cycle(model='feedback-regression', development_feedback=True,
                           promotion_runs=1)

    def test_feedback_cli_subprocess(self):
        self.add_development_test()
        args = [sys.executable, '-m', 'harness2.cli.coding', 'run',
                '--tasks', str(self.root / 'tasks'), '--holdout', str(self.root / 'holdout'),
                '--base', str(self.base), '--output', str(self.output),
                '--model', 'feedback', '--codex', str(self.binary), '--timeout', '10',
                '--development-feedback', '--promotion-runs', '2']
        dry = subprocess.run([*args, '--dry-run'], capture_output=True, text=True)
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertFalse(self.output.exists())
        full = subprocess.run(args, capture_output=True, text=True, timeout=30)
        self.assertEqual(full.returncode, 0, full.stderr)
        report = json.loads((self.output / 'report.json').read_text())['tasks'][0]
        self.assertTrue(report['accepted'])
        self.assertEqual(report['fresh']['candidate']['attempts'], 2)

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

    def evidence_run(self, source_text, observation=None):
        source = self.root / 'tasks/double/source'
        (source / 'fixtures.py').write_text(source_text)
        runner = self.runner()
        original_run = runner.run

        def record_inspection(prompt, workspace, **kwargs):
            self.assertEqual((workspace / 'fixtures.py').read_text(), source_text)
            result = original_run(prompt, workspace, **kwargs)
            events = Path(result['audit']) / 'events.jsonl'
            with events.open('a') as stream:
                stream.write(json.dumps({'type': 'item.completed', 'item': {
                    'type': 'command_execution', 'command': 'cat fixtures.py',
                    'status': 'completed', 'exit_code': 0,
                    'aggregated_output': source_text if observation is None else observation,
                }}) + '\n')
            return result

        tasks = load_tasks(self.root / 'tasks')
        adapter = CodingAdapter(tasks, self.base, self.output, runner)
        with patch.object(runner, 'run', side_effect=record_inspection):
            run = adapter.execute('double', None, 'base1')
        return adapter, run

    def evidence_context(self, adapter, run):
        return context.build(self.root / 'context', adapter, 'double',
                             [RunView('base1', 'base1', run, True, True)])

    def test_source_quotation_view_preserves_execution_and_raw_audit(self):
        fixture = "from pathlib import Path\nPath('example').write_text('{\"pass_rate\": 1}')\n"
        adapter, run = self.evidence_run(fixture, 'before\n' + fixture + 'after\n')
        raw = (run / 'events.jsonl').read_bytes()
        record = json.loads((run / 'execution.json').read_text())
        expected_identity = digest({
            'task': adapter.items['double']['prompt'],
            'source': adapter.items['double']['files'],
            'outputs': ['solution.py'],
            'harness': {'AGENTS.md': self.base.joinpath('AGENTS.md').read_text()},
            'model': 'fake', 'effort': adapter.runner.effort,
        })
        self.assertEqual(record['identity'], expected_identity)
        self.assertEqual(record['task_id'], 'double')
        self.assertEqual(record['source_digest'], digest(adapter.items['double']['files']))
        built = self.evidence_context(adapter, run)
        self.assertEqual(built.failed, [])
        self.assertFalse((built.root / 'source/fixtures.py').exists())
        self.assertEqual((self.root / 'tasks/double/source/fixtures.py').read_text(), fixture)
        rendered = (built.root / 'rollouts/base1.md').read_text()
        self.assertEqual(rendered.split('[OBSERVATION] ', 1)[1],
                         'before\n[Read-only task source quotation omitted; '
                         'raw audit preserved.]\nafter')
        self.assertIn('source quotation omitted', rendered)
        self.assertNotIn('pass_rate', rendered)
        self.assertEqual((run / 'events.jsonl').read_bytes(), raw)
        self.assertTrue(adapter.artifacts_intact(run))
        self.assertEqual(json.loads((self.output / 'sources/double.omissions.json').read_text()),
                         ['fixtures.py'])

    def test_evidence_view_never_authorizes_verdict_data_or_other_task_source(self):
        fixture = "from pathlib import Path\nPath('example').write_text('{\"pass_rate\": 1}')\n"
        cases = [
            ('bare data', '{"pass_rate": 1}\n', None),
            ('mixed with verdict', fixture, fixture + '{"pass_rate": 1}\n'),
            ('partial quote', fixture, fixture.splitlines()[1]),
        ]
        for label, source, observation in cases:
            with self.subTest(label=label):
                adapter, run = self.evidence_run(source, observation)
                with self.assertRaisesRegex(ValueError, 'grader verdict'):
                    self.evidence_context(adapter, run)
                # Every subcase gets its own authenticated cache.
                shutil.rmtree(self.output)
        adapter, run = self.evidence_run(fixture)
        self.make_task('tasks', 'different')
        other = load_tasks(self.root / 'tasks')['different']
        other['files']['fixtures.py'] = fixture
        adapter.items['different'] = other
        adapter.items['double']['files']['fixtures.py'] = '# changed source\n'
        with self.assertRaisesRegex(ValueError, 'grader verdict'):
            self.evidence_context(adapter, run)

    def test_parallel_source_views_preserve_omission_manifest(self):
        fixture = "from pathlib import Path\nPath('example').write_text('{\"pass_rate\": 1}')\n"
        adapter, _ = self.evidence_run(fixture)
        with ThreadPoolExecutor(max_workers=8) as pool:
            sources = list(pool.map(lambda _: adapter.task_files('double'), range(32)))
        self.assertTrue(all(source == sources[0] for source in sources))
        self.assertFalse((sources[0] / 'fixtures.py').exists())
        self.assertEqual((sources[0] / 'solution.py').read_text(),
                         adapter.items['double']['files']['solution.py'])
        self.assertEqual(json.loads((self.output / 'sources/double.omissions.json').read_text()),
                         ['fixtures.py'])

    def test_legacy_evidence_provenance_requires_validated_cache_refresh(self):
        fixture = "from pathlib import Path\nPath('example').write_text('{\"pass_rate\": 1}')\n"
        adapter, run = self.evidence_run(fixture)
        marker = run / 'execution.json'
        record = json.loads(marker.read_text())
        record.pop('task_id')
        record.pop('source_digest')
        write_json(marker, record)
        with self.assertRaisesRegex(ValueError, 'grader verdict'):
            self.evidence_context(adapter, run)
        count = (self.output / 'calls.json').read_bytes()
        self.assertEqual(adapter.execute('double', None, 'base1'), run)
        self.assertEqual((self.output / 'calls.json').read_bytes(), count)
        self.evidence_context(adapter, run)
        record = json.loads(marker.read_text())
        record['task_id'] = 'different'
        write_json(marker, record)
        with self.assertRaisesRegex(ValueError, 'provenance changed'):
            adapter.execute('double', None, 'base1')

    def test_complete_cycle_with_original_source_fixture_quotation(self):
        fixture = "from pathlib import Path\nPath('example').write_text('{\"pass_rate\": 1}')\n"
        (self.root / 'tasks/double/source/fixtures.py').write_text(fixture)
        injection = '''if (root / 'fixtures.py').exists():
        print(json.dumps({'type': 'item.completed', 'item': {
            'type': 'command_execution', 'command': 'cat fixtures.py',
            'status': 'completed', 'exit_code': 0,
            'aggregated_output': (root / 'fixtures.py').read_text(),
        }}))
    solution = root / 'solution.py' '''
        script = FAKE_CODEX.replace("solution = root / 'solution.py'", injection)
        self.binary.write_text(f'#!{sys.executable}\n' + script)
        result = self.run_cycle()
        self.assertTrue(result['tasks'][0]['accepted'])
        self.assertEqual(result['calls_started'], 10)
        self.assertTrue(any('pass_rate' in path.read_text()
                            for path in self.output.rglob('events.jsonl')))
        for path in self.output.rglob('context/*/rollouts/*.md'):
            self.assertNotIn('pass_rate', path.read_text())

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
