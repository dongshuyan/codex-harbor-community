"""Reference-catalog acceptance; no credentials, runtime history or paid calls."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'engine'))
import model_metadata as metadata
import core
import reasoning


class CherryCatalogTests(unittest.TestCase):
    def test_astra_sparse_api_gets_context_and_five_reference_efforts(self):
        result = metadata.resolve('gpt-6-astra', {'id': 'gpt-6-astra'}, metadata.public_catalog())
        self.assertEqual(result['context'], 1050000)
        self.assertEqual(result['levels'], ['low', 'medium', 'high', 'xhigh', 'max'])
        self.assertEqual(result['reasoning'], 'standard')
        self.assertEqual(result['context_source'], 'catalog')
        self.assertEqual(result['reasoning_source'], 'catalog')
        self.assertEqual(result['catalog_provider'], 'cherry')
        self.assertEqual(result['reasoning_catalog_provider'], 'cherry')
        self.assertEqual(result['reasoning_catalog_model'], 'gpt-6-astra')
        body = {'reasoning': {'effort': 'max'}}
        reasoning.apply(result, body)
        self.assertEqual(body, {'reasoning': {'effort': 'max'}})

    def test_provider_fields_override_reference_independently(self):
        result = metadata.resolve('gpt-6-astra', {'context_window': 256000,
            'supported_reasoning_efforts': ['low', 'high'], 'reasoning_field': 'thinking_level',
            'effort_values': {'low': 'brief', 'high': 'deep'}}, {})
        self.assertEqual((result['context'], result['context_source']), (256000, 'router'))
        self.assertEqual((result['levels'], result['reasoning_source']), (['low', 'high'], 'router'))
        self.assertEqual(result['effort_field'], 'thinking_level')
        partial = metadata.resolve('gpt-6-astra', {'context_window': 256000}, {})
        self.assertEqual(partial['context_source'], 'router')
        self.assertEqual(partial['reasoning_catalog_provider'], 'cherry')

    def test_explicit_provider_rejection_is_not_overruled_by_catalog(self):
        for record in ({'reasoning': False}, {'supported_reasoning_efforts': []},
                       {'supported_reasoning_efforts': ['alien']},
                       {'supported_reasoning_efforts': ['low'], 'reasoning_field': 'Authorization'}):
            with self.subTest(record=record):
                result = metadata.resolve('gpt-6-astra', record, {})
                self.assertEqual(result['reasoning'], 'off')
                self.assertEqual(result['levels'], [])

    def test_conflicting_api_records_do_not_enable_catalog_capabilities(self):
        response = MagicMock(); response.__enter__.return_value = response; response.status = 200
        response.read.return_value = json.dumps({'data': [
            {'id': 'gpt-6-astra', 'reasoning': False, 'context_window': 128000},
            {'id': 'gpt-6-astra', 'supported_reasoning_efforts': ['low'], 'context_window': 256000},
            {'id': 'glm-5.3'}]}).encode()
        with patch('model_metadata.urllib.request.build_opener') as opener:
            opener.return_value.open.return_value = response
            result = metadata.fetch_worker(dict(url='https://example.invalid/v1', key='SYNTHETIC_KEY',
                models=['gpt-6-astra', 'glm-5.3']))
        conflict = result['models']['gpt-6-astra']
        self.assertEqual(conflict['reasoning'], 'off')
        self.assertEqual(conflict['levels'], [])
        self.assertEqual(conflict['context_source'], 'preset')
        self.assertEqual(result['status'], 'conflicting_metadata')
        self.assertEqual(result['models']['glm-5.3']['reasoning_catalog_provider'], 'cherry')

    def test_model_prefix_and_version_separator_keep_actual_model_identity(self):
        result = metadata.resolve('infi/glm-5.3', {}, {})
        self.assertEqual(result['catalog_model'], 'glm-5-3')
        self.assertEqual(result['levels'], ['low', 'high', 'max'])
        self.assertEqual(result['context'], 1310720)
        for unknown in ('synthetic/unknown-model', 'glm-5.3-thinking', 'glm-5.3:free', 'glm-5.4'):
            with self.subTest(unknown=unknown):
                result = metadata.resolve(unknown, {}, {})
                self.assertEqual((result['context'], result['reasoning']), (128000, 'off'))

    def test_budget_or_toggle_controls_do_not_invent_effort_wire(self):
        for model in ('claude-sonnet-4-5', 'claude-opus-4-5', 'mimo-v2-5-pro'):
            with self.subTest(model=model):
                result = metadata.resolve(model, {}, {})
                self.assertEqual(result['catalog_provider'], 'cherry')
                self.assertEqual(result['reasoning'], 'off')
                self.assertEqual(result['levels'], [])

    def test_ambiguous_normalized_ids_are_not_guessed(self):
        models = {'vendor/glm-5.3': {'contextWindow': 256000},
                  'other/glm-5-3': {'contextWindow': 128000}}
        self.assertEqual(metadata.reference_match('infi/glm-5.3', models), ('', {}))
        self.assertEqual(metadata.reference_match('vendor/glm-5.3', models)[0], 'vendor/glm-5.3')

    def test_every_bundled_record_resolves_to_a_valid_responses_rule(self):
        for name in metadata.cherry_catalog():
            with self.subTest(name=name):
                result = metadata.resolve(name, {}, {})
                self.assertIsNotNone(metadata.context_number(result['context']))
                reasoning.validate(result)
                self.assertNotIn('ultra', result['levels'])

    def test_failed_metadata_request_still_returns_offline_reference(self):
        import urllib.error
        with patch('model_metadata.urllib.request.build_opener') as opener:
            opener.return_value.open.side_effect = urllib.error.URLError('synthetic offline')
            result = metadata.fetch_worker(dict(url='https://example.invalid/v1', key='SYNTHETIC_KEY',
                models=['gpt-6-astra', 'synthetic/unknown-model']))
        self.assertEqual(result['status'], 'network_error')
        self.assertEqual(result['models']['gpt-6-astra']['reasoning_catalog_provider'], 'cherry')
        self.assertEqual(result['models']['synthetic/unknown-model']['context_source'], 'preset')
        self.assertNotIn('SYNTHETIC_KEY', json.dumps(result))

    def test_catalog_sources_survive_save_reload_without_changing_key(self):
        with tempfile.TemporaryDirectory() as folder:
            store = core.Store(Path(folder) / 'control', Path(folder) / 'external', mounted=lambda: True, community=False)
            resolved = metadata.resolve('gpt-6-astra', {}, {})
            mapping = dict(resolved, alias='gpt-6-luna', upstream='gpt-6-astra', parameters={
                'mode': 'auto', 'manual': [], 'context_source': resolved['context_source'],
                'reasoning_source': resolved['reasoning_source'], 'catalog_model': resolved['catalog_model'],
                'reasoning_catalog_model': resolved.get('reasoning_catalog_model', ''),
                'catalog_provider': resolved['catalog_provider'],
                'reasoning_catalog_provider': resolved['reasoning_catalog_provider']})
            profile = store.save_profile(dict(name='Synthetic', kind='api', protocol='responses',
                url='https://example.invalid/v1', api_key='SYNTHETIC_KEY', models=[mapping]))
            saved = store.public({'id': profile['id']})['models'][0]
            self.assertEqual(saved['parameters']['reasoning_catalog_model'], 'gpt-6-astra')
            self.assertEqual(saved['parameters']['reasoning_catalog_provider'], 'cherry')
            self.assertEqual(saved['levels'], resolved['levels'])
            self.assertEqual(store.secret(profile['id']), 'SYNTHETIC_KEY')


if __name__ == '__main__':
    unittest.main()
