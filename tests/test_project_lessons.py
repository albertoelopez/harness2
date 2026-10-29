"""Project lesson retrieval and real subprocess delivery, without paid model calls."""

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness2.core.context import assert_tree_has_no_grader_verdicts
from harness2.core.spec import ChannelSpec
from harness2.local_codex import CodexProposer, LocalCodex
from harness2.project_lessons import (
    lesson_context, load_project_lessons, relevant_project_lessons,
)


class ProjectLessonTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.lessons = load_project_lessons()
        self.audio = next(lesson for lesson in self.lessons
                          if lesson['id'] == 'consistent-external-state-snapshot')
        self.vision = next(lesson for lesson in self.lessons
                           if lesson['id'] == 'early-optional-capability-check')
        self.cleanup = next(lesson for lesson in self.lessons
                            if lesson['id'] == 'operation-scoped-output-cleanup')
        self.retention = next(lesson for lesson in self.lessons
                              if lesson['id'] == 'retained-artifact-ownership')
        self.binary = self.root / "fake-codex"
        self.binary.write_text(f"#!{sys.executable}\n" + '''
import json
import sys
from pathlib import Path
prompt = sys.stdin.read()
Path('received-prompt.md').write_text(prompt)
text = 'lesson received' if '# Relevant project lessons' in prompt else 'no lesson'
print(json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': text}}))
print(json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 1, 'output_tokens': 1}}))
''')
        self.binary.chmod(0o755)

    def runner(self, **kwargs):
        return LocalCodex(self.root / 'audit', 'fixture', binary=str(self.binary),
                          timeout=10, **kwargs)

    def test_catalog_has_public_evidence_and_limited_claim(self):
        self.assertEqual({lesson['id'] for lesson in self.lessons},
                         {'consistent-external-state-snapshot', 'early-optional-capability-check',
                          'operation-scoped-output-cleanup', 'retained-artifact-ownership'})
        lesson = self.audio
        self.assertEqual(lesson['status'], 'verified_project_lesson')
        self.assertIn('f52a349d08eaa10bafcde44b38c5f0b5038ee298', lesson['evidence'])
        self.assertIn('test_audio_device_no_devices_reuses_snapshot', lesson['evidence'])
        self.assertIn('has not been measured', lesson['confidence'])
        self.assertEqual(self.vision['status'], 'verified_project_lesson')
        self.assertIn('b9aa46cd227447db679d79a8613ff8234f3eb548', self.vision['evidence'])
        self.assertIn('test_describe_preflight_and_outputs', self.vision['evidence'])
        self.assertIn('has not been measured', self.vision['confidence'])
        self.assertIn('without a lesson-disabled control', self.vision['confidence'])
        self.assertEqual(self.cleanup['status'], 'verified_project_lesson')
        self.assertIn('8ba7d81c1ea6691f3c7287754e92d361a270644b', self.cleanup['evidence'])
        self.assertIn('test_cleanup_is_exact_and_idempotent', self.cleanup['evidence'])
        self.assertIn('no observed reliability gain', self.cleanup['confidence'])
        self.assertIn('did not run a lesson-enabled comparison', self.cleanup['confidence'])
        self.assertIn('baseline fixed the issue on its first attempt', self.cleanup['confidence'])
        self.assertIn('82992e1a67d37aa345fc328f906a474f28032002', self.retention['evidence'])
        self.assertIn('test_reconnect_keeps_earlier_images', self.retention['evidence'])
        self.assertIn('has not undergone a transfer comparison', self.retention['confidence'])
        assert_tree_has_no_grader_verdicts({'project-lesson.md': lesson_context(self.lessons)})

    def test_retention_lesson_matches_reconnect_and_ownership_tasks(self):
        for prompt in (
            'Prevent live frames from being overwritten on reconnect.',
            'Fix FRAME_CAPTURE cleanup on early close.',
            'Preserve retained artifacts across producer restarts.',
            'Clarify ownership of yielded files when a consumer fails.',
            'Keep captured images intact when reconnecting.',
            'Do not overwrite published artifacts.',
        ):
            with self.subTest(prompt=prompt):
                self.assertEqual(relevant_project_lessons(prompt, self.lessons), [self.retention])

    def test_retention_lesson_ignores_unrelated_and_partial_cues(self):
        for prompt in (
            'Improve frame capture quality.', 'Reconnect the database client.',
            'Clean up variable names.', 'Document retained artifacts.',
            'Remove a live framework during cleanup.',
            'Fix frame capture restarters.',
        ):
            with self.subTest(prompt=prompt):
                self.assertEqual(relevant_project_lessons(prompt, self.lessons), [])

    def test_subprocess_receives_retention_lesson_with_exact_audit(self):
        result = self.runner().run('Preserve live frames across reconnects.', self.root)
        self.assertEqual(result['text'], 'lesson received')
        audit = Path(result['audit'])
        delivered = (self.root / 'received-prompt.md').read_text()
        self.assertEqual(delivered, (audit / 'prompt.md').read_text())
        self.assertIn(self.retention['lesson'], delivered)
        for lesson in (self.audio, self.vision, self.cleanup):
            self.assertNotIn(lesson['lesson'], delivered)
        self.assertEqual(json.loads((audit / 'project-lessons.json').read_text()),
                         {'selected': [self.retention]})
        self.assertEqual(result['project_lessons'], [self.retention['id']])

    def test_retention_lesson_obeys_task_scope_and_control_setting(self):
        prompt = 'Background: live frames reconnect. Improve caption formatting.'
        result = self.runner().run(prompt, self.root, task_prompt='Improve caption formatting.')
        self.assertEqual(result['project_lessons'], [])
        self.assertEqual((self.root / 'received-prompt.md').read_text(), prompt)
        prompt = 'Fix retained files cleanup.'
        result = self.runner(use_project_lessons=False).run(prompt, self.root)
        self.assertEqual(result['project_lessons'], [])
        self.assertEqual((self.root / 'received-prompt.md').read_text(), prompt)

    def test_retention_and_cleanup_lessons_are_delivered_once_when_both_match(self):
        result = self.runner().run(
            'Preserve retained files during output directory cleanup after reconnect. '
            'Check retained files ownership.', self.root)
        expected = [self.retention, self.cleanup]
        self.assertEqual(result['project_lessons'], [lesson['id'] for lesson in expected])
        audit = Path(result['audit'])
        self.assertEqual(json.loads((audit / 'project-lessons.json').read_text()),
                         {'selected': expected})
        delivered = (self.root / 'received-prompt.md').read_text()
        for lesson in expected:
            self.assertEqual(delivered.count(lesson['lesson']), 1)

    def test_relevant_cues_cover_original_and_transfer_tasks(self):
        for prompt in (
            'Fix contradictory audio-device error messages.',
            'Service availability checks twice before showing setup guidance.',
            'Device discovery reports inconsistent state.',
            'External_state diagnostics must match the decision.',
        ):
            with self.subTest(prompt=prompt):
                self.assertEqual(relevant_project_lessons(prompt, self.lessons), [self.audio])

    def test_vision_lesson_matches_original_and_transfer_tasks(self):
        for prompt in (
            'Check the installed vision model before batch processing.',
            'Warn when the selected Ollama model is missing.',
            'Validate model_inventory during preflight.',
            'Preserve fallback output when an optional capability is unavailable.',
            'Check optional dependencies and report missing prerequisites.',
        ):
            with self.subTest(prompt=prompt):
                self.assertEqual(relevant_project_lessons(prompt, self.lessons), [self.vision])

    def test_vision_lesson_ignores_unrelated_and_partial_cues(self):
        for prompt in (
            'Improve vision model caption quality.',
            'Document the Ollama command line.',
            'Fix a missing transcript heading.',
            'Check installed audio devices.',
            'Add a fallback font.',
            'Explain optional dependencies.',
            'Evaluate unavailable vision modelers.',
        ):
            with self.subTest(prompt=prompt):
                self.assertEqual(relevant_project_lessons(prompt, self.lessons), [])

    def test_both_lessons_can_match_without_duplicate_delivery(self):
        prompt = ('Check vision model availability before processing; keep service availability '
                  'diagnostics consistent. Repeat the vision model availability check next run.')
        self.assertEqual(relevant_project_lessons(prompt, self.lessons), [self.audio, self.vision])

    def test_cleanup_lesson_matches_original_and_transfer_tasks(self):
        for prompt in (
            'Fix stale translated transcripts when reusing an output directory.',
            'Clean up translation files from the prior run.',
            'Handle reused output directories in the report exporter.',
            'Keep incremental output intact during cleanup.',
            'Remove stale generated artifacts for the requested variant.',
            'Fix OUTPUT_LIFECYCLE cleanup on reruns.',
        ):
            with self.subTest(prompt=prompt):
                self.assertEqual(relevant_project_lessons(prompt, self.lessons), [self.cleanup])

    def test_cleanup_lesson_ignores_unrelated_and_partial_cues(self):
        for prompt in (
            'Improve translation quality.', 'Render translated transcripts.',
            'Create an output directory.', 'Clean up variable names.',
            'Fix a stale login session.', 'Reuse the loaded Whisper model.',
            'Remove stale generated filenames from a documentation sentence.',
        ):
            with self.subTest(prompt=prompt):
                self.assertEqual(relevant_project_lessons(prompt, self.lessons), [])

    def test_subprocess_receives_cleanup_lesson_with_exact_audit(self):
        result = self.runner().run('Fix stale translation files in reused output directories.',
                                   self.root)
        self.assertEqual(result['text'], 'lesson received')
        audit = Path(result['audit'])
        delivered = (self.root / 'received-prompt.md').read_text()
        self.assertEqual(delivered, (audit / 'prompt.md').read_text())
        self.assertIn(self.cleanup['lesson'], delivered)
        self.assertNotIn(self.audio['lesson'], delivered)
        self.assertNotIn(self.vision['lesson'], delivered)
        self.assertEqual(json.loads((audit / 'project-lessons.json').read_text()),
                         {'selected': [self.cleanup]})
        self.assertEqual(result['project_lessons'], [self.cleanup['id']])

    def test_cleanup_lesson_obeys_scope_and_control_setting(self):
        prompt = 'Background: stale translation files. Improve caption formatting.'
        result = self.runner().run(prompt, self.root, task_prompt='Improve caption formatting.')
        self.assertEqual(result['project_lessons'], [])
        self.assertEqual((self.root / 'received-prompt.md').read_text(), prompt)
        prompt = 'Clean up stale translation files.'
        result = self.runner(use_project_lessons=False).run(prompt, self.root)
        self.assertEqual(result['project_lessons'], [])
        self.assertEqual((self.root / 'received-prompt.md').read_text(), prompt)

    def test_cleanup_and_vision_lessons_are_delivered_once_when_both_match(self):
        result = self.runner().run(
            'Check a missing optional dependency and clean up stale translation files. '
            'Preserve output files during cleanup.', self.root)
        expected = [self.cleanup, self.vision]
        self.assertEqual(result['project_lessons'], [lesson['id'] for lesson in expected])
        audit = Path(result['audit'])
        self.assertEqual(json.loads((audit / 'project-lessons.json').read_text()),
                         {'selected': expected})
        delivered = (self.root / 'received-prompt.md').read_text()
        for lesson in expected:
            self.assertEqual(delivered.count(lesson['lesson']), 1)

    def test_unrelated_and_partial_cues_do_not_match(self):
        for prompt in (
            '', 'Implement signed_total.', 'Improve error messages for invalid JSON.',
            'Add audio-device recording support.', 'Update the service discovery documentation.',
            'Fix an audio-device diagnostician biography.',
            'Create a responsive layout with inconsistent margins.',
        ):
            with self.subTest(prompt=prompt):
                self.assertEqual(relevant_project_lessons(prompt, self.lessons), [])
        self.assertEqual(lesson_context([]), '')

    def test_invalid_catalog_fails_before_starting_a_session(self):
        catalog = self.root / 'package' / 'lessons'
        catalog.mkdir(parents=True)
        invalid = [None, {}, {**self.lessons[0], 'evidence': ''},
                   {**self.lessons[0], 'status': 'unverified'},
                   *[{**self.lessons[0], 'match_all': groups}
                     for groups in (None, [], ['device'], [[]], [['']], [[4]], [['---']])]]
        for value in invalid:
            with self.subTest(value=value):
                (catalog / 'lesson.json').write_text(json.dumps(value))
                with patch('harness2.project_lessons.files', return_value=catalog.parent):
                    with self.assertRaisesRegex(ValueError, 'Invalid project lesson'):
                        self.runner()
        (catalog / 'lesson.json').write_text('{')
        with patch('harness2.project_lessons.files', return_value=catalog.parent):
            with self.assertRaises(json.JSONDecodeError):
                self.runner()
        self.assertFalse((self.root / 'audit/calls.json').exists())

    def test_duplicate_ids_rejected_and_non_json_files_ignored(self):
        catalog = self.root / 'package' / 'lessons'
        catalog.mkdir(parents=True)
        (catalog / 'README.md').write_text('Not an executable lesson.')
        (catalog / 'a.json').write_text(json.dumps(self.lessons[0]))
        with patch('harness2.project_lessons.files', return_value=catalog.parent):
            self.assertEqual(load_project_lessons(), [self.lessons[0]])
            (catalog / 'b.json').write_text(json.dumps(self.lessons[0]))
            with self.assertRaisesRegex(ValueError, 'Duplicate project lesson'):
                load_project_lessons()

    def test_runner_freezes_catalog_until_a_new_runner_is_created(self):
        runner = self.runner()
        changed = copy.deepcopy(self.lessons)
        changed[0]['lesson'] = 'Revised project guidance.'
        with patch('harness2.local_codex.load_project_lessons', return_value=changed):
            self.assertEqual(runner.lessons_for('Audio device error message'), [self.audio])
            self.assertEqual(self.runner().lessons_for('Audio device error message'), [changed[0]])

    def test_subprocess_receives_matching_lesson_with_exact_audit(self):
        runner = self.runner()
        result = runner.run('Fix the audio-device error message.', self.root)
        self.assertEqual(result['text'], 'lesson received')
        audit = Path(result['audit'])
        delivered = (self.root / 'received-prompt.md').read_text()
        self.assertEqual(delivered, (audit / 'prompt.md').read_text())
        self.assertIn(self.audio['lesson'], delivered)
        self.assertNotIn(self.vision['lesson'], delivered)
        self.assertEqual(json.loads((audit / 'project-lessons.json').read_text()),
                         {'selected': [self.audio]})
        self.assertEqual(result['project_lessons'], [self.audio['id']])
        self.assertFalse((self.root / 'audit/active.json').exists())
        self.assertFalse((self.root / 'audit/library').exists())

    def test_subprocess_receives_vision_lesson_with_exact_audit(self):
        result = self.runner().run('Check the installed vision model before batch processing.',
                                   self.root)
        self.assertEqual(result['text'], 'lesson received')
        audit = Path(result['audit'])
        delivered = (self.root / 'received-prompt.md').read_text()
        self.assertEqual(delivered, (audit / 'prompt.md').read_text())
        self.assertIn(self.vision['lesson'], delivered)
        self.assertNotIn(self.audio['lesson'], delivered)
        self.assertEqual(json.loads((audit / 'project-lessons.json').read_text()),
                         {'selected': [self.vision]})
        self.assertEqual(result['project_lessons'], [self.vision['id']])

    def test_vision_lesson_obeys_task_scope_and_control_setting(self):
        prompt = 'Background: missing vision model. Fix transcript formatting.'
        result = self.runner().run(prompt, self.root, task_prompt='Fix transcript formatting.')
        self.assertEqual(result['project_lessons'], [])
        self.assertEqual((self.root / 'received-prompt.md').read_text(), prompt)
        prompt = 'Fix missing vision model guidance.'
        result = self.runner(use_project_lessons=False).run(prompt, self.root)
        self.assertEqual(result['project_lessons'], [])
        self.assertEqual((self.root / 'received-prompt.md').read_text(), prompt)

    def test_subprocess_omits_unrelated_guidance_and_obeys_explicit_task_scope(self):
        runner = self.runner()
        for prompt, scope in (
            ('Implement signed_total.', None),
            ('Background: audio-device error message. Implement signed_total.',
             'Implement signed_total.'),
            ('Background: audio-device error message.', ''),
        ):
            with self.subTest(prompt=prompt, scope=scope):
                result = runner.run(prompt, self.root, task_prompt=scope)
                self.assertEqual(result['text'], 'no lesson')
                self.assertEqual(result['project_lessons'], [])
                self.assertEqual((self.root / 'received-prompt.md').read_text(), prompt)

    def test_lessons_can_be_disabled_for_a_control_run(self):
        result = self.runner(use_project_lessons=False).run(
            'Fix the audio-device error message.', self.root)
        self.assertEqual(result['text'], 'no lesson')
        self.assertEqual(result['project_lessons'], [])

    def test_proposer_does_not_load_project_lessons_from_its_mission(self):
        runner = self.runner()
        proposer = CodexProposer(ChannelSpec('codex_cli', 'fixture'), runner,
                                self.root / 'proposer-audit')
        reply = proposer.call('Reflect on service discovery diagnostics.', None, 'reflection')
        self.assertEqual(reply.text, 'no lesson')
        self.assertEqual(json.loads((self.root / 'audit/sessions/0001/project-lessons.json')
                                    .read_text()), {'selected': []})


if __name__ == '__main__':
    unittest.main()
