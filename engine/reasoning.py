"""Explicit per-upstream Responses capabilities. Alias names grant no capability."""
import re

LEVELS = ['none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max']
# Deliberately limited to documented-style reasoning extensions. No arbitrary
# request rewriting, headers, tokens, model names, tools or input can be mapped.
FIELDS = ['reasoning.effort', 'reasoning.level', 'reasoning_effort', 'thinking_level']


def validate(mapping):
    from core import UserError
    mode = mapping.get('reasoning', 'off')
    if mode not in ['off', 'forward', 'standard', 'custom']:
        raise UserError('推理参数模式无效。')
    effort = mapping.get('effort', 'low')
    if mode == 'off':
        return dict(reasoning='off', effort='low', levels=[], effort_field='reasoning.effort', effort_values={})
    # Older forward profiles only explicitly declared their default. Preserve
    # that default, without claiming the GPT alias's other levels are supported.
    levels = mapping.get('levels', [effort] if mode == 'forward' else [])
    if mode == 'forward' and levels == []: levels = [effort]
    if (not isinstance(levels, list) or not levels or
            any(not isinstance(x, str) or x not in LEVELS for x in levels) or len(set(levels)) != len(levels)):
        raise UserError('请选择上游明确支持的推理程度；Ultra 不能作为普通 API 推理参数。')
    levels = [x for x in LEVELS if x in levels]
    if effort not in levels: raise UserError('默认推理程度必须包含在此模型已声明的选项中。')
    field = mapping.get('effort_field', 'reasoning.effort') if mode == 'custom' else 'reasoning.effort'
    values = mapping.get('effort_values', {}) if mode == 'custom' else {x:x for x in levels}
    if field not in FIELDS: raise UserError('此版只支持列表中的 Responses 推理扩展字段。')
    if not isinstance(values, dict) or any(not isinstance(values.get(x), str) or
            not re.fullmatch(r'[a-zA-Z0-9_-]{1,40}', values[x]) for x in levels):
        raise UserError('请为每个程度指定上游实际接受的值（1–40 个英文字母、数字、下划线或连字符）。')
    return dict(reasoning=mode, effort=effort, levels=levels, effort_field=field,
                effort_values={x:values[x] for x in levels})


def apply(mapping, body):
    from core import UserError
    rule = validate(mapping)
    incoming = body.pop('reasoning', {}) or {}
    for field in ['reasoning_effort', 'thinking_level']: body.pop(field, None)
    if rule['reasoning'] == 'off': return
    if not isinstance(incoming, dict): raise UserError('推理参数必须为对象。')
    effort = incoming.get('effort') or rule['effort']
    if effort not in rule['levels']:
        raise UserError('当前 API 模型未声明支持这个推理程度，请在 Codex 中改选已配置的程度。')
    parts = rule['effort_field'].split('.')
    value = rule['effort_values'][effort]
    if len(parts) == 2: body[parts[0]] = {parts[1]:value}
    else: body[parts[0]] = value


def configure_model(mapping, model):
    rule = validate(mapping)
    model['supported_reasoning_levels'] = [dict(effort=x, description='API 已配置程度：'+x) for x in rule['levels']]
    model['default_reasoning_level'] = rule['effort'] if rule['levels'] else None
    model['supports_reasoning_effort_updates'] = bool(rule['levels'])
    model['supports_reasoning_summary_parameter'] = False
    model['supports_reasoning_summaries'] = False
