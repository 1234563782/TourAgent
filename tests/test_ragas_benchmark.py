"""Regression tests for the policy benchmark adapter; no network or database."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from contextlib import ExitStack

from evals import run_ragas_eval as benchmark


def adapter():
    return vars(benchmark)


class AdapterRegressionTests(unittest.TestCase):
    def test_partial_preserves_limitations_and_next_step(self):
        result = {'rag': {'status': 'partial', 'proposal': {
            'claims': [{'text': '交通标准已核实。'}],
            'limitations': [{'topic': '住宿', 'detail': '住宿档次未知。',
                             'next_step': '请核对账号住宿身份。'}]}}}
        answer = adapter()['extract_answer_text'](result)
        self.assertIn('交通标准已核实', answer)
        self.assertIn('住宿档次未知', answer)
        self.assertIn('请核对账号住宿身份', answer)

    def test_clarify_preserves_actual_question(self):
        result = {'rag': {'status': 'clarify', 'proposal': {'question': '请提供目的地。'}}}
        self.assertEqual(adapter()['extract_answer_text'](result), '请提供目的地。')

    def test_mention_of_clause_is_not_retrieval(self):
        self.assertFalse(adapter()['check_target_clauses'](
            [{'clause': '第九十九条', 'text': '另见第六条。'}], ['第六条']))

    def test_all_required_targets_must_be_retrieved(self):
        self.assertFalse(adapter()['check_target_clauses'](
            [{'clause': '第十八条', 'text': '会议伙食规定'}], ['第十八条', '第二十五条']))

    def test_arabic_clause_normalization_and_partial_coverage(self):
        coverage = benchmark.clause_coverage([{'clause': '第18条'}], ['第十八条', '第二十五条'])
        self.assertTrue(coverage['any'])
        self.assertFalse(coverage['all'])
        self.assertEqual(coverage['missing'], ['第二十五条'])
        self.assertTrue(benchmark.check_target_clauses([{'clause': '第六条'}], ['第6条']))

    def test_current_dataset_has_explicit_inputs_and_rubrics(self):
        cases = benchmark.load_benchmark_cases()
        self.assertEqual(len(cases), 31)
        case = next(c for c in cases if c['id'] == 'CASE-28')
        self.assertNotIn('600', case['ground_truth'])
        self.assertEqual(case['policy_query']['category'], 'meal')
        self.assertEqual(case['facts']['kind'], 'meal')

    def test_incomplete_targets_do_not_become_complete_evidence(self):
        self.assertFalse(benchmark.check_target_clauses([], []))

    def test_unavailable_is_not_judged_as_wrong_policy(self):
        case = benchmark.load_benchmark_cases()[0]
        judge = Mock()
        with patch('business_travel.policy_guard.render_policy', return_value='模型不可用'):
            row = benchmark.evaluate_case(case, {
                'purpose': 'explain', 'sources': [],
                'rag': {'status': 'unavailable', 'diagnostic': {'failure_code': 'model_timeout'}}}, judge)
        judge.assert_not_called()
        self.assertFalse(row['passed'])
        self.assertEqual(row['diagnostic']['failure_code'], 'model_timeout')
        report = benchmark.summary([row], 3)
        self.assertEqual(report['statuses'], {'unavailable': 1})
        self.assertEqual(report['correctness_scored'], 0)
        self.assertEqual(report['pass_rate_all_cases'], 0)

    def test_wrong_condition_fails_even_when_conclusion_is_correct(self):
        case = benchmark.load_benchmark_cases()[0]
        judge = Mock(return_value={'conclusion_correct': True,
                                  'required_point_checks': [False],
                                  'no_material_error': True, 'explanation': '遗漏条件'})
        row = benchmark.evaluate_case(case, {'sources': [], 'rag': {'status': 'answered',
             'proposal': {'claims': [{'text': '看似正确的回答'}]}}}, judge)
        self.assertFalse(row['passed'])
        self.assertEqual(row['correctness_status'], 'scored')

    def test_score_denominators_include_missing_and_failed_cases(self):
        rows = [{'rag_status': 'answered', 'passed': True, 'correctness_status': 'scored',
                 'faithfulness': 1.0}, {'rag_status': 'incomplete', 'passed': False,
                 'faithfulness': float('nan')}]
        report = benchmark.summary(rows, 3)
        self.assertEqual(report['pass_rate_all_cases'], 1/3)
        self.assertEqual(report['metrics']['faithfulness'], {'mean': 1.0, 'valid': 1, 'missing': 1})
        self.assertEqual(report['statuses'], {'answered': 1, 'incomplete': 1})

    def test_validation_rejects_duplicate_id_and_bad_fixture(self):
        case = benchmark.load_benchmark_cases()[0]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'cases.json'
            path.write_text(json.dumps([case, case]), encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'duplicate'):
                benchmark.load_benchmark_cases(path)
            case['facts'] = {'kind': 'hotel'}
            path.write_text(json.dumps([case]), encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'facts.kind'):
                benchmark.load_benchmark_cases(path)

    def test_judge_failure_is_recorded(self):
        case = benchmark.load_benchmark_cases()[0]
        row = benchmark.evaluate_case(case, self.kernel_result(), Mock(side_effect=TimeoutError()))
        self.assertEqual(row['correctness_status'], 'error')
        self.assertEqual(row['judge_error_type'], 'TimeoutError')
        self.assertFalse(row['passed'])

    @staticmethod
    def kernel_result():
        return {'purpose': 'explain', 'sources': [{'clause': '第六条', 'text': '原文',
                'version': '2026-project-v1', 'source_sha256': 'a' * 64}],
                'rag': {'status': 'answered', 'proposal': {'claims': [{'text': '院士可坐商务座。'}]}}}

    def test_correct_answer_cannot_pass_with_wrong_policy_version(self):
        case = benchmark.load_benchmark_cases()[0]
        result = self.kernel_result()
        result['sources'][0]['version'] = 'old-version'
        judge = Mock(return_value={'conclusion_correct': True, 'required_point_checks': [True],
                                  'no_material_error': True, 'explanation': '正确'})
        row = benchmark.evaluate_case(case, result, judge)
        self.assertTrue(row['answer_correct'])
        self.assertFalse(row['policy_version_match'])
        self.assertFalse(row['passed'])

    def test_structured_judge_requires_every_point(self):
        model = Mock()
        model.with_structured_output.return_value.invoke.return_value = {
            'conclusion_correct': True, 'required_point_checks': [],
            'no_material_error': True, 'explanation': '遗漏逐项结果'}
        judge = benchmark.make_correctness_judge(model)
        with self.assertRaisesRegex(ValueError, 'wrong number'):
            judge(benchmark.load_benchmark_cases()[0], '答案')

    def run_isolated(self, directory, *, judge_error=False, ragas_error=False):
        # Exercise the full artifact loop while replacing only external boundaries.
        from business_travel.settings import Settings
        config = Settings(api_key='synthetic', model='fixture', base_url='https://example.invalid',
                          node_path='', flyai_cli_path='', storage_dir=Path(directory),
                          legacy_memory_dir=Path(directory), policy_backend='pgvector',
                          policy_key='hfut_travel')
        verdict = {'conclusion_correct': True, 'required_point_checks': [True],
                   'no_material_error': True, 'explanation': '正确'}
        with ExitStack() as stack:
            stack.enter_context(patch('business_travel.settings.Settings.from_env', return_value=config))
            stack.enter_context(patch('business_travel.settings.Settings.missing_policy_config', return_value=[]))
            stack.enter_context(patch('business_travel.policy.PolicyStore.from_settings', return_value=Mock()))
            stack.enter_context(patch('business_travel.models.create_model', return_value=Mock()))
            kernel = stack.enter_context(patch('business_travel.policy_rag.explain_policy', return_value=self.kernel_result()))
            judge = Mock(side_effect=TimeoutError()) if judge_error else Mock(return_value=verdict)
            stack.enter_context(patch.object(benchmark, 'make_correctness_judge', return_value=judge))
            stack.enter_context(patch.object(benchmark, 'run_ragas', side_effect=RuntimeError() if ragas_error else None))
            stack.enter_context(patch.object(benchmark, 'load_ragas'))
            stack.enter_context(patch('builtins.print'))
            try:
                return benchmark.run_evaluation(1, output=Path(directory)/'run', skip_ragas=not ragas_error)
            finally:
                args, kwargs = kernel.call_args
                self.assertEqual(args[3], benchmark.load_benchmark_cases()[0]['question'])
                self.assertEqual(set(kwargs), {'facts', 'policy_query', 'request_context'})
                self.assertNotIn('ground_truth', kwargs['request_context'])
                self.assertEqual(kwargs['facts'], {'kind': 'transport'})

    def test_run_persists_result_before_judge_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(RuntimeError):
                self.run_isolated(directory, judge_error=True)
            root = Path(directory)/'run'
            self.assertTrue((root/'raw.jsonl').read_text(encoding='utf-8'))
            row = json.loads((root/'cases.jsonl').read_text(encoding='utf-8'))
            self.assertEqual(row['correctness_status'], 'error')
            manifest = json.loads((root/'manifest.json').read_text(encoding='utf-8'))
            self.assertEqual(manifest['status'], 'failed')
            self.assertNotIn('api_key', manifest)

    def test_ragas_failure_keeps_kernel_and_correctness_results(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(RuntimeError):
                self.run_isolated(directory, ragas_error=True)
            root = Path(directory)/'run'
            row = json.loads((root/'cases.jsonl').read_text(encoding='utf-8'))
            self.assertTrue(row['passed'])
            self.assertTrue((root/'report.csv').exists())
            self.assertEqual(json.loads((root/'manifest.json').read_text(encoding='utf-8'))['status'], 'failed')

    def test_successful_isolated_run_has_hashes_and_denominators(self):
        with tempfile.TemporaryDirectory() as directory:
            report = self.run_isolated(directory)
            self.assertEqual(report['correctness_passed'], 1)
            self.assertEqual(report['metrics']['faithfulness']['missing'], 1)
            manifest = json.loads((Path(directory)/'run/manifest.json').read_text(encoding='utf-8'))
            self.assertEqual(manifest['ragas'], 'skipped')
            self.assertEqual(len(manifest['dataset_sha256']), 64)
            self.assertIn(['2026-project-v1', 'a'*64], manifest['policy_sources'])

    def test_cli_exit_status_is_nonzero_for_failed_cases(self):
        with patch.object(benchmark, 'run_evaluation', return_value={'correctness_passed': 1, 'total': 2}):
            self.assertEqual(benchmark.main(['--skip-ragas']), 1)

    def test_empty_or_invalid_targets_never_hit(self):
        self.assertFalse(benchmark.check_target_clauses([{'clause': ''}], ['invalid']))

    def test_ragas_maps_only_eligible_rows_without_inventing_context(self):
        eligible = {'case_id': 'one', 'rag_status': 'partial', 'question': '问题',
                    'response': '结论及限制', 'reference': '参考', 'contexts': ['原文']}
        missing = {'case_id': 'two', 'rag_status': 'insufficient', 'contexts': []}
        evaluate, dataset = Mock(), Mock()
        evaluate.return_value.to_pandas.return_value.to_dict.return_value = [
            {'faithfulness': 1.0, 'context_recall': .5, 'context_precision': .25}]
        with patch.object(benchmark, 'load_ragas', return_value=(evaluate, ('a', 'b', 'c'), dataset)):
            benchmark.run_ragas([eligible, missing], Mock())
        payload = dataset.from_dict.call_args.args[0]
        self.assertEqual(payload['contexts'], [['原文']])
        self.assertEqual(payload['answer'], ['结论及限制'])
        self.assertEqual(eligible['context_precision'], .25)
        self.assertNotIn('faithfulness', missing)
        self.assertTrue(evaluate.call_args.kwargs['raise_exceptions'])

    def test_nan_ragas_metrics_fail_with_available_scores_retained(self):
        row = {'rag_status': 'answered', 'question': '问题', 'response': '回答',
               'reference': '参考', 'contexts': ['原文']}
        evaluate = Mock()
        evaluate.return_value.to_pandas.return_value.to_dict.return_value = [
            {'faithfulness': float('nan'), 'context_recall': 1.0, 'context_precision': 1.0}]
        with patch.object(benchmark, 'load_ragas', return_value=(evaluate, (), Mock())):
            with self.assertRaisesRegex(ValueError, 'non-finite'):
                benchmark.run_ragas([row], Mock())
        self.assertIsNone(row['faithfulness'])
        self.assertEqual(row['context_recall'], 1.0)



if __name__ == '__main__':
    unittest.main()
