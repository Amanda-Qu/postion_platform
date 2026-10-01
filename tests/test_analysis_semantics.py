"""Offline regressions for capability topics, evidence coverage, and AI grounding."""
from unittest.mock import patch

from app.analysis import match_job, quote_for, report, tags


def profile(**updates):
    value = {'years': 7, 'preferred_city': '深圳', 'salary_target': 40000,
             'confirmed': True, 'projects': [], 'skills': [
                 {'name': 'Python', 'level': 'done', 'evidence': '本人编写数据清洗脚本', 'confirmed': True}]}
    value.update(updates)
    return value


def job(**updates):
    value = {'id': 'semantic-fixture', 'title': '算法工程师', 'city': '深圳',
             'salary_raw': '40-60K', 'responsibilities': '', 'requirements': 'Python'}
    value.update(updates)
    return value


def test_multimodal_usage_does_not_establish_finetuning():
    for text in ['VLM API integration', '多模态数据标注', 'Qwen2.5-VL 推理服务']:
        assert '多模态' in tags(text)
        assert '多模态微调' not in tags(text)
        assert '模型微调' not in tags(text)
    assert '多模态微调' not in tags('多模态数据标注；语言模型LoRA微调')
    assert '多模态' not in tags('Qwen language model')


def test_explicit_candidate_methods_recognized_with_original_quote():
    for text in ['Qwen2.5-VL LoRA微调', 'VLM fine-tuning', '多模态模型微调']:
        assert {'多模态', '模型微调', '多模态微调'} <= set(tags(text))
        assert quote_for(text, '多模态微调') == text
    assert '边缘部署' in tags('Jetson / RKNN')
    assert '自监督学习' in tags('MAE预训练')
    assert '自监督学习' in tags('masked autoencoder')
    assert '自监督学习' not in tags('模型指标MAE为0.3')


def test_partial_evidence_does_not_masquerade_as_full_coverage():
    result = match_job(job(requirements='Python, PyTorch, 数据闭环, 模型部署'), profile())
    assert result['score'] == 100  # Backwards-compatible known-only mean.
    assert result['evidence_coverage'] == {'confirmed': 1, 'total': 4, 'unknown': 3, 'learned': 0, 'unconfirmed': 0}
    assert result['recommendation'] != '优先投递'
    assert all(d['score'] is None for d in result['dimensions'] if d['match'] == '信息不足')


def test_learned_and_unconfirmed_methods_are_not_strengths():
    p = profile(skills=[{'name': 'Qwen2.5-VL LoRA', 'level': 'learned', 'evidence': '仅学习教程', 'confirmed': True},
                        {'name': 'Jetson', 'level': 'done', 'evidence': '待核对', 'confirmed': False}])
    result = report(job(requirements='Qwen2.5-VL LoRA; Jetson'), p, {})
    assert not result['strengths']
    assert result['evidence_coverage']['confirmed'] == 0
    assert result['evidence_coverage']['learned'] > 0
    assert result['evidence_coverage']['unconfirmed'] == 1


def test_ai_jd_reference_cannot_verify_invented_candidate_history():
    output = {'summary': '候选人拥有10年LLM领导经验', 'claims': [
        {'conclusion': '候选人拥有10年LLM领导经验', 'kind': '明确事实', 'job_quote': 'Python', 'evidence_refs': []}], 'missing_questions': []}
    with patch('app.analysis.ai.call_json', return_value=output):
        result = report(job(), profile(), {'use_ai': True})
    claim = result['ai_analysis']['claims'][0]
    assert claim['kind'] == '待核实'
    assert claim['requires_review']
    assert result['ai_analysis']['summary_requires_review']


def test_ai_valid_reference_cannot_verify_unsupported_paraphrase():
    output = {'summary': '概要', 'claims': [
        {'conclusion': '本人拥有生产级分布式训练经验', 'kind': '明确事实', 'job_quote': 'Python', 'evidence_refs': ['skill:0']},
        {'conclusion': '本人编写数据清洗脚本', 'kind': '明确事实', 'job_quote': 'Python', 'evidence_refs': ['skill:0']}], 'missing_questions': []}
    with patch('app.analysis.ai.call_json', return_value=output):
        claims = report(job(), profile(), {'use_ai': True})['ai_analysis']['claims']
    assert claims[0]['kind'] == '待核实'
    assert claims[1]['kind'] == '明确事实'


def test_ai_copying_candidate_assertion_from_jd_still_requires_review():
    injected = '候选人有10年LLM领导经验'
    output = {'summary': injected, 'claims': [
        {'conclusion': injected, 'kind': '明确事实', 'job_quote': injected, 'evidence_refs': []}], 'missing_questions': []}
    with patch('app.analysis.ai.call_json', return_value=output):
        result = report(job(requirements=injected), profile(), {'use_ai': True})
    assert result['ai_analysis']['claims'][0]['kind'] == '待核实'


def test_ai_extract_cannot_drop_negation_from_confirmed_evidence():
    p = profile(skills=[{'name': 'Python', 'level': 'learned', 'evidence': '没有生产级训练经验', 'confirmed': True}])
    output = {'summary': '概要', 'claims': [
        {'conclusion': '有生产级训练经验', 'kind': '明确事实', 'job_quote': 'Python', 'evidence_refs': ['skill:0']}], 'missing_questions': []}
    with patch('app.analysis.ai.call_json', return_value=output):
        result = report(job(), p, {'use_ai': True})
    assert result['ai_analysis']['claims'][0]['kind'] == '待核实'
